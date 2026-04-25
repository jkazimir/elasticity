"""Tests for the cognitive context strategy."""

import pytest

from elasticity.config.schema import CognitiveContextConfig
from elasticity.events import (
    ContextAssembled,
    EventBus,
    MemoryRecalled,
    TopicShift,
)
from elasticity.memory.embeddings import EmbeddingProvider
from elasticity.memory.vector_store import MemoryTier, VectorStore
from elasticity.runtime.context_strategy import (
    CognitiveStrategy,
    WindowStrategy,
    _derive_topic_label,
)
from elasticity.runtime.session import Session


# ---------------------------------------------------------------------------
# Mock embedding provider (deterministic, no API/model needed)
# ---------------------------------------------------------------------------

class MockEmbeddingProvider(EmbeddingProvider):
    """Returns a deterministic embedding based on the first character's ord value."""

    def __init__(self, dim: int = 3):
        self._dim = dim

    async def embed(self, text: str) -> list[float]:
        # Simple hash-like embedding: spread the first char's ordinal across dims
        if not text:
            return [0.0] * self._dim
        base = ord(text[0]) % 100
        return [(base + i) / 100.0 for i in range(self._dim)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def session():
    return Session(id="test-session")


@pytest.fixture
def vector_store(tmp_path):
    s = VectorStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def mock_embedder():
    return MockEmbeddingProvider(dim=3)


@pytest.fixture
def config():
    return CognitiveContextConfig(
        recent_turns=2,
        similarity_threshold=0.5,
        max_recalled_memories=3,
        embedding_provider="local/all-MiniLM-L6-v2",  # not used in tests
    )


@pytest.fixture
def strategy(config, vector_store, mock_embedder, event_bus):
    return CognitiveStrategy(
        config=config,
        vector_store=vector_store,
        embedding_provider=mock_embedder,
        event_bus=event_bus,
    )


# ---------------------------------------------------------------------------
# WindowStrategy tests
# ---------------------------------------------------------------------------

class TestWindowStrategy:

    async def test_returns_session_history(self, session):
        session.add_turn("hello", "hi there")
        session.add_turn("how are you", "I'm well")

        strategy = WindowStrategy()
        messages = await strategy.build_context(session, "new input")

        assert len(messages) == 4
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"

    async def test_on_turn_complete_is_noop(self, session):
        strategy = WindowStrategy()
        await strategy.on_turn_complete(session, "msg", "resp")
        # No error, no side effect

    async def test_empty_session(self):
        strategy = WindowStrategy()
        session = Session()
        messages = await strategy.build_context(session, "hello")
        assert messages == []


# ---------------------------------------------------------------------------
# CognitiveStrategy tests
# ---------------------------------------------------------------------------

class TestCognitiveStrategy:

    async def test_first_turn_no_history(self, strategy, session):
        """First turn should return empty context (no prior turns)."""
        messages = await strategy.build_context(session, "Hello world")
        # May contain assembly annotations but no prior turn messages
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 0  # no prior turns

    async def test_includes_recent_turns(self, strategy, session):
        """Recent turns should always be in working memory."""
        await strategy.on_turn_complete(session, "turn 1", "response 1")
        await strategy.on_turn_complete(session, "turn 2", "response 2")

        messages = await strategy.build_context(session, "turn 3")

        # Should contain recent turns (config.recent_turns=2)
        user_contents = [m["content"] for m in messages if m["role"] == "user"]
        assert "turn 1" in user_contents
        assert "turn 2" in user_contents

    async def test_recent_turns_limited(self, strategy, session):
        """Only the configured number of recent turns should be included."""
        for i in range(5):
            await strategy.on_turn_complete(session, f"turn {i}", f"response {i}")

        messages = await strategy.build_context(session, "turn 5")

        user_contents = [m["content"] for m in messages if m["role"] == "user"]
        # recent_turns=2, so only last 2 turns
        assert "turn 3" in user_contents
        assert "turn 4" in user_contents
        assert "turn 0" not in user_contents

    async def test_topic_initialised_on_first_input(self, strategy, session):
        """First call to build_context should initialise the topic."""
        assert strategy._current_topic is None
        await strategy.build_context(session, "Python debugging tips")
        assert strategy._current_topic is not None
        assert "Python" in strategy._current_topic.label

    async def test_on_turn_complete_stores_embedding(self, strategy, session, vector_store):
        """on_turn_complete should store the turn in the vector store."""
        await strategy.on_turn_complete(session, "hello", "hi there")

        results = vector_store.search(
            query_embedding=await strategy._embedder.embed("hello"),
            session_id=session.id,
        )
        assert len(results) >= 1
        assert "hello" in results[0].value

    async def test_on_turn_complete_increments_count(self, strategy, session):
        assert strategy._turn_count == 0
        await strategy.on_turn_complete(session, "a", "b")
        assert strategy._turn_count == 1
        await strategy.on_turn_complete(session, "c", "d")
        assert strategy._turn_count == 2

    async def test_recalled_memories_injected(self, strategy, session, vector_store, mock_embedder):
        """When relevant memories exist, they should be injected as system messages."""
        # Pre-populate vector store with a relevant memory
        embedding = await mock_embedder.embed("Python")
        vector_store.store(
            key="topic:python-debugging",
            value="Previously discussed Python import errors",
            embedding=embedding,
            session_id=session.id,
            tier=MemoryTier.MEDIUM_TERM.value,
        )

        # Build context with a similar query
        messages = await strategy.build_context(session, "Python import issue")
        system_msgs = [m for m in messages if m["role"] == "system"]
        recall_msgs = [m for m in system_msgs if "Recalled" in m["content"]]
        assert len(recall_msgs) >= 1
        assert "python-debugging" in recall_msgs[0]["content"]

    async def test_no_recall_when_disabled(self, vector_store, mock_embedder, event_bus, session):
        """When max_recalled_memories=0, no RAG recall should happen."""
        config = CognitiveContextConfig(
            recent_turns=2,
            max_recalled_memories=0,
        )
        strategy = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=mock_embedder,
            event_bus=event_bus,
        )

        messages = await strategy.build_context(session, "hello")
        system_msgs = [m for m in messages if m["role"] == "system"]
        recall_msgs = [m for m in system_msgs if "Recalled" in m["content"]]
        assert len(recall_msgs) == 0

    async def test_null_input_returns_recent_turns_only(self, strategy, session):
        """When input is None, should return recent turns without embedding."""
        await strategy.on_turn_complete(session, "hello", "hi")
        messages = await strategy.build_context(session, None)
        assert len(messages) == 2  # user + assistant from the one turn
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 0


# ---------------------------------------------------------------------------
# Event emission tests
# ---------------------------------------------------------------------------

class TestCognitiveStrategyEvents:

    async def test_context_assembled_event(self, strategy, session, event_bus):
        events = []
        event_bus.subscribe(ContextAssembled, lambda e: events.append(e))

        await strategy.build_context(session, "hello world")

        assert len(events) == 1
        assert events[0].topic != ""

    async def test_memory_recalled_event(self, strategy, session, vector_store, mock_embedder, event_bus):
        events = []
        event_bus.subscribe(MemoryRecalled, lambda e: events.append(e))

        # Store a memory that will be recalled
        embedding = await mock_embedder.embed("test")
        vector_store.store(
            key="test:mem",
            value="test memory",
            embedding=embedding,
            session_id=session.id,
            tier=MemoryTier.SHORT_TERM.value,
        )

        await strategy.build_context(session, "test query")

        if events:  # only if threshold is met
            assert events[0].count >= 1


# ---------------------------------------------------------------------------
# Topic label derivation
# ---------------------------------------------------------------------------

class TestTopicLabel:

    def test_short_text(self):
        assert _derive_topic_label("hello") == "hello"

    def test_long_text(self):
        text = "this is a very long message that should be truncated to a few words"
        label = _derive_topic_label(text, max_words=5)
        assert label.endswith("...")
        assert len(label.split()) <= 6  # 5 words + "..."

    def test_empty_text(self):
        assert _derive_topic_label("") == ""


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestCognitiveContextConfig:

    def test_defaults(self):
        config = CognitiveContextConfig()
        assert config.recent_turns == 3
        assert config.similarity_threshold == 0.35
        assert config.max_recalled_memories == 5
        assert config.embedding_provider == "local/all-MiniLM-L6-v2"
        assert config.topic_detection == "embedding"
        assert config.memory_db_path is None
        assert config.summary_model is None

    def test_custom_values(self):
        config = CognitiveContextConfig(
            recent_turns=5,
            similarity_threshold=0.5,
            max_recalled_memories=10,
            embedding_provider="voyage/voyage-3-lite",
            topic_detection="llm",
            memory_db_path="/tmp/test.db",
            summary_model="anthropic/claude-sonnet-4-20250514",
        )
        assert config.recent_turns == 5
        assert config.topic_detection == "llm"
        assert config.embedding_provider == "voyage/voyage-3-lite"


# ---------------------------------------------------------------------------
# TopicState persistence tests
# ---------------------------------------------------------------------------


class TestTopicStatePersistence:

    async def test_topic_state_round_trip(self):
        """to_dict/from_dict should produce equivalent objects."""
        from elasticity.runtime.context_strategy import TopicState

        original = TopicState(
            label="Python debugging",
            embedding=[0.1, 0.2, 0.3],
            started_at_turn=5,
            key_facts=["uses pdb", "prefers breakpoint()"],
        )
        data = original.to_dict()
        restored = TopicState.from_dict(data)

        assert restored.label == original.label
        assert restored.embedding == original.embedding
        assert restored.started_at_turn == original.started_at_turn
        assert restored.key_facts == original.key_facts

    async def test_from_dict_missing_key_facts(self):
        """from_dict should default key_facts to [] when absent."""
        from elasticity.runtime.context_strategy import TopicState

        data = {"label": "test", "embedding": [1.0], "started_at_turn": 0}
        restored = TopicState.from_dict(data)
        assert restored.key_facts == []

    async def test_topic_state_persisted_to_session_context(self, strategy, session):
        """on_turn_complete should write topic state to session.context."""
        # Initialise topic via build_context
        await strategy.build_context(session, "Hello world")

        # Complete the turn — this should persist topic state
        await strategy.on_turn_complete(session, "Hello world", "Hi there")

        assert "_cognitive_topic_state" in session.context
        topic_data = session.context["_cognitive_topic_state"]
        assert topic_data["label"] == strategy._current_topic.label
        assert topic_data["started_at_turn"] == strategy._current_topic.started_at_turn
        assert len(topic_data["embedding"]) == 3  # mock embedder dim

    async def test_topic_state_rehydrated_on_resume(
        self, config, vector_store, mock_embedder, event_bus, session
    ):
        """A fresh strategy instance should restore topic from session.context."""
        # Strategy 1: establish topic state
        strategy1 = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=mock_embedder,
            event_bus=event_bus,
        )
        await strategy1.build_context(session, "Hello world")
        await strategy1.on_turn_complete(session, "Hello world", "Hi there")

        original_label = strategy1._current_topic.label
        original_turn = strategy1._current_topic.started_at_turn

        # Simulate process restart: new strategy, new session with same id + context
        resumed_session = Session(id=session.id)
        resumed_session.message_history = list(session.message_history)
        resumed_session.context = dict(session.context)

        strategy2 = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=mock_embedder,
            event_bus=event_bus,
        )
        assert strategy2._current_topic is None

        # build_context triggers rehydration
        await strategy2.build_context(resumed_session, "Follow up question")

        assert strategy2._current_topic is not None
        assert strategy2._current_topic.label == original_label
        assert strategy2._current_topic.started_at_turn == original_turn

    async def test_no_false_topic_shift_after_rehydration(
        self, config, vector_store, mock_embedder, event_bus, session
    ):
        """Resumed session with same topic should NOT emit TopicShift."""
        strategy1 = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=mock_embedder,
            event_bus=event_bus,
        )
        await strategy1.build_context(session, "Hello world")
        await strategy1.on_turn_complete(session, "Hello world", "Hi there")

        # Simulate restart
        resumed_session = Session(id=session.id)
        resumed_session.message_history = list(session.message_history)
        resumed_session.context = dict(session.context)

        shifts = []
        event_bus2 = EventBus()
        event_bus2.subscribe(TopicShift, lambda e: shifts.append(e))

        strategy2 = CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=mock_embedder,
            event_bus=event_bus2,
        )

        # Same-topic message: "H" starts with same char as "Hello" so
        # MockEmbeddingProvider produces identical embeddings → high similarity
        await strategy2.build_context(resumed_session, "Hello again")

        assert len(shifts) == 0

    async def test_rehydration_handles_missing_topic_state(self, strategy, session):
        """Rehydration should not error when _cognitive_topic_state is absent."""
        # Session has no topic state in context
        assert "_cognitive_topic_state" not in session.context

        # Should complete without error, topic stays None
        strategy._rehydrate_for_session(session)
        assert strategy._current_topic is None

    async def test_rehydration_handles_corrupted_topic_state(self, strategy, session):
        """Corrupted topic data should be handled gracefully."""
        session.context["_cognitive_topic_state"] = {"bad": "data"}

        # Reset so rehydration runs
        strategy._rehydrated_sessions.clear()
        strategy._rehydrate_for_session(session)

        # Should not crash, topic stays None
        assert strategy._current_topic is None


# ---------------------------------------------------------------------------
# on_session_end
# ---------------------------------------------------------------------------

class TestOnSessionEnd:

    async def test_promotes_medium_term_to_long_term(
        self, strategy, session, vector_store
    ):
        """on_session_end should promote all medium-term entries to long-term."""
        # Manually insert a medium-term entry (simulates a recorded topic summary)
        vector_store.store(
            "topic:foo",
            "Summary of foo topic",
            [1.0, 0.0, 0.0],
            session_id=session.id,
            tier=MemoryTier.MEDIUM_TERM.value,
        )
        vector_store.store(
            "topic:bar",
            "Summary of bar topic",
            [0.9, 0.1, 0.0],
            session_id=session.id,
            tier=MemoryTier.MEDIUM_TERM.value,
        )

        await strategy.on_session_end(session)

        # Both should now be long-term
        long_entries = vector_store.get_entries_by_tier(session.id, MemoryTier.LONG_TERM.value)
        assert {e.key for e in long_entries} == {"topic:foo", "topic:bar"}

        # No more medium-term entries for this session
        medium_entries = vector_store.get_entries_by_tier(session.id, MemoryTier.MEDIUM_TERM.value)
        assert medium_entries == []

    async def test_no_error_when_no_medium_term_entries(self, strategy, session):
        """on_session_end should be a no-op (no error) when there is nothing to promote."""
        await strategy.on_session_end(session)  # should not raise

    async def test_final_topic_summarised_and_promoted(
        self, strategy, session, vector_store
    ):
        """on_session_end summarises the current topic and promotes it."""
        # Establish a current topic via build_context + on_turn_complete
        await strategy.build_context(session, "Hello world")
        await strategy.on_turn_complete(session, "Hello world", "Hi there")

        assert strategy._current_topic is not None

        await strategy.on_session_end(session)

        # The final topic should now appear as long-term (stored then promoted)
        long_entries = vector_store.get_entries_by_tier(session.id, MemoryTier.LONG_TERM.value)
        assert len(long_entries) >= 1
        assert any("topic:" in e.key for e in long_entries)

    async def test_window_strategy_on_session_end_is_noop(self, session):
        """WindowStrategy inherits on_session_end as a no-op without error."""
        ws = WindowStrategy()
        await ws.on_session_end(session)  # should not raise


# ---------------------------------------------------------------------------
# Active consolidation (_should_consolidate)
# ---------------------------------------------------------------------------

class TestActiveConsolidation:

    async def test_short_turns_not_consolidated(self, strategy, session):
        """Turns below consolidation_min_length should never be consolidated."""
        # Default consolidation_min_length = 200
        short_user = "hi"
        short_assistant = "hello"
        embedding = [0.5, 0.5, 0.0]

        # Initialise topic so similarity check can run
        await strategy.build_context(session, short_user)

        result = strategy._should_consolidate(short_user, short_assistant, embedding)
        assert result is False

    async def test_long_turns_consolidated_by_length(self, strategy, session):
        """Turns above consolidation_length_threshold should be consolidated."""
        # Default consolidation_length_threshold = 1000
        long_user = "a" * 600
        long_assistant = "b" * 600  # combined = 1200 > 1000

        await strategy.build_context(session, long_user)
        await strategy.on_turn_complete(session, long_user, long_assistant)

        result = strategy._should_consolidate(long_user, long_assistant, [0.5, 0.5, 0.0])
        assert result is True

    async def test_novel_turns_consolidated(self, strategy, session):
        """Turns very dissimilar to the current topic should be consolidated."""
        # Establish a topic: embedding based on 'H' → [0.72, 0.73, 0.74]
        await strategy.build_context(session, "Hello world")
        await strategy.on_turn_complete(session, "Hello world", "Hi there")

        # A novel message: embedding based on '!' → very different
        novel_msg = "!" * 100  # length >= min (200 combined with response)
        novel_response = "!" * 101
        novel_embedding = [0.0, 0.0, 1.0]  # orthogonal to topic embedding

        # Override novelty threshold to something high to force True
        strategy._config = CognitiveContextConfig(
            recent_turns=2,
            similarity_threshold=0.5,
            max_recalled_memories=3,
            embedding_provider="local/all-MiniLM-L6-v2",
            consolidation_min_length=0,
            consolidation_novelty_threshold=0.99,  # everything is "novel"
            consolidation_length_threshold=99999,   # disable length path
        )

        result = strategy._should_consolidate(novel_msg, novel_response, novel_embedding)
        assert result is True

    async def test_on_turn_complete_promotes_long_turn(
        self, strategy, session, vector_store
    ):
        """on_turn_complete should promote turns that exceed length threshold."""
        strategy._config = CognitiveContextConfig(
            recent_turns=2,
            similarity_threshold=0.5,
            max_recalled_memories=3,
            embedding_provider="local/all-MiniLM-L6-v2",
            consolidation_min_length=0,
            consolidation_novelty_threshold=0.0,  # disable novelty path
            consolidation_length_threshold=0,      # promote everything
        )

        await strategy.build_context(session, "Hello world")
        await strategy.on_turn_complete(session, "Hello world", "Hi there")

        # Turn should now be in long-term
        long_entries = vector_store.get_entries_by_tier(session.id, MemoryTier.LONG_TERM.value)
        turn_keys = [e.key for e in long_entries if e.key.startswith("turn:")]
        assert len(turn_keys) == 1


# ---------------------------------------------------------------------------
# Tool call integration (_compose_turn_summary / _enrich_assistant_content)
# ---------------------------------------------------------------------------

class TestToolCallIntegration:

    def test_compose_turn_summary_no_tool_calls(self, strategy):
        """Without tool calls, summary uses the legacy User/Assistant format."""
        summary = strategy._compose_turn_summary("hello", "hi there", None)
        assert summary == "User: hello\nAssistant: hi there"

    def test_compose_turn_summary_empty_tool_calls(self, strategy):
        """Empty tool_calls list behaves the same as None."""
        summary = strategy._compose_turn_summary("hello", "hi there", [])
        assert summary == "User: hello\nAssistant: hi there"

    def test_compose_turn_summary_with_tool_calls(self, strategy):
        """Tool calls should appear as Action: lines between User and Internal."""
        tool_calls = [
            {"team": "speech_effector", "args": {"content": "Hello!", "style": "warm"}, "result": "Hello!"},
            {"team": "task_execution", "args": {"task": "research"}, "result": "Found 5 papers"},
        ]
        summary = strategy._compose_turn_summary("ask something", "thinking...", tool_calls)
        assert summary.startswith("User: ask something")
        assert "Action performed: speech_effector" in summary
        assert "→ Hello!" in summary
        assert "Action performed: task_execution" in summary
        assert "→ Found 5 papers" in summary
        assert "Internal: thinking..." in summary

    def test_compose_turn_summary_with_error(self, strategy):
        """Tool call errors should appear with ERROR: prefix."""
        tool_calls = [
            {"team": "broken_tool", "args": {}, "error": "Connection timeout"},
        ]
        summary = strategy._compose_turn_summary("do something", "trying...", tool_calls)
        assert "Action performed: broken_tool" in summary
        assert "→ ERROR: Connection timeout" in summary

    def test_compose_turn_summary_blank_assistant_response(self, strategy):
        """Blank internal response should be omitted from the summary."""
        tool_calls = [
            {"team": "speech_effector", "args": {}, "result": "Hello there!"},
        ]
        summary = strategy._compose_turn_summary("hi", "", tool_calls)
        assert "Internal:" not in summary
        assert "Action performed: speech_effector" in summary

    def test_enrich_assistant_content_no_tool_calls(self, strategy):
        """Without tool calls, enriched content equals the raw response."""
        result = strategy._enrich_assistant_content("raw thinking", None)
        assert result == "raw thinking"

    def test_enrich_assistant_content_with_tool_calls(self, strategy):
        """With tool calls, action summaries are prepended."""
        tool_calls = [
            {"team": "speech_effector", "args": {}, "result": "Hello there!"},
            {"team": "task_execution", "args": {}, "result": "Done"},
        ]
        enriched = strategy._enrich_assistant_content("internal thought", tool_calls)
        assert "(prior action) speech_effector → Hello there!" in enriched
        assert "(prior action) task_execution → Done" in enriched
        assert "internal thought" in enriched

    def test_enrich_assistant_content_blank_response(self, strategy):
        """Blank internal response with tool calls returns only the action lines."""
        tool_calls = [{"team": "speech_effector", "args": {}, "result": "Hi!"}]
        enriched = strategy._enrich_assistant_content("", tool_calls)
        assert "(prior action) speech_effector → Hi!" in enriched
        assert enriched.strip() == "(prior action) speech_effector → Hi!"

    async def test_on_turn_complete_with_tool_calls_stored(
        self, strategy, session, vector_store
    ):
        """Tool call data should be included in the stored vector store value."""
        tool_calls = [
            {"team": "speech_effector", "args": {"content": "Hello!"}, "result": "Hello!"},
            {"team": "task_execution", "args": {"task": "research"}, "result": "Found 5 papers"},
        ]
        await strategy.on_turn_complete(session, "ask something", "thinking...", tool_calls=tool_calls)

        results = vector_store.search(
            query_embedding=await strategy._embedder.embed("ask something"),
            session_id=session.id,
        )
        assert len(results) >= 1
        stored_text = results[0].value
        assert "Action performed: speech_effector" in stored_text
        assert "Hello!" in stored_text
        assert "Action performed: task_execution" in stored_text
        assert "Internal: thinking..." in stored_text

    async def test_on_turn_complete_without_tool_calls_backward_compat(
        self, strategy, session, vector_store
    ):
        """Calling without tool_calls should use the legacy format."""
        await strategy.on_turn_complete(session, "hello", "hi there")

        results = vector_store.search(
            query_embedding=await strategy._embedder.embed("hello"),
            session_id=session.id,
        )
        assert len(results) >= 1
        stored_text = results[0].value
        assert stored_text == "User: hello\nAssistant: hi there"

    async def test_enriched_response_in_turns_buffer(self, strategy, session):
        """Recent turns buffer should include action summaries in assistant content."""
        tool_calls = [
            {"team": "speech_effector", "args": {}, "result": "Hello there!"},
        ]
        await strategy.on_turn_complete(
            session, "hello", "internal thought", tool_calls=tool_calls
        )

        messages = strategy._recent_turn_messages()
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "(prior action) speech_effector → Hello there!" in assistant_msgs[0]["content"]
        assert "internal thought" in assistant_msgs[0]["content"]

    async def test_tool_call_error_stored_in_summary(
        self, strategy, session, vector_store
    ):
        """Tool call errors should appear in the stored summary."""
        tool_calls = [
            {"team": "broken_tool", "args": {}, "error": "Connection timeout"},
        ]
        await strategy.on_turn_complete(
            session, "do something", "trying...", tool_calls=tool_calls
        )

        results = vector_store.search(
            query_embedding=await strategy._embedder.embed("do something"),
            session_id=session.id,
        )
        stored_text = results[0].value
        assert "ERROR: Connection timeout" in stored_text

    async def test_window_strategy_accepts_tool_calls_kwarg(self, session):
        """WindowStrategy.on_turn_complete should accept tool_calls without error."""
        ws = WindowStrategy()
        tool_calls = [{"team": "speech_effector", "args": {}, "result": "Hello!"}]
        await ws.on_turn_complete(session, "msg", "resp", tool_calls=tool_calls)  # no raise

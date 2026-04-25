"""Context strategies for assembling conversation history for LLM calls.

Two strategies are provided:

* :class:`WindowStrategy` — the existing behaviour: return the session's
  sliding-window history verbatim.
* :class:`CognitiveStrategy` — RAG-based cognitive context that curates a
  working-memory view of the conversation, detecting topic shifts and
  recalling relevant memories from a tiered vector store.
"""

import json
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from ..config.schema import CognitiveContextConfig
from ..events import (
    ContextAssembled,
    EventBus,
    MemoryRecalled,
    TopicShift,
)
from ..memory.embeddings import EmbeddingProvider
from ..memory.vector_store import MemoryTier, VectorStore, _cosine_similarity
from ..runtime.session import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ContextStrategy(ABC):
    """Strategy for assembling conversation context for LLM calls."""

    @abstractmethod
    async def build_context(
        self,
        session: Session,
        current_input: Optional[str],
    ) -> List[Dict[str, str]]:
        """Build the message history to inject before the current user message.

        Returns a list of message dicts (``role``/``content``) that the
        :class:`AgentRunner` inserts between the system prompt and the new
        user message.
        """

    @abstractmethod
    async def on_turn_complete(
        self,
        session: Session,
        user_message: str,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Hook called after each turn completes.

        Allows the strategy to update internal state, store embeddings, etc.

        ``tool_calls`` is an optional list of team/tool invocations that
        occurred during the turn.  Each entry is a dict with at least a
        ``"team"`` key and either a ``"result"`` or ``"error"`` key.  When
        provided, the data is woven into the stored turn summary so that RAG
        recall reflects what the system actually did, not just what it thought.
        """

    async def on_session_end(self, session: Session) -> None:
        """Hook called when a chat session ends.

        Allows the strategy to finalise state, promote memories to long-term
        storage, etc. Default implementation is a no-op.
        """


# ---------------------------------------------------------------------------
# Default: simple sliding window (existing behaviour)
# ---------------------------------------------------------------------------


class WindowStrategy(ContextStrategy):
    """Default strategy — wraps ``session.get_history()`` unchanged."""

    async def build_context(
        self,
        session: Session,
        current_input: Optional[str],
    ) -> List[Dict[str, str]]:
        return session.get_history()

    async def on_turn_complete(
        self,
        session: Session,
        user_message: str,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        pass  # Session.add_turn() already handles windowing


# ---------------------------------------------------------------------------
# Cognitive strategy internals
# ---------------------------------------------------------------------------


@dataclass
class TopicState:
    """Tracks the active conversational topic."""

    label: str
    embedding: List[float]
    started_at_turn: int
    key_facts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict for persistence."""
        return {
            "label": self.label,
            "embedding": self.embedding,
            "started_at_turn": self.started_at_turn,
            "key_facts": self.key_facts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TopicState":
        """Reconstruct from a persisted dict."""
        return cls(
            label=data["label"],
            embedding=data["embedding"],
            started_at_turn=data["started_at_turn"],
            key_facts=data.get("key_facts", []),
        )


def _derive_topic_label(text: str, max_words: int = 8) -> str:
    """Extract a short topic label from the first words of a message."""
    words = text.split()[:max_words]
    label = " ".join(words)
    if len(text.split()) > max_words:
        label += "..."
    return label


# ---------------------------------------------------------------------------
# Cognitive strategy
# ---------------------------------------------------------------------------


class CognitiveStrategy(ContextStrategy):
    """RAG-based cognitive context strategy.

    Instead of dumping the full sliding-window history into the LLM context,
    this strategy curates a *working memory* view:

    1. Always includes the most recent ``recent_turns`` raw turns.
    2. Detects topic shifts (via embedding similarity or LLM classification).
    3. On a shift, summarises the outgoing topic and stores it to medium-term
       memory in the vector store.
    4. Uses RAG recall to inject relevant memories from earlier in the session
       or from long-term (cross-session) storage.
    5. Injects annotations (system messages) to orient the LLM about context
       switches and recalled information.
    """

    def __init__(
        self,
        config: CognitiveContextConfig,
        vector_store: VectorStore,
        embedding_provider: EmbeddingProvider,
        event_bus: Optional[EventBus] = None,
        # Backend callable for LLM-based topic detection / summarisation.
        # Signature: async (model, messages, max_tokens) -> str
        llm_fn: Optional[Any] = None,
    ):
        self._config = config
        self._store = vector_store
        self._embedder = embedding_provider
        self._events = event_bus or EventBus()
        self._llm_fn = llm_fn

        self._current_topic: Optional[TopicState] = None
        # Internal turn buffer — never trimmed.  Stores raw turn pairs
        # independently of Session's windowed history so that we always have
        # access to recent raw messages for the working memory portion.
        self._turns: Deque[Dict[str, str]] = deque()
        self._turn_count: int = 0
        # Track which sessions have been rehydrated this process lifetime
        # to avoid redundant DB queries on every turn.
        self._rehydrated_sessions: set = set()

    # -----------------------------------------------------------------
    # Session rehydration
    # -----------------------------------------------------------------

    def _rehydrate_for_session(self, session: Session) -> None:
        """Recover in-memory state from persistent storage for a resumed session.

        Idempotent: no-ops if already called for this session.id within this
        process lifetime, avoiding redundant DB queries on every turn.

        Fixes two bugs that manifest when a Conductor is re-instantiated across
        process restarts while the same session.id is reused:

        1. **Key collision** — ``_turn_count`` resets to 0, so every resumed
           turn writes to ``turn:<sid>:1`` and the UPSERT silently overwrites
           all prior turns, leaving exactly 1 ``short_term`` row.
        2. **Empty working memory** — ``_turns`` deque starts empty, so
           ``_recent_turn_messages()`` returns ``[]`` and the LLM receives no
           conversational context for the first resumed message.
        """
        if session.id in self._rehydrated_sessions:
            return
        self._rehydrated_sessions.add(session.id)

        # Fix 1: recover _turn_count from the highest stored turn suffix.
        # Must use max key suffix (not a count) because active consolidation
        # promotes turns from short_term to long_term — a tier-filtered count
        # would understate the true maximum and cause key collisions on resume.
        stored_max = self._store.max_session_turn_number(session.id)
        if stored_max > self._turn_count:
            self._turn_count = stored_max
            logger.debug(
                "CognitiveStrategy: recovered _turn_count=%d for session %s",
                self._turn_count,
                session.id[:8],
            )

        # Fix 2: rehydrate _turns from session.message_history
        if not self._turns and session.message_history:
            for msg in session.message_history:
                self._turns.append(msg)
            logger.debug(
                "CognitiveStrategy: rehydrated %d messages into _turns for session %s",
                len(session.message_history),
                session.id[:8],
            )

        # Fix 3: recover _current_topic from session context
        if self._current_topic is None:
            topic_data = session.context.get("_cognitive_topic_state")
            if topic_data is not None:
                try:
                    self._current_topic = TopicState.from_dict(topic_data)
                    logger.debug(
                        "CognitiveStrategy: recovered topic '%s' (started at turn %d) for session %s",
                        self._current_topic.label,
                        self._current_topic.started_at_turn,
                        session.id[:8],
                    )
                except (KeyError, TypeError) as exc:
                    logger.warning(
                        "CognitiveStrategy: failed to restore topic state for session %s: %s",
                        session.id[:8],
                        exc,
                    )

    # -----------------------------------------------------------------
    # build_context
    # -----------------------------------------------------------------

    async def build_context(
        self,
        session: Session,
        current_input: Optional[str],
    ) -> List[Dict[str, str]]:
        self._rehydrate_for_session(session)
        messages: List[Dict[str, str]] = []

        if not current_input:
            # Nothing to embed — fall back to recent turns only
            return self._recent_turn_messages()

        # 1. Embed the current input
        input_embedding = await self._embedder.embed(current_input)

        # 2. Topic shift detection
        topic_shifted = False
        new_topic_label = _derive_topic_label(current_input)

        if self._current_topic is not None:
            topic_shifted = await self._detect_topic_shift(
                input_embedding, current_input
            )

        if topic_shifted and self._current_topic is not None:
            # Summarise and store the outgoing topic
            summary = await self._summarise_outgoing_topic()
            if summary:
                self._store.store(
                    key=f"topic:{self._current_topic.label}",
                    value=summary,
                    embedding=self._current_topic.embedding,
                    session_id=session.id,
                    tier=MemoryTier.MEDIUM_TERM.value,
                )

            # Inject a topic-shift annotation
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"[Context note: Conversation shifted from "
                        f"'{self._current_topic.label}' to a new topic. "
                        f"Prior topic summary stored for recall.]"
                    ),
                }
            )
            self._events.emit(
                TopicShift(
                    from_topic=self._current_topic.label,
                    to_topic=new_topic_label,
                )
            )

            # Start a new topic
            self._current_topic = TopicState(
                label=new_topic_label,
                embedding=input_embedding,
                started_at_turn=self._turn_count,
            )
        elif self._current_topic is None:
            # First turn — initialise topic
            self._current_topic = TopicState(
                label=new_topic_label,
                embedding=input_embedding,
                started_at_turn=self._turn_count,
            )

        # 3. RAG recall
        if self._config.max_recalled_memories > 0:
            recalled = self._store.search(
                query_embedding=input_embedding,
                limit=self._config.max_recalled_memories,
                session_id=session.id,
                threshold=self._config.similarity_threshold,
            )
            if recalled:
                recall_lines = [
                    f"- [{m.key}] {m.value}" for m in recalled
                ]
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "[Recalled context from earlier in this conversation:]\n"
                            + "\n".join(recall_lines)
                        ),
                    }
                )
                self._events.emit(
                    MemoryRecalled(
                        count=len(recalled),
                        memory_keys=",".join(m.key for m in recalled),
                    )
                )

        # 4. Append recent raw turns (always in working memory)
        recent = self._recent_turn_messages()
        messages.extend(recent)

        # 5. Emit assembly event
        topic_label = self._current_topic.label if self._current_topic else ""
        self._events.emit(
            ContextAssembled(
                total_messages=len(messages),
                recalled_memories=len(recalled) if self._config.max_recalled_memories > 0 and recalled else 0,
                recent_turns=len(recent) // 2,
                topic=topic_label,
            )
        )

        return messages

    # -----------------------------------------------------------------
    # on_turn_complete
    # -----------------------------------------------------------------

    async def on_turn_complete(
        self,
        session: Session,
        user_message: str,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._rehydrate_for_session(session)

        # Build enriched assistant content for the working-memory buffer so
        # that recent turns show what actions were taken, not just reasoning.
        enriched_assistant = self._enrich_assistant_content(
            assistant_response, tool_calls
        )

        # Store turn in internal buffer (enriched view)
        self._turns.append({"role": "user", "content": user_message})
        self._turns.append({"role": "assistant", "content": enriched_assistant})
        self._turn_count += 1

        # Embed and store in vector store for future RAG recall
        embedding = await self._embedder.embed(user_message)
        turn_summary = self._compose_turn_summary(
            user_message, assistant_response, tool_calls
        )
        self._store.store(
            key=f"turn:{session.id}:{self._turn_count}",
            value=turn_summary,
            embedding=embedding,
            session_id=session.id,
            tier=MemoryTier.SHORT_TERM.value,
        )

        # Update current topic embedding (running average with new input)
        if self._current_topic is not None:
            # Blend current topic embedding towards the new message
            alpha = 0.3
            self._current_topic.embedding = [
                (1 - alpha) * old + alpha * new
                for old, new in zip(self._current_topic.embedding, embedding)
            ]

        # Persist current topic state for cross-restart recovery
        if self._current_topic is not None:
            session.context["_cognitive_topic_state"] = self._current_topic.to_dict()

        # Active consolidation: promote important turns to long-term immediately
        if self._should_consolidate(user_message, assistant_response, embedding):
            self._store.promote(
                f"turn:{session.id}:{self._turn_count}",
                MemoryTier.LONG_TERM.value,
            )
            logger.debug(
                "CognitiveStrategy: consolidated turn %d to long-term for session %s",
                self._turn_count,
                session.id[:8],
            )

    # -----------------------------------------------------------------
    # on_session_end
    # -----------------------------------------------------------------

    async def on_session_end(self, session: Session) -> None:
        """Promote medium-term topic summaries to long-term on session close.

        Ensures the final topic (which may not have triggered a shift) is
        summarised and stored, then promotes all medium-term entries for
        this session to long-term so they are available for future recall.
        """
        self._rehydrate_for_session(session)

        # Summarise the current (final) topic if it has not been stored yet
        if self._current_topic is not None:
            summary = await self._summarise_outgoing_topic()
            if summary:
                self._store.store(
                    key=f"topic:{self._current_topic.label}",
                    value=summary,
                    embedding=self._current_topic.embedding,
                    session_id=session.id,
                    tier=MemoryTier.MEDIUM_TERM.value,
                )

        # Promote all medium-term entries to long-term
        medium_entries = self._store.get_entries_by_tier(
            session_id=session.id,
            tier=MemoryTier.MEDIUM_TERM.value,
        )
        for entry in medium_entries:
            self._store.promote(entry.key, MemoryTier.LONG_TERM.value)

        if medium_entries:
            logger.info(
                "CognitiveStrategy: promoted %d medium-term memories to long-term for session %s",
                len(medium_entries),
                session.id[:8],
            )

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _compose_turn_summary(
        self,
        user_message: str,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> str:
        """Build the text stored in the vector store for a completed turn.

        When tool_calls are present, each invocation is rendered as an
        ``Action:`` line so that RAG recall reflects what the system did.
        """
        parts = [f"User: {user_message}"]

        if tool_calls:
            for tc in tool_calls:
                team = tc.get("team", tc.get("name", "unknown"))
                args = tc.get("args", {})
                args_summary = "; ".join(
                    f"{k}: {str(v)[:100]}" for k, v in args.items()
                )
                inputs_str = f" (inputs: {args_summary})" if args_summary else ""
                if "result" in tc:
                    result_preview = str(tc["result"])[:500]
                    parts.append(f"Action performed: {team}{inputs_str} → {result_preview}")
                elif "error" in tc:
                    parts.append(
                        f"Action performed: {team}{inputs_str} → ERROR: {tc['error']}"
                    )

        if assistant_response.strip():
            label = "Internal" if tool_calls else "Assistant"
            parts.append(f"{label}: {assistant_response}")

        return "\n".join(parts)

    def _enrich_assistant_content(
        self,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]],
    ) -> str:
        """Build the assistant content stored in the recent-turns buffer.

        When actions were taken, they are surfaced as log-style summaries
        above the raw internal reasoning so the LLM's working memory shows
        what actually happened during the turn.

        NOTE: The format here intentionally reads as a historical log, NOT as
        a callable syntax. Using [team: outcome] was found to cause models to
        reproduce that surface form as prose instead of emitting real tool_use
        blocks. The arrow format below is unambiguously a past event record.
        """
        if not tool_calls:
            return assistant_response

        action_lines = []
        for tc in tool_calls:
            team = tc.get("team", tc.get("name", "unknown"))
            outcome = tc.get("result", tc.get("error", ""))
            action_lines.append(f"(prior action) {team} → {str(outcome)[:200]}")

        prefix = "\n".join(action_lines)
        if assistant_response.strip():
            return f"{prefix}\n{assistant_response}"
        return prefix

    def _should_consolidate(
        self,
        user_message: str,
        assistant_response: str,
        embedding: List[float],
    ) -> bool:
        """Decide whether a turn is important enough to promote to long-term immediately.

        Runs on every turn — no LLM call, must be fast.

        Rules (all use configurable thresholds from CognitiveContextConfig):
        1. Gate: skip if combined length < consolidation_min_length (trivial exchanges)
        2. Novelty: promote if similarity to current topic < consolidation_novelty_threshold
        3. Length: promote if combined length > consolidation_length_threshold
        """
        combined_length = len(user_message) + len(assistant_response)
        if combined_length < self._config.consolidation_min_length:
            return False

        if self._current_topic is not None:
            similarity = _cosine_similarity(embedding, self._current_topic.embedding)
            if similarity < self._config.consolidation_novelty_threshold:
                return True

        return combined_length > self._config.consolidation_length_threshold

    def _recent_turn_messages(self) -> List[Dict[str, str]]:
        """Return the last ``recent_turns`` turn pairs from the internal buffer."""
        count = self._config.recent_turns * 2  # user + assistant per turn
        if len(self._turns) <= count:
            return list(self._turns)
        return list(self._turns)[-count:]

    async def _detect_topic_shift(
        self,
        input_embedding: List[float],
        input_text: str,
    ) -> bool:
        """Detect whether the current input represents a topic shift."""
        if self._current_topic is None:
            return False

        if self._config.topic_detection == "llm" and self._llm_fn is not None:
            return await self._detect_topic_shift_llm(input_text)

        # Default: embedding similarity
        return self._detect_topic_shift_embedding(input_embedding)

    def _detect_topic_shift_embedding(self, input_embedding: List[float]) -> bool:
        """Embedding-based topic shift detection using cosine similarity."""
        if self._current_topic is None:
            return False

        similarity = _cosine_similarity(
            input_embedding, self._current_topic.embedding
        )
        return similarity < self._config.similarity_threshold

    async def _detect_topic_shift_llm(self, input_text: str) -> bool:
        """LLM-based topic shift detection."""
        if self._llm_fn is None or self._current_topic is None:
            return False

        model = self._config.summary_model or "anthropic/claude-sonnet-4-6"
        prompt = (
            f'The current conversation topic is: "{self._current_topic.label}"\n'
            f'The user\'s new message is: "{input_text[:2000]}"\n\n'
            f"Has the topic changed? Reply with ONLY a JSON object: "
            f'{{"shifted": true/false, "new_topic": "short label"}}'
        )
        try:
            result = await self._llm_fn(
                model,
                [{"role": "user", "content": prompt}],
                50,
            )
            parsed = json.loads(result)
            if parsed.get("shifted"):
                # Update the topic label from the LLM's response
                if self._current_topic and parsed.get("new_topic"):
                    pass  # label will be set by build_context
                return True
            return False
        except Exception:
            logger.debug("LLM topic detection failed, falling back to embedding", exc_info=True)
            return False

    async def _summarise_outgoing_topic(self) -> Optional[str]:
        """Summarise the outgoing topic's turns for medium-term storage."""
        if self._current_topic is None:
            return None

        # Gather turns from when this topic started
        start_idx = self._current_topic.started_at_turn * 2
        topic_turns = list(self._turns)[start_idx:]

        if not topic_turns:
            return None

        # If we have an LLM function, use it for summarisation
        if self._llm_fn is not None:
            model = self._config.summary_model or "anthropic/claude-sonnet-4-6"
            turns_text = "\n".join(
                f"{t['role'].title()}: {t['content']}" for t in topic_turns
            )
            prompt = (
                f"Summarise the following conversation segment about "
                f"'{self._current_topic.label}' in 2-3 sentences. "
                f"Focus on key facts, decisions, and conclusions.\n\n{turns_text}"
            )
            try:
                return await self._llm_fn(
                    model,
                    [{"role": "user", "content": prompt}],
                    1000,
                )
            except Exception:
                logger.warning("LLM summarisation failed", exc_info=True)

        # Fallback: concatenate first/last turns as a basic summary
        parts = []
        first_t = topic_turns[0]
        last_t = topic_turns[-1]
        parts.append(f"{first_t['role'].title()}: {first_t['content'][:3000]}")
        parts.append(f"{last_t['role'].title()}: {last_t['content'][:3000]}")
        return " | ".join(parts)

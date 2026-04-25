"""Tests for streaming backend support and AgentToken events."""

import asyncio
import tempfile
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from elasticity.backends.base import Backend, CompletionResponse, StreamChunk, ToolCall
from elasticity.events import EventBus, AgentToken, AgentCompleted


# ---------------------------------------------------------------------------
# StreamChunk defaults
# ---------------------------------------------------------------------------


def test_stream_chunk_defaults():
    chunk = StreamChunk()
    assert chunk.delta == ""
    assert chunk.tool_call is None
    assert chunk.done is False


def test_stream_chunk_done_flag():
    chunk = StreamChunk(done=True)
    assert chunk.done is True


def test_stream_chunk_with_tool_call():
    tc = ToolCall(id="t1", name="search", arguments={"q": "hello"})
    chunk = StreamChunk(tool_call=tc)
    assert chunk.tool_call is tc
    assert chunk.delta == ""


# ---------------------------------------------------------------------------
# Backend default stream() falls back to complete()
# ---------------------------------------------------------------------------


class MinimalBackend(Backend):
    """Concrete backend using only the default stream() fallback."""

    def __init__(self, content="hello world", tool_calls=None):
        self._content = content
        self._tool_calls = tool_calls or []

    async def complete(self, model, messages, tools=None, max_tokens=4096, response_format=None):
        return CompletionResponse(content=self._content, tool_calls=self._tool_calls)


@pytest.mark.asyncio
async def test_default_stream_fallback_yields_content():
    backend = MinimalBackend(content="hello world")
    chunks = []
    async for chunk in backend.stream("m", []):
        chunks.append(chunk)

    text_chunks = [c for c in chunks if c.delta]
    done_chunks = [c for c in chunks if c.done]
    assert any("hello world" in c.delta for c in text_chunks)
    assert len(done_chunks) == 1


@pytest.mark.asyncio
async def test_default_stream_fallback_yields_tool_calls():
    tc = ToolCall(id="t1", name="search", arguments={"q": "test"})
    backend = MinimalBackend(content="", tool_calls=[tc])
    chunks = []
    async for chunk in backend.stream("m", []):
        chunks.append(chunk)

    tool_chunks = [c for c in chunks if c.tool_call]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_call.name == "search"


# ---------------------------------------------------------------------------
# AgentRunner streaming path emits AgentToken events
# ---------------------------------------------------------------------------


def _simple_config_data():
    return {
        "agent_types": {
            "writer": {
                "model": "openai/gpt-4o",
                "system_prompt": "You are a writer.",
            }
        },
        "tools": {},
        "orchestrations": {
            "write": {
                "input": {"topic": "string"},
                "flow": [
                    {"agent": "writer", "input": "Write about {topic}", "output_as": "essay"}
                ],
            }
        },
    }


def _streaming_mock_backend(tokens: list[str]):
    """Build a mock backend whose stream() yields individual tokens."""

    async def fake_stream(model, messages, tools=None, max_tokens=4096, response_format=None):
        for token in tokens:
            yield StreamChunk(delta=token)
        yield StreamChunk(done=True)

    mock = MagicMock()
    mock.stream = fake_stream
    mock.complete = AsyncMock(
        return_value=CompletionResponse(content="".join(tokens), tool_calls=[])
    )
    return mock


def test_agent_runner_streaming_emits_token_events():
    """AgentRunner with stream_responses=True emits AgentToken events per chunk."""
    from elasticity import Orchestration

    tokens_received = []
    bus = EventBus()
    bus.subscribe(AgentToken, lambda e: tokens_received.append(e.token))

    mock_backend = _streaming_mock_backend(["Hello", " world", "!"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_data(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(
                orch.run(
                    "write",
                    input_data={"topic": "AI"},
                    event_bus=bus,
                    stream_responses=True,
                )
            )

        assert tokens_received == ["Hello", " world", "!"]
    finally:
        Path(config_path).unlink()


def test_agent_runner_streaming_assembles_final_output():
    """The final AgentCompleted event should carry the fully assembled text."""
    from elasticity import Orchestration

    completed_events = []
    bus = EventBus()
    bus.subscribe(AgentCompleted, completed_events.append)

    mock_backend = _streaming_mock_backend(["Hello", " ", "world"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_data(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(
                orch.run(
                    "write",
                    input_data={"topic": "AI"},
                    event_bus=bus,
                    stream_responses=True,
                )
            )

        assert len(completed_events) == 1
        assert completed_events[0].output == "Hello world"
    finally:
        Path(config_path).unlink()


def test_agent_runner_non_streaming_no_token_events():
    """With stream_responses=False (default) no AgentToken events should fire."""
    from elasticity import Orchestration
    from elasticity.backends.base import CompletionResponse

    token_events = []
    bus = EventBus()
    bus.subscribe(AgentToken, token_events.append)

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        return CompletionResponse(content="Result", tool_calls=[])

    mock = MagicMock()
    mock.complete = fake_complete

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_data(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock, "gpt-4o")):
            asyncio.run(
                orch.run(
                    "write",
                    input_data={"topic": "AI"},
                    event_bus=bus,
                    stream_responses=False,
                )
            )

        assert token_events == []
    finally:
        Path(config_path).unlink()


def test_streaming_with_tool_call_executes_tool():
    """Streaming path must still execute tool calls accumulated from chunks."""
    from elasticity.runtime.agent import AgentRunner
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.runtime.context import ContextManager
    from elasticity.config.schema import AgentTypeDefinition, ToolDefinition, ParameterSchema

    tool_results = []

    def mock_tool(query: str) -> str:
        tool_results.append(query)
        return f"Result for {query}"

    registry = ToolRegistry()
    from elasticity.config.schema import ToolDefinition, ParameterSchema
    registry.register(
        "search",
        ToolDefinition(
            description="Search the web",
            callable="builtins.str",  # placeholder, will be overridden
            parameters={"query": ParameterSchema(type="string")},
        ),
    )
    registry._callables["search"] = mock_tool

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You search things.",
        tools=["search"],
        max_tool_rounds=2,
    )

    tc = ToolCall(id="tc1", name="search", arguments={"query": "elasticity"})

    round_counter = [0]

    async def fake_stream(model, messages, tools=None, max_tokens=4096, response_format=None):
        round_counter[0] += 1
        if round_counter[0] == 1:
            yield StreamChunk(tool_call=tc)
            yield StreamChunk(done=True)
        else:
            yield StreamChunk(delta="Final answer.")
            yield StreamChunk(done=True)

    mock = MagicMock()
    mock.stream = fake_stream

    runner = AgentRunner(registry)
    runner.stream_responses = True
    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock, "gpt-4o")):
        result = asyncio.run(
            runner.run(agent_type, "searcher", "Search elasticity", context)
        )

    assert "elasticity" in tool_results
    assert result["content"] == "Final answer."


# ---------------------------------------------------------------------------
# Truncation detection and recovery
# ---------------------------------------------------------------------------


def test_stream_chunk_truncated_default():
    """StreamChunk.truncated defaults to False."""
    assert StreamChunk().truncated is False
    assert StreamChunk(done=True).truncated is False


def test_stream_chunk_truncated_flag():
    """StreamChunk.truncated can be set on the done chunk."""
    chunk = StreamChunk(done=True, truncated=True)
    assert chunk.truncated is True


def test_completion_response_stop_reason_default():
    """CompletionResponse.stop_reason defaults to None."""
    resp = CompletionResponse(content="hello", tool_calls=[])
    assert resp.stop_reason is None


def test_completion_response_stop_reason():
    """CompletionResponse.stop_reason stores the provided value."""
    resp = CompletionResponse(content="hello", tool_calls=[], stop_reason="max_tokens")
    assert resp.stop_reason == "max_tokens"


def test_default_stream_fallback_truncated_propagates():
    """Backend.stream() fallback propagates truncated=True when stop_reason is max_tokens."""

    class TruncatedBackend(Backend):
        async def complete(self, model, messages, tools=None, max_tokens=4096, response_format=None):
            tc = ToolCall(id="t1", name="file_write", arguments={})
            return CompletionResponse(content="", tool_calls=[tc], stop_reason="max_tokens")

    async def _run():
        chunks = []
        async for chunk in TruncatedBackend().stream("m", []):
            chunks.append(chunk)
        return chunks

    import asyncio
    chunks = asyncio.run(_run())
    done_chunks = [c for c in chunks if c.done]
    assert len(done_chunks) == 1
    assert done_chunks[0].truncated is True


def test_streaming_truncation_does_not_execute_tool():
    """When stream ends with truncated=True, broken tool calls are NOT executed."""
    from elasticity.runtime.agent import AgentRunner
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.runtime.context import ContextManager
    from elasticity.config.schema import AgentTypeDefinition, ToolDefinition, ParameterSchema
    from unittest.mock import patch

    tool_calls_made = []

    def mock_file_write(path: str, content: str) -> str:
        tool_calls_made.append((path, content))
        return "ok"

    registry = ToolRegistry()
    registry.register(
        "file_write",
        ToolDefinition(
            description="Write a file",
            callable="builtins.str",
            parameters={
                "path": ParameterSchema(type="string"),
                "content": ParameterSchema(type="string"),
            },
        ),
    )
    registry._callables["file_write"] = mock_file_write

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You write files.",
        tools=["file_write"],
        max_tool_rounds=3,
    )

    # Round 1: truncated response with an incomplete tool call
    # Round 2: successful response (no tool call, just text)
    round_counter = [0]

    async def fake_stream(model, messages, tools=None, max_tokens=4096, response_format=None):
        round_counter[0] += 1
        if round_counter[0] == 1:
            # Truncated: backend skips the broken tool call and sets truncated=True
            yield StreamChunk(done=True, truncated=True)
        else:
            yield StreamChunk(delta="Done.")
            yield StreamChunk(done=True)

    mock = MagicMock()
    mock.stream = fake_stream

    runner = AgentRunner(registry)
    runner.stream_responses = True
    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock, "gpt-4o")):
        result = asyncio.run(runner.run(agent_type, "writer", "Write a file", context))

    # The broken tool call must not have been executed
    assert tool_calls_made == []
    # The runner should have proceeded to round 2 and returned its content
    assert result["content"] == "Done."
    assert round_counter[0] == 2


def test_streaming_truncation_injects_recovery_message():
    """When truncated, the agent runner injects a recovery message before retrying."""
    from elasticity.runtime.agent import AgentRunner
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.runtime.context import ContextManager
    from elasticity.config.schema import AgentTypeDefinition
    from unittest.mock import patch

    messages_seen_in_round2 = []
    round_counter = [0]

    async def fake_stream(model, messages, tools=None, max_tokens=4096, response_format=None):
        round_counter[0] += 1
        if round_counter[0] == 1:
            yield StreamChunk(delta="I'll write the file now.")
            yield StreamChunk(done=True, truncated=True)
        else:
            messages_seen_in_round2.extend(messages)
            yield StreamChunk(delta="OK, writing shorter content.")
            yield StreamChunk(done=True)

    mock = MagicMock()
    mock.stream = fake_stream

    runner = AgentRunner(ToolRegistry())
    runner.stream_responses = True
    context = ContextManager("message_passing")

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You write files.",
        max_tool_rounds=3,
    )

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock, "gpt-4o")):
        asyncio.run(runner.run(agent_type, "writer", "Write a big file", context))

    # The last two messages injected before round 2 should be the assistant text
    # and the recovery user message.
    roles = [m["role"] for m in messages_seen_in_round2[-2:]]
    assert roles == ["assistant", "user"]
    recovery_msg = messages_seen_in_round2[-1]["content"]
    assert "truncated" in recovery_msg.lower()


def test_complete_truncation_does_not_execute_tool():
    """Non-streaming path: when stop_reason is max_tokens, tool calls are not executed."""
    from elasticity.runtime.agent import AgentRunner
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.runtime.context import ContextManager
    from elasticity.config.schema import AgentTypeDefinition, ToolDefinition, ParameterSchema
    from unittest.mock import patch, AsyncMock

    tool_calls_made = []

    def mock_tool(path: str, content: str) -> str:
        tool_calls_made.append((path, content))
        return "ok"

    registry = ToolRegistry()
    registry.register(
        "file_write",
        ToolDefinition(
            description="Write a file",
            callable="builtins.str",
            parameters={
                "path": ParameterSchema(type="string"),
                "content": ParameterSchema(type="string"),
            },
        ),
    )
    registry._callables["file_write"] = mock_tool

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You write files.",
        tools=["file_write"],
        max_tool_rounds=3,
    )

    truncated_tc = ToolCall(id="t1", name="file_write", arguments={})
    round_counter = [0]

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        round_counter[0] += 1
        if round_counter[0] == 1:
            return CompletionResponse(
                content="Let me write that file.",
                tool_calls=[truncated_tc],
                stop_reason="max_tokens",
            )
        return CompletionResponse(content="Done.", tool_calls=[])

    mock = MagicMock()
    mock.complete = fake_complete

    runner = AgentRunner(registry)
    runner.stream_responses = False
    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock, "gpt-4o")):
        result = asyncio.run(runner.run(agent_type, "writer", "Write a big file", context))

    assert tool_calls_made == []
    assert result["content"] == "Done."
    assert round_counter[0] == 2

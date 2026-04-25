"""Tests for the EventBus and event system."""

import asyncio
import tempfile
import yaml
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from elasticity.events import (
    Event,
    EventBus,
    AgentStarted,
    AgentCompleted,
    AgentErrorEvent,
    AgentToken,
    ToolCalled,
    ToolResult,
    NodeStarted,
    NodeCompleted,
    OrchestrationStarted,
    OrchestrationCompleted,
    LoopIteration,
    RouteTaken,
    ParallelStarted,
    ParallelCompleted,
    SpawnStarted,
    SpawnCompleted,
    SupervisorWorkerStarted,
    SupervisorReview,
    SupervisorAccepted,
    SupervisorRejected,
)
from elasticity.tracing import RunTrace
from elasticity.backends.base import CompletionResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _simple_config_yaml():
    return {
        "agent_types": {
            "worker": {
                "model": "openai/gpt-4o",
                "system_prompt": "You are a helpful worker.",
            }
        },
        "tools": {},
        "orchestrations": {
            "simple": {
                "input": {"topic": "string"},
                "flow": [
                    {"agent": "worker", "input": "Work on {topic}", "output_as": "result"}
                ],
            }
        },
    }


def _make_mock_backend(response_content="Done."):
    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        return CompletionResponse(content=response_content, tool_calls=[])

    mock = MagicMock()
    mock.complete = fake_complete
    return mock


# ---------------------------------------------------------------------------
# EventBus subscribe / emit
# ---------------------------------------------------------------------------


def test_event_bus_basic_subscribe_and_emit():
    bus = EventBus()
    received = []
    bus.subscribe(AgentStarted, received.append)

    event = AgentStarted(agent_name="researcher", step_id="step_1")
    bus.emit(event)

    assert len(received) == 1
    assert received[0] is event


def test_event_bus_does_not_deliver_to_wrong_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe(AgentCompleted, received.append)

    bus.emit(AgentStarted(agent_name="researcher", step_id="step_1"))

    assert received == []


def test_event_bus_base_class_subscription_receives_all_events():
    """Subscribing to Event base class should receive every event type."""
    bus = EventBus()
    received = []
    bus.subscribe(Event, received.append)

    bus.emit(AgentStarted(agent_name="a", step_id="s1"))
    bus.emit(ToolCalled(agent_name="a", tool_name="search", arguments={}))
    bus.emit(OrchestrationCompleted(run_id="r1", orchestration_name="test"))

    assert len(received) == 3


def test_event_bus_multiple_subscribers_same_type():
    bus = EventBus()
    received_a = []
    received_b = []
    bus.subscribe(AgentStarted, received_a.append)
    bus.subscribe(AgentStarted, received_b.append)

    bus.emit(AgentStarted(agent_name="x", step_id="s"))

    assert len(received_a) == 1
    assert len(received_b) == 1


def test_event_bus_subscriber_exception_is_swallowed():
    """A crashing subscriber must not propagate to the caller."""
    bus = EventBus()

    def bad_subscriber(event):
        raise RuntimeError("boom")

    good_received = []
    bus.subscribe(AgentStarted, bad_subscriber)
    bus.subscribe(AgentStarted, good_received.append)

    # Should not raise
    bus.emit(AgentStarted(agent_name="a", step_id="s"))

    # Good subscriber should still have received the event
    assert len(good_received) == 1


def test_event_bus_no_subscribers_is_safe():
    bus = EventBus()
    bus.emit(AgentStarted(agent_name="a", step_id="s"))  # must not raise


# ---------------------------------------------------------------------------
# Event dataclass defaults
# ---------------------------------------------------------------------------


def test_event_has_timestamp():
    e = AgentStarted(agent_name="a", step_id="s")
    assert isinstance(e.timestamp, float)
    assert e.timestamp > 0


def test_tool_called_default_arguments():
    e = ToolCalled(agent_name="a", tool_name="t")
    assert e.arguments == {}


def test_supervisor_rejected_optional_feedback():
    e = SupervisorRejected(supervisor="s", worker_id="w", attempt=1)
    assert e.feedback is None

    e2 = SupervisorRejected(supervisor="s", worker_id="w", attempt=1, feedback="try harder")
    assert e2.feedback == "try harder"


# ---------------------------------------------------------------------------
# RunTrace as event subscriber
# ---------------------------------------------------------------------------


def test_run_trace_subscribe_to_records_events():
    bus = EventBus()
    trace = RunTrace(run_id="r1", orchestration_name="test_orch", log_to_console=False)
    trace.subscribe_to(bus)

    bus.emit(AgentStarted(agent_name="researcher", step_id="step_1"))
    bus.emit(AgentCompleted(agent_name="researcher", step_id="step_1", output="done", duration_ms=100.0))

    assert len(trace.events) == 2
    assert trace.events[0]["type"] == "AgentStarted"
    assert trace.events[1]["type"] == "AgentCompleted"


def test_run_trace_to_dict_structure():
    bus = EventBus()
    trace = RunTrace(run_id="run-123", orchestration_name="my_orch", log_to_console=False)
    trace.subscribe_to(bus)

    bus.emit(NodeStarted(step_id="n1", node_type="agent"))
    trace.complete()

    d = trace.to_dict()
    assert d["run_id"] == "run-123"
    assert d["orchestration_name"] == "my_orch"
    assert d["started_at"] is not None
    assert d["completed_at"] is not None
    assert len(d["events"]) == 1


def test_run_trace_legacy_add_event_still_works():
    """Direct add_event() calls (legacy) still populate the events list."""
    trace = RunTrace(run_id="r", orchestration_name="o", log_to_console=False)
    trace.add_event("agent_start", step_id="s1", agent_name="researcher")

    assert len(trace.events) == 1
    assert trace.events[0]["type"] == "agent_start"
    assert trace.events[0]["agent_name"] == "researcher"


# ---------------------------------------------------------------------------
# Executor emits events (integration, using mock backend)
# ---------------------------------------------------------------------------


def test_executor_emits_agent_started_and_completed():
    """Executor should emit AgentStarted and AgentCompleted events."""
    from elasticity import Orchestration

    received = []
    bus = EventBus()
    bus.subscribe(AgentStarted, received.append)
    bus.subscribe(AgentCompleted, received.append)

    mock_backend = _make_mock_backend("Result from worker.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_yaml(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(orch.run("simple", input_data={"topic": "test"}, event_bus=bus))

        started = [e for e in received if isinstance(e, AgentStarted)]
        completed = [e for e in received if isinstance(e, AgentCompleted)]
        assert len(started) >= 1
        assert len(completed) >= 1
        assert started[0].agent_name == "worker"
        assert completed[0].agent_name == "worker"
    finally:
        Path(config_path).unlink()


def test_executor_emits_orchestration_lifecycle():
    """OrchestrationStarted and OrchestrationCompleted should be emitted."""
    from elasticity import Orchestration

    received = []
    bus = EventBus()
    bus.subscribe(OrchestrationStarted, received.append)
    bus.subscribe(OrchestrationCompleted, received.append)

    mock_backend = _make_mock_backend("Done.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_yaml(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(orch.run("simple", input_data={"topic": "test"}, event_bus=bus))

        assert any(isinstance(e, OrchestrationStarted) for e in received)
        completed = [e for e in received if isinstance(e, OrchestrationCompleted)]
        assert len(completed) >= 1
        assert completed[0].duration_ms >= 0.0
    finally:
        Path(config_path).unlink()


def test_agent_completed_has_output_and_duration():
    """AgentCompleted event should carry the agent's output and a non-negative duration."""
    from elasticity import Orchestration

    received = []
    bus = EventBus()
    bus.subscribe(AgentCompleted, received.append)

    mock_backend = _make_mock_backend("Agent output text.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_yaml(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(orch.run("simple", input_data={"topic": "test"}, event_bus=bus))

        assert len(received) == 1
        assert received[0].output == "Agent output text."
        assert received[0].duration_ms >= 0.0
    finally:
        Path(config_path).unlink()


def test_run_trace_receives_events_via_orchestration():
    """Passing a RunTrace to Orchestration.run() should populate it via events."""
    from elasticity import Orchestration

    trace = RunTrace(run_id="test", orchestration_name="simple", log_to_console=False)
    mock_backend = _make_mock_backend("Output.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(_simple_config_yaml(), f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(orch.run("simple", input_data={"topic": "test"}, trace=trace))

        assert len(trace.events) > 0
        event_types = {e["type"] for e in trace.events}
        assert "AgentStarted" in event_types
        assert "AgentCompleted" in event_types
    finally:
        Path(config_path).unlink()


def test_event_bus_parallel_emits_started_and_completed():
    """ParallelStarted and ParallelCompleted should fire for parallel flow steps."""
    from elasticity import Orchestration

    config_data = {
        "agent_types": {
            "worker_a": {"model": "openai/gpt-4o", "system_prompt": "You are A."},
            "worker_b": {"model": "openai/gpt-4o", "system_prompt": "You are B."},
        },
        "tools": {},
        "orchestrations": {
            "parallel_test": {
                "input": {"topic": "string"},
                "flow": [
                    {
                        "parallel": [
                            {"agent": "worker_a", "input": "{topic}", "output_as": "out_a"},
                            {"agent": "worker_b", "input": "{topic}", "output_as": "out_b"},
                        ]
                    }
                ],
            }
        },
    }

    received = []
    bus = EventBus()
    bus.subscribe(ParallelStarted, received.append)
    bus.subscribe(ParallelCompleted, received.append)

    mock_backend = _make_mock_backend("Done.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = f.name

    try:
        orch = Orchestration.from_file(config_path)
        with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
            asyncio.run(orch.run("parallel_test", input_data={"topic": "test"}, event_bus=bus))

        assert any(isinstance(e, ParallelStarted) for e in received)
        assert any(isinstance(e, ParallelCompleted) for e in received)
        started = next(e for e in received if isinstance(e, ParallelStarted))
        assert started.branch_count == 2
    finally:
        Path(config_path).unlink()

"""Tests for the tool_call flow step type."""

import pytest
from unittest.mock import AsyncMock

from elasticity.config.schema import (
    Config,
    AgentTypeDefinition,
    OrchestrationDefinition,
    ToolCallConfig,
    ToolCallEntry,
    ToolCallStep,
    ToolDefinition,
    ParameterSchema,
)
from elasticity.config.validator import validate_references
from elasticity.compiler.graph import GraphBuilder, NodeType
from elasticity.runtime.context import ContextManager
from elasticity.runtime.executor import Executor
from elasticity.runtime.tools import ToolRegistry
from elasticity.events import EventBus, ToolCallStepStarted, ToolCallStepCompleted
from elasticity.errors import ExecutionError, ConfigReferenceError


# ---------------------------------------------------------------------------
# Schema parsing tests
# ---------------------------------------------------------------------------


def test_tool_call_config_single_tool():
    """Single-tool shorthand parses correctly."""
    tc = ToolCallConfig(tool="http_request", parameters={"url": "http://example.com"})
    assert tc.tool == "http_request"
    assert tc.parameters == {"url": "http://example.com"}
    assert tc.calls is None


def test_tool_call_config_multi_tool():
    """Multi-tool calls list parses correctly."""
    tc = ToolCallConfig(calls=[
        ToolCallEntry(tool="memory_store", parameters={"key": "k", "value": "v"}),
        ToolCallEntry(tool="file_read", parameters={"path": "/tmp/f"}, output_as="content"),
    ])
    assert tc.tool is None
    assert len(tc.calls) == 2
    assert tc.calls[1].output_as == "content"


def test_tool_call_config_rejects_both_tool_and_calls():
    """Cannot specify both 'tool' and 'calls'."""
    with pytest.raises(ValueError, match="cannot specify both"):
        ToolCallConfig(
            tool="http_request",
            calls=[ToolCallEntry(tool="file_read")],
        )


def test_tool_call_config_rejects_neither_tool_nor_calls():
    """Must specify either 'tool' or 'calls'."""
    with pytest.raises(ValueError, match="must specify either"):
        ToolCallConfig()


def test_tool_call_step_wraps_config():
    """ToolCallStep wraps ToolCallConfig."""
    step = ToolCallStep(tool_call=ToolCallConfig(tool="shell", parameters={"command": "ls"}))
    assert step.tool_call.tool == "shell"


def test_tool_call_config_on_error_default():
    """on_error defaults to 'fail'."""
    tc = ToolCallConfig(tool="shell")
    assert tc.on_error == "fail"


def test_tool_call_config_on_error_skip():
    """on_error can be set to 'skip'."""
    tc = ToolCallConfig(tool="shell", on_error="skip")
    assert tc.on_error == "skip"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


def _make_config_with_tool_call(tool_call_config: dict) -> Config:
    """Helper to build a Config with a tool_call step."""
    return Config(
        agent_types={
            "dummy": AgentTypeDefinition(model="openai/gpt-4o", system_prompt="test"),
        },
        tools={
            "my_tool": ToolDefinition(
                description="test tool",
                callable="builtins.print",
                parameters={"msg": ParameterSchema(type="string", required=True)},
            ),
        },
        orchestrations={
            "test": OrchestrationDefinition(
                flow=[ToolCallStep(tool_call=ToolCallConfig.model_validate(tool_call_config))]
            ),
        },
    )


def test_validate_tool_call_valid_tool():
    """Validation passes when tool exists."""
    config = _make_config_with_tool_call({"tool": "my_tool"})
    validate_references(config)  # should not raise


def test_validate_tool_call_undefined_tool():
    """Validation fails when tool does not exist."""
    config = _make_config_with_tool_call({"tool": "nonexistent"})
    with pytest.raises(ConfigReferenceError):
        validate_references(config)


def test_validate_tool_call_multi_undefined():
    """Validation fails when any tool in calls list is undefined."""
    config = _make_config_with_tool_call({
        "calls": [
            {"tool": "my_tool", "parameters": {}},
            {"tool": "missing_tool", "parameters": {}},
        ]
    })
    with pytest.raises(ConfigReferenceError):
        validate_references(config)


# ---------------------------------------------------------------------------
# Graph compilation tests
# ---------------------------------------------------------------------------


def _make_graph_config(steps):
    """Helper to build a Config for graph compilation."""
    return Config(
        agent_types={
            "worker": AgentTypeDefinition(model="openai/gpt-4o", system_prompt="test"),
        },
        tools={
            "my_tool": ToolDefinition(
                description="test",
                callable="builtins.print",
                parameters={},
            ),
        },
        orchestrations={
            "test": OrchestrationDefinition(flow=steps),
        },
    )


def test_graph_build_tool_call_single():
    """Single-tool tool_call compiles to TOOL_CALL node."""
    config = _make_graph_config([
        ToolCallStep(tool_call=ToolCallConfig(tool="my_tool", parameters={"x": "1"}, output_as="res")),
    ])
    graph = GraphBuilder(config).build("test")

    tc_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.TOOL_CALL]
    assert len(tc_nodes) == 1

    node = tc_nodes[0]
    assert len(node.config["calls"]) == 1
    assert node.config["calls"][0]["tool"] == "my_tool"
    assert node.config["calls"][0]["parameters"] == {"x": "1"}
    assert node.config["on_error"] == "fail"


def test_graph_build_tool_call_multi():
    """Multi-tool tool_call compiles calls list correctly."""
    config = _make_graph_config([
        ToolCallStep(tool_call=ToolCallConfig(calls=[
            ToolCallEntry(tool="my_tool", parameters={"a": "1"}, output_as="r1"),
            ToolCallEntry(tool="my_tool", parameters={"b": "2"}, output_as="r2"),
        ], output_as="final")),
    ])
    graph = GraphBuilder(config).build("test")

    tc_nodes = [n for n in graph.nodes.values() if n.node_type == NodeType.TOOL_CALL]
    assert len(tc_nodes) == 1
    assert len(tc_nodes[0].config["calls"]) == 2


def test_graph_tool_call_chained_with_agent():
    """tool_call node chains correctly with a following agent node."""
    from elasticity.config.schema import StepInput

    config = _make_graph_config([
        ToolCallStep(tool_call=ToolCallConfig(tool="my_tool", output_as="data")),
        StepInput(agent="worker", input="{data}"),
    ])
    graph = GraphBuilder(config).build("test")

    entry = graph.nodes[graph.entry_node]
    assert entry.node_type == NodeType.TOOL_CALL
    assert entry.next is not None
    assert graph.nodes[entry.next].node_type == NodeType.AGENT


# ---------------------------------------------------------------------------
# Executor tests
# ---------------------------------------------------------------------------


def _make_executor_with_mock_tool(tool_fn, on_error="fail"):
    """Helper to create an Executor with a mock tool registered."""
    config = Config(
        agent_types={},
        tools={
            "mock_tool": ToolDefinition(
                description="mock",
                callable="builtins.print",  # placeholder
                parameters={},
            ),
        },
        orchestrations={},
    )

    registry = ToolRegistry()
    registry.register("mock_tool", config.tools["mock_tool"])
    # Override the callable resolution with our mock
    registry._callables["mock_tool"] = tool_fn

    bus = EventBus()
    executor = Executor(config, registry, event_bus=bus)
    return executor, bus


@pytest.mark.asyncio
async def test_execute_tool_call_single():
    """Single tool invocation sets output_as in context."""
    mock_fn = AsyncMock(return_value="hello world")
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc1",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {"msg": "hi"}, "output_as": "result"}],
            "output_as": "result",
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    assert context.get_output("result") == "hello world"
    mock_fn.assert_awaited_once_with(msg="hi")


@pytest.mark.asyncio
async def test_execute_tool_call_template_interpolation():
    """String parameters are template-interpolated from context."""
    mock_fn = AsyncMock(return_value="done")
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc2",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {"key": "{my_var}"}, "output_as": "res"}],
            "output_as": "res",
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    context.set_output("my_var", "interpolated_value")
    await executor._execute_tool_call_node(node, context)

    mock_fn.assert_awaited_once_with(key="interpolated_value")


@pytest.mark.asyncio
async def test_execute_tool_call_non_string_passthrough():
    """Non-string parameter values pass through without interpolation."""
    mock_fn = AsyncMock(return_value="ok")
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc3",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {"count": 42, "flag": True}, "output_as": None}],
            "output_as": None,
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    mock_fn.assert_awaited_once_with(count=42, flag=True)


@pytest.mark.asyncio
async def test_execute_tool_call_multi_sequential():
    """Multiple calls execute sequentially; later calls can reference earlier output_as."""
    call_log = []

    async def mock_fn(**kwargs):
        call_log.append(kwargs)
        return f"result_{len(call_log)}"

    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc4",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [
                {"tool": "mock_tool", "parameters": {"step": "first"}, "output_as": "r1"},
                {"tool": "mock_tool", "parameters": {"prev": "{r1}"}, "output_as": "r2"},
            ],
            "output_as": "final",
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    assert context.get_output("r1") == "result_1"
    assert context.get_output("r2") == "result_2"
    assert context.get_output("final") == "result_2"
    # Second call should have interpolated r1
    assert call_log[1]["prev"] == "result_1"


@pytest.mark.asyncio
async def test_execute_tool_call_on_error_skip():
    """on_error='skip' logs warning and continues with empty result."""
    mock_fn = AsyncMock(side_effect=RuntimeError("boom"))
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc5",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {}, "output_as": "res"}],
            "output_as": "res",
            "on_error": "skip",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    # Should set empty string, not raise
    assert context.get_output("res") == ""


@pytest.mark.asyncio
async def test_execute_tool_call_on_error_fail():
    """on_error='fail' raises ExecutionError."""
    mock_fn = AsyncMock(side_effect=RuntimeError("boom"))
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc6",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {}, "output_as": None}],
            "output_as": None,
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    with pytest.raises(ExecutionError, match="tool_call step failed"):
        await executor._execute_tool_call_node(node, context)


@pytest.mark.asyncio
async def test_execute_tool_call_events_emitted():
    """ToolCallStepStarted and ToolCallStepCompleted events are emitted."""
    mock_fn = AsyncMock(return_value="result")
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    events_received = []
    bus.subscribe(ToolCallStepStarted, lambda e: events_received.append(("started", e)))
    bus.subscribe(ToolCallStepCompleted, lambda e: events_received.append(("completed", e)))

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc7",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {"x": "1"}, "output_as": None}],
            "output_as": None,
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    assert len(events_received) == 2
    assert events_received[0][0] == "started"
    assert events_received[0][1].tool_name == "mock_tool"
    assert events_received[0][1].parameters == {"x": "1"}
    assert events_received[1][0] == "completed"
    assert events_received[1][1].tool_name == "mock_tool"
    assert events_received[1][1].duration_ms >= 0


@pytest.mark.asyncio
async def test_execute_tool_call_none_result():
    """Tool returning None stores empty string."""
    mock_fn = AsyncMock(return_value=None)
    executor, bus = _make_executor_with_mock_tool(mock_fn)

    from elasticity.compiler.graph import GraphNode

    node = GraphNode(
        node_id="tc8",
        node_type=NodeType.TOOL_CALL,
        config={
            "calls": [{"tool": "mock_tool", "parameters": {}, "output_as": "res"}],
            "output_as": "res",
            "on_error": "fail",
        },
    )

    context = ContextManager("message_passing")
    await executor._execute_tool_call_node(node, context)

    assert context.get_output("res") == ""

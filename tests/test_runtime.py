"""Tests for runtime execution."""

import pytest
from pathlib import Path
from elasticity import Orchestration
from elasticity.config.loader import load_config
from elasticity.compiler.graph import GraphBuilder
from elasticity.runtime.tools import ToolRegistry
from elasticity.runtime.context import ContextManager
from elasticity.runtime.executor import Executor
from elasticity.config.schema import Config, AgentTypeDefinition


def test_context_manager_message_passing():
    """Test context manager with message passing."""
    context = ContextManager("message_passing")

    context.set_output("test_output", "test_value")
    assert context.get_output("test_output") == "test_value"

    formatted = context.format_input("Hello {test_output}")
    assert formatted == "Hello test_value"


def test_context_manager_shared_context():
    """Test context manager with shared context."""
    context = ContextManager("shared_context")

    context.set_shared("key", "value")
    assert context.get_shared("key") == "value"

    formatted = context.format_input("Hello {key}")
    assert formatted == "Hello value"


def test_context_manager_both():
    """Test context manager with both modes."""
    context = ContextManager("both")

    context.set_output("output", "output_value")
    context.set_shared("shared", "shared_value")

    assert context.get_output("output") == "output_value"
    assert context.get_shared("shared") == "shared_value"

    formatted = context.format_input("Output: {output}, Shared: {shared}")
    assert formatted == "Output: output_value, Shared: shared_value"


def test_context_manager_initial_input():
    """Test that initial input is always available for templating, regardless of communication mode."""
    # Test with message_passing mode
    context = ContextManager("message_passing")
    context.set_initial_input({"message": "hello", "user_id": "123"})
    
    # Should be able to format with initial_input even though no set_output was called
    formatted = context.format_input("Message: {message}, User: {user_id}")
    assert formatted == "Message: hello, User: 123"
    
    # Test that messages can override initial_input (messages come after in merge order)
    context.set_output("message", "overridden")
    formatted = context.format_input("Message: {message}")
    assert formatted == "Message: overridden"  # messages override initial_input
    
    # Test with shared_context mode
    context2 = ContextManager("shared_context")
    context2.set_initial_input({"message": "test"})
    formatted = context2.format_input("Message: {message}")
    assert formatted == "Message: test"
    
    # Test with both mode - messages override initial_input, shared_context, and initial_input
    context3 = ContextManager("both")
    context3.set_initial_input({"message": "initial"})
    context3.set_shared("message", "shared")
    context3.set_output("message", "output")
    formatted = context3.format_input("Message: {message}")
    assert formatted == "Message: output"  # messages (last in merge) override everything


def test_tool_registry():
    """Test tool registry."""
    registry = ToolRegistry()

    from elasticity.config.schema import ToolDefinition, ParameterSchema

    tool_def = ToolDefinition(
        description="Test tool",
        callable="builtins.print",
        parameters={
            "message": ParameterSchema(type="string", required=True),
        },
    )

    registry.register("test_tool", tool_def)
    assert "test_tool" in registry.get_available_tools()

    schema = registry.get_tool_schema("test_tool")
    assert schema["function"]["name"] == "test_tool"


def test_tool_init_hook_called():
    """Test that _tool_init is called when config is present."""
    import tempfile
    import os
    from pathlib import Path
    from elasticity.config.schema import ToolDefinition, ParameterSchema
    import elasticity.tools.memory as memory_module

    # Reset module state
    memory_module._connections.clear()
    memory_module._default_db_path = None
    memory_module._memory_store.clear()

    registry = ToolRegistry()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        # Register tool with config
        tool_def = ToolDefinition(
            description="Test memory tool",
            callable="elasticity.tools.memory.store",
            config={"db_path": db_path},
            parameters={
                "key": ParameterSchema(type="string", required=True),
                "value": ParameterSchema(type="string", required=True),
            },
        )

        registry.register("test_memory", tool_def)

        # Load callable - should trigger _tool_init and initialize SQLite
        callable_func = registry.load_callable("test_memory")

        # Verify SQLite was initialized
        resolved = str(Path(db_path).expanduser().resolve())
        assert resolved in memory_module._connections

        # Verify it works
        result = callable_func(key="test_key", value="test_value")
        assert "Stored memory under key 'test_key'" in result

    finally:
        # Cleanup
        for conn in memory_module._connections.values():
            conn.close()
        memory_module._connections.clear()
        memory_module._default_db_path = None
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_tool_init_called_once_per_module():
    """Test that _tool_init is only called once per module even with multiple tools."""
    import tempfile
    import os
    from pathlib import Path
    from elasticity.config.schema import ToolDefinition, ParameterSchema
    import elasticity.tools.memory as memory_module

    # Reset module state
    memory_module._connections.clear()
    memory_module._default_db_path = None
    memory_module._memory_store.clear()

    registry = ToolRegistry()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        # Register two tools from the same module
        tool_def1 = ToolDefinition(
            description="Store tool",
            callable="elasticity.tools.memory.store",
            config={"db_path": db_path},
            parameters={
                "key": ParameterSchema(type="string", required=True),
                "value": ParameterSchema(type="string", required=True),
            },
        )
        tool_def2 = ToolDefinition(
            description="Retrieve tool",
            callable="elasticity.tools.memory.retrieve",
            config={"db_path": db_path},
            parameters={"query": ParameterSchema(type="string", required=True)},
        )

        registry.register("store_tool", tool_def1)
        registry.register("retrieve_tool", tool_def2)

        # Load both callables - should only initialize once
        store_func = registry.load_callable("store_tool")
        retrieve_func = registry.load_callable("retrieve_tool")

        # Verify SQLite was initialized (only once)
        resolved = str(Path(db_path).expanduser().resolve())
        assert resolved in memory_module._connections

        # Verify both tools work
        store_func(key="test", value="value")
        result = retrieve_func(query="test")
        assert "test" in result
        assert "value" in result

    finally:
        # Cleanup
        for conn in memory_module._connections.values():
            conn.close()
        memory_module._connections.clear()
        memory_module._default_db_path = None
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_memory_store_retrieve_sqlite():
    """Test memory store and retrieve with SQLite persistence."""
    import tempfile
    import os
    from elasticity.tools.memory import store, retrieve, _tool_init

    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        # Initialize with SQLite
        _tool_init({"db_path": db_path})

        # Store a memory
        result = store("test_key", "test_value")
        assert "Stored memory under key 'test_key'" in result

        # Retrieve it
        retrieved = retrieve("test_key")
        assert "test_key" in retrieved
        assert "test_value" in retrieved
        assert "Created:" in retrieved
        assert "Updated:" in retrieved

        # Test that it persists across function calls
        retrieved2 = retrieve("test")
        assert "test_key" in retrieved2
        assert "test_value" in retrieved2

        # Store another memory
        store("another_key", "another_value")
        retrieved3 = retrieve("another")
        assert "another_key" in retrieved3
        assert "another_value" in retrieved3

        # Update existing key
        store("test_key", "updated_value")
        retrieved4 = retrieve("test_key")
        assert "updated_value" in retrieved4
        assert "test_value" not in retrieved4

    finally:
        # Cleanup
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_memory_timestamps():
    """Test that timestamps are populated correctly."""
    import tempfile
    import os
    from datetime import datetime
    from elasticity.tools.memory import store, retrieve, _tool_init

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        _tool_init({"db_path": db_path})

        # Store a memory
        store("timestamp_test", "test_value")

        # Retrieve and check timestamps
        retrieved = retrieve("timestamp_test")
        assert "Created:" in retrieved
        assert "Updated:" in retrieved

        # Parse timestamps (they should be ISO format)
        lines = retrieved.split("\n")
        created_line = [l for l in lines if "Created:" in l][0]
        updated_line = [l for l in lines if "Updated:" in l][0]

        created_str = created_line.split("Created:")[1].strip()
        updated_str = updated_line.split("Updated:")[1].strip()

        # Should be valid ISO format timestamps
        datetime.fromisoformat(created_str)
        datetime.fromisoformat(updated_str)

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_memory_fallback_in_memory():
    """Test fallback to in-memory dict when _tool_init is not called."""
    from elasticity.tools.memory import store, retrieve
    import elasticity.tools.memory as memory_module

    # Reset module state
    memory_module._connections.clear()
    memory_module._default_db_path = None
    memory_module._memory_store.clear()

    # Should work with in-memory fallback
    result = store("fallback_key", "fallback_value")
    assert "Stored memory under key 'fallback_key'" in result

    retrieved = retrieve("fallback_key")
    assert "fallback_key" in retrieved
    assert "fallback_value" in retrieved
    assert "Created:" not in retrieved  # No timestamps in fallback mode


def test_evaluate_condition_comparison_expressions():
    """Test condition evaluation with comparison expressions."""
    # Create a minimal config for executor
    config = Config(agent_types={}, tools={}, orchestrations={})
    executor = Executor(config, ToolRegistry())
    context = ContextManager("message_passing")

    # Test >= operator
    context.set_output("coherence_score", 0.92)
    assert executor._evaluate_condition("coherence_score >= 0.85", context) is True
    assert executor._evaluate_condition("coherence_score >= 0.95", context) is False

    # Test <= operator
    context.set_output("quality_score", 0.7)
    assert executor._evaluate_condition("quality_score <= 0.9", context) is True
    assert executor._evaluate_condition("quality_score <= 0.5", context) is False

    # Test > operator
    context.set_output("score", 0.6)
    assert executor._evaluate_condition("score > 0.5", context) is True
    assert executor._evaluate_condition("score > 0.7", context) is False

    # Test < operator
    context.set_output("value", 0.3)
    assert executor._evaluate_condition("value < 0.5", context) is True
    assert executor._evaluate_condition("value < 0.2", context) is False

    # Test == operator
    context.set_output("status", "done")
    assert executor._evaluate_condition('status == "done"', context) is True
    assert executor._evaluate_condition('status == "pending"', context) is False

    # Test != operator
    context.set_output("flag", True)
    assert executor._evaluate_condition("flag != False", context) is True
    assert executor._evaluate_condition("flag != True", context) is False

    # Test with shared context (need to use "both" mode for shared context)
    context_shared = ContextManager("both")
    context_shared.set_shared("shared_var", 42)
    assert executor._evaluate_condition("shared_var == 42", context_shared) is True


def test_evaluate_condition_simple_variable():
    """Test condition evaluation with simple variable references (backward compatibility)."""
    config = Config(agent_types={}, tools={}, orchestrations={})
    executor = Executor(config, ToolRegistry())
    context = ContextManager("message_passing")

    # Test simple truthy variable
    context.set_output("flag", True)
    assert executor._evaluate_condition("{flag}", context) is True

    context.set_output("flag", False)
    assert executor._evaluate_condition("{flag}", context) is False

    # Test numeric truthiness
    context.set_output("count", 5)
    assert executor._evaluate_condition("{count}", context) is True

    context.set_output("count", 0)
    assert executor._evaluate_condition("{count}", context) is False

    # Test string truthiness
    context.set_output("status", "true")
    assert executor._evaluate_condition("{status}", context) is True

    context.set_output("status", "false")
    assert executor._evaluate_condition("{status}", context) is False


def test_extract_schema_fields():
    """Test extraction of output_schema fields into context."""
    config = Config(agent_types={}, tools={}, orchestrations={})
    executor = Executor(config, ToolRegistry())
    context = ContextManager("message_passing")

    # Test with valid JSON
    output_schema = {"coherence_score": "float", "feedback": "string"}
    content = '{"coherence_score": 0.92, "feedback": "Good response"}'
    executor._extract_schema_fields(content, output_schema, context)

    assert context.get_output("coherence_score") == 0.92
    assert context.get_output("feedback") == "Good response"

    # Test with JSON embedded in text
    context2 = ContextManager("message_passing")
    content2 = 'Here is the result: {"quality_score": 0.85, "notes": "Needs improvement"}'
    output_schema2 = {"quality_score": "float", "notes": "string"}
    executor._extract_schema_fields(content2, output_schema2, context2)

    assert context2.get_output("quality_score") == 0.85
    assert context2.get_output("notes") == "Needs improvement"

    # Test with invalid JSON (should fail silently)
    context3 = ContextManager("message_passing")
    content3 = "This is not JSON at all"
    executor._extract_schema_fields(content3, output_schema, context3)

    # Should not have set any values
    assert context3.get_output("coherence_score") is None


def test_extract_schema_fields_type_coercion():
    """Test that extracted fields are coerced to the declared schema types."""
    config = Config(agent_types={}, tools={}, orchestrations={})
    executor = Executor(config, ToolRegistry())

    # Float coercion: LLM may return an int (1) that should become a float
    context = ContextManager("message_passing")
    executor._extract_schema_fields(
        '{"score": 1, "label": "ok"}',
        {"score": "float", "label": "string"},
        context,
    )
    assert context.get_output("score") == 1.0
    assert isinstance(context.get_output("score"), float)
    assert context.get_output("label") == "ok"

    # Int coercion: LLM may return a float that should be an int
    context2 = ContextManager("message_passing")
    executor._extract_schema_fields(
        '{"count": 3.0}',
        {"count": "int"},
        context2,
    )
    assert context2.get_output("count") == 3
    assert isinstance(context2.get_output("count"), int)

    # Bool coercion from string
    context3 = ContextManager("message_passing")
    executor._extract_schema_fields(
        '{"flag": "true"}',
        {"flag": "bool"},
        context3,
    )
    assert context3.get_output("flag") is True

    # String coercion from number
    context4 = ContextManager("message_passing")
    executor._extract_schema_fields(
        '{"code": 42}',
        {"code": "string"},
        context4,
    )
    assert context4.get_output("code") == "42"

    # Unknown type hint: value should pass through unchanged
    context5 = ContextManager("message_passing")
    executor._extract_schema_fields(
        '{"data": [1, 2, 3]}',
        {"data": "list"},
        context5,
    )
    assert context5.get_output("data") == [1, 2, 3]


def test_agent_runner_injects_output_schema_into_prompt():
    """AgentRunner should append JSON instructions to the system prompt when output_schema is set."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from elasticity.runtime.agent import AgentRunner
    from elasticity.backends.base import CompletionResponse

    tool_registry = ToolRegistry()
    runner = AgentRunner(tool_registry)

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a classifier.",
        output_schema={"category": "string"},
    )

    captured_messages = []

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        captured_messages.extend(messages)
        return CompletionResponse(content='{"category": "analytical"}', tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        asyncio.run(runner.run(agent_type, "classifier", "Classify this text.", context))

    system_message = next(m for m in captured_messages if m["role"] == "system")
    assert "JSON" in system_message["content"]
    assert "category" in system_message["content"]
    assert "string" in system_message["content"]


def test_output_schema_prompt_includes_tool_sequencing():
    """When output_schema is set and tools are present, the prompt should instruct the LLM
    to complete tool calls before producing the JSON output."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from elasticity.runtime.agent import AgentRunner
    from elasticity.backends.base import CompletionResponse
    from elasticity.config.schema import ToolDefinition, ParameterSchema

    tool_registry = ToolRegistry()
    tool_registry.register(
        "memory_store",
        ToolDefinition(
            description="Store a value",
            callable="elasticity.tools.memory.store",
            parameters={"key": ParameterSchema(type="string", required=True), "value": ParameterSchema(type="string", required=True)},
        ),
    )
    runner = AgentRunner(tool_registry)

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a planner.",
        output_schema={"plan_path": "string"},
        tools=["memory_store"],
    )

    captured_messages = []

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        captured_messages.extend(messages)
        return CompletionResponse(content='{"plan_path": "plans/foo.md"}', tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        asyncio.run(runner.run(agent_type, "planner", "Write a plan.", context))

    system_message = next(m for m in captured_messages if m["role"] == "system")
    assert "Complete all tool calls" in system_message["content"]
    assert "final" in system_message["content"]
    assert "plan_path" in system_message["content"]


def test_agent_runner_no_output_schema_prompt_unchanged():
    """AgentRunner should NOT append JSON instructions when output_schema is absent."""
    import asyncio
    from unittest.mock import MagicMock, patch
    from elasticity.runtime.agent import AgentRunner
    from elasticity.backends.base import CompletionResponse

    tool_registry = ToolRegistry()
    runner = AgentRunner(tool_registry)

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a helpful assistant.",
    )

    captured_messages = []

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        captured_messages.extend(messages)
        return CompletionResponse(content="Hello!", tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        asyncio.run(runner.run(agent_type, "assistant", "Hi", context))

    system_message = next(m for m in captured_messages if m["role"] == "system")
    assert system_message["content"] == "You are a helpful assistant."


def test_multi_turn_tool_calling():
    """Test that AgentRunner loops until LLM stops requesting tools."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from elasticity.runtime.agent import AgentRunner
    from elasticity.backends.base import CompletionResponse, ToolCall

    tool_registry = ToolRegistry()
    
    # Register a test tool
    from elasticity.config.schema import ToolDefinition, ParameterSchema
    tool_def = ToolDefinition(
        description="Test tool",
        callable="builtins.dict",
        parameters={
            "value": ParameterSchema(type="string", required=True),
        },
    )
    tool_registry.register("test_tool", tool_def)
    
    runner = AgentRunner(tool_registry)

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a test agent.",
        tools=["test_tool"],
        max_tool_rounds=5,
    )

    call_count = 0
    captured_messages = []

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        nonlocal call_count
        call_count += 1
        captured_messages.extend(messages)
        
        if call_count == 1:
            # First call: request tool
            return CompletionResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="test_tool", arguments={"value": "test"})]
            )
        else:
            # Second call: final response
            return CompletionResponse(content="Tool executed successfully", tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    context = ContextManager("message_passing")

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        result = asyncio.run(runner.run(agent_type, "test_agent", "Test input", context))

    # Should have called backend twice (tool call + final response)
    assert call_count == 2
    assert result["content"] == "Tool executed successfully"
    assert len(result["tool_calls"]) == 1
    # Should have sent tool result back to LLM
    assert len(captured_messages) > 2  # system + user + assistant + tool result


def test_builtin_tool_resolution():
    """Test that builtin field resolves to correct callable and parameters."""
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.config.schema import ToolDefinition

    registry = ToolRegistry()

    # Register tool using builtin shorthand
    tool_def = ToolDefinition(
        description="File read tool",
        builtin="file_read",
    )

    registry.register("my_file_read", tool_def)

    # Verify it was resolved
    registered_def = registry._tools["my_file_read"]
    assert registered_def.callable == "elasticity.tools.filesystem.read"
    assert "path" in registered_def.parameters
    assert registered_def.parameters["path"].required is True


def test_builtin_tool_unknown():
    """Test that unknown builtin tool raises error."""
    from elasticity.runtime.tools import ToolRegistry
    from elasticity.config.schema import ToolDefinition
    from elasticity.errors import ToolError

    registry = ToolRegistry()

    tool_def = ToolDefinition(
        description="Unknown tool",
        builtin="nonexistent_tool",
    )

    with pytest.raises(ToolError, match="Unknown built-in tool"):
        registry.register("test", tool_def)


def test_session_management():
    """Test Session class for conversational mode."""
    from elasticity.runtime.session import Session

    session = Session()
    assert session.id is not None
    assert len(session.message_history) == 0
    assert len(session.context) == 0

    # Add turns
    session.add_turn("Hello", "Hi there!")
    assert len(session.message_history) == 2
    assert session.message_history[0]["role"] == "user"
    assert session.message_history[0]["content"] == "Hello"
    assert session.message_history[1]["role"] == "assistant"
    assert session.message_history[1]["content"] == "Hi there!"

    # Test history windowing
    session.max_history_turns = 2
    for i in range(5):
        session.add_turn(f"Message {i}", f"Response {i}")
    
    # Should only keep last 2 turns (4 messages)
    assert len(session.message_history) == 4

    # Test clear
    session.context["test"] = "value"
    session.clear()
    assert len(session.message_history) == 0
    assert len(session.context) == 0


def test_conversational_mode_with_session():
    """Test that conversational orchestrations use session context and history."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from elasticity.runtime.session import Session
    from elasticity.runtime.agent import AgentRunner
    from elasticity.backends.base import CompletionResponse

    tool_registry = ToolRegistry()
    runner = AgentRunner(tool_registry)

    agent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a helpful assistant.",
    )

    captured_messages = []

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        captured_messages.extend(messages)
        return CompletionResponse(content="Response", tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    context = ContextManager("message_passing")
    session = Session()
    session.add_turn("Previous message", "Previous response")
    session.context["previous_data"] = "value"

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        asyncio.run(runner.run(agent_type, "assistant", "New message", context, session))

    # Should have included session history
    user_messages = [m for m in captured_messages if m["role"] == "user"]
    assert len(user_messages) >= 2  # Previous + new


def test_conversational_orchestration_receives_user_message():
    """Test that conversational orchestrations properly pass user message to first agent."""
    import asyncio
    from unittest.mock import MagicMock, patch
    from elasticity.runtime.executor import Executor
    from elasticity.compiler.graph import GraphBuilder
    from elasticity.backends.base import CompletionResponse
    from elasticity.config.schema import OrchestrationDefinition, StepInput, RouteStep, RouteCase

    # Create a minimal conversational orchestration config
    classifier_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a classifier. Respond with only: research, file, or general.",
    )
    
    generalist_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are a helpful assistant.",
    )

    config = Config(
        agent_types={
            "classifier": classifier_type,
            "generalist": generalist_type,
        },
        tools={},
        orchestrations={
            "assistant": OrchestrationDefinition(
                mode="conversational",
                response_key="response",
                input={"message": "string"},
                communication="message_passing",  # Default mode
                flow=[
                    StepInput(
                        agent="classifier",
                        input="{message}",
                        output_as="intent",
                    ),
                    RouteStep(
                        route=RouteCase(
                            condition="{intent}",
                            cases={
                                "general": [
                                    StepInput(
                                        agent="generalist",
                                        input="{message}",
                                        output_as="response",
                                    )
                                ],
                            },
                        ),
                    ),
                ],
            )
        },
    )

    captured_classifier_input = None

    async def fake_complete(model, messages, tools=None, max_tokens=4096, response_format=None):
        nonlocal captured_classifier_input
        # Capture the user message sent to classifier
        user_msgs = [m for m in messages if m["role"] == "user"]
        if user_msgs:
            # Check if this is the classifier by looking at system prompt
            system_msgs = [m for m in messages if m["role"] == "system"]
            if system_msgs and "classifier" in system_msgs[0].get("content", "").lower():
                captured_classifier_input = user_msgs[0]["content"]
        
        # Return appropriate responses based on which agent is being called
        system_msgs = [m for m in messages if m["role"] == "system"]
        if system_msgs and "classifier" in system_msgs[0].get("content", "").lower():
            return CompletionResponse(content="general", tool_calls=[])
        else:
            return CompletionResponse(content="Hello! How can I help?", tool_calls=[])

    mock_backend = MagicMock()
    mock_backend.complete = fake_complete

    graph_builder = GraphBuilder(config)
    graph = graph_builder.build("assistant")
    executor = Executor(config, ToolRegistry())

    with patch("elasticity.runtime.agent.resolve_backend", return_value=(mock_backend, "gpt-4o")):
        result = asyncio.run(
            executor.execute(
                graph,
                "assistant",
                ContextManager("message_passing"),
                input_data={"message": "Hello, I need help with something"},
            )
        )

    # Verify the classifier received the actual user message, not the literal "{message}"
    assert captured_classifier_input is not None
    assert captured_classifier_input == "Hello, I need help with something"
    assert "{message}" not in captured_classifier_input
    # Verify response is in messages (result structure includes initial_input and messages)
    assert "messages" in result
    assert "response" in result["messages"]
    assert result["messages"]["response"] == "Hello! How can I help?"


def test_builtin_filesystem_tools():
    """Test built-in filesystem tools."""
    import tempfile
    from pathlib import Path
    from elasticity.tools.filesystem import read, write, list_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.txt"
        test_content = "Hello, world!"

        # Test write
        result = write(str(test_file), test_content)
        assert "Successfully wrote" in result
        assert test_file.exists()

        # Test read
        content = read(str(test_file))
        assert content == test_content

        # Test list_dir
        listing = list_dir(tmpdir)
        assert "FILE" in listing
        assert "test.txt" in listing


def test_builtin_tool_validation():
    """Test that validator checks builtin tool names."""
    from elasticity.config.loader import load_config
    from elasticity.config.validator import validate_references
    from elasticity.errors import ConfigReferenceError
    import tempfile
    import yaml

    # Invalid builtin name
    invalid_config = {
        "tools": {
            "bad_tool": {
                "builtin": "nonexistent_builtin",
                "description": "Test",
            }
        },
        "agent_types": {},
        "orchestrations": {},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(invalid_config, f)
        config_path = f.name

    try:
        config = load_config(config_path)
        with pytest.raises(ConfigReferenceError, match="unknown built-in tool"):
            validate_references(config)
    finally:
        Path(config_path).unlink()


def test_builtin_only_tool_no_description():
    """Test that builtin-only tools can omit description."""
    from elasticity.config.loader import load_config
    from elasticity.config.validator import validate_references
    import tempfile
    import yaml

    # Builtin-only tool without description (should work)
    config_data = {
        "tools": {
            "file_read": {
                "builtin": "file_read",
            },
            "file_write": {
                "builtin": "file_write",
            },
        },
        "agent_types": {},
        "orchestrations": {},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_data, f)
        config_path = f.name

    try:
        config = load_config(config_path)
        validate_references(config)  # Should not raise
        
        # Verify tools were loaded correctly
        assert "file_read" in config.tools
        assert "file_write" in config.tools
        assert config.tools["file_read"].builtin == "file_read"
        assert config.tools["file_read"].description is None
    finally:
        Path(config_path).unlink()


def test_callable_tool_requires_description():
    """Test that callable-only tools must provide description."""
    from elasticity.config.loader import load_config
    from elasticity.errors import ConfigError
    import tempfile
    import yaml

    # Callable-only tool without description (should fail)
    invalid_config = {
        "tools": {
            "custom_tool": {
                "callable": "some.module.function",
            }
        },
        "agent_types": {},
        "orchestrations": {},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(invalid_config, f)
        config_path = f.name

    try:
        with pytest.raises(ConfigError, match="must provide a 'description'"):
            load_config(config_path)
    finally:
        Path(config_path).unlink()

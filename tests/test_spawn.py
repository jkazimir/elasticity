"""Tests for the spawn feature."""

import asyncio
from unittest.mock import MagicMock
from typing import Any, List, Optional

from elasticity.config.schema import AgentTypeDefinition, Config
from elasticity.events import (
    EventBus,
    SpawnCompleted,
    SpawnParseFailed,
    SpawnStarted,
    SpawnWaveCompleted,
    SpawnWaveStarted,
)
from elasticity.runtime.context import ContextManager
from elasticity.runtime.executor import Executor
from elasticity.runtime.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(
    parent_can_spawn: Optional[List[str]] = None,
    max_concurrent_spawns: Optional[int] = None,
) -> Executor:
    """Build a minimal Executor with a parent agent and a child agent type."""
    parent_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are the parent.",
        can_spawn=parent_can_spawn or ["child"],
        max_concurrent_spawns=max_concurrent_spawns,
    )
    child_type = AgentTypeDefinition(
        model="openai/gpt-4o",
        system_prompt="You are the child.",
    )
    config = Config(
        agent_types={"parent": parent_type, "child": child_type},
        tools={},
        orchestrations={},
    )
    bus = EventBus()
    executor = Executor(config, ToolRegistry(), event_bus=bus)
    return executor


def _captured_events(executor: Executor, event_type: type) -> List[Any]:
    """Subscribe to the executor's bus and capture all events of the given type."""
    received = []
    executor._events.subscribe(event_type, received.append)
    return received


# ---------------------------------------------------------------------------
# _parse_spawn_requests — parsing tests (synchronous)
# ---------------------------------------------------------------------------


def test_parse_clean_json():
    """Fast path: content is exactly valid spawn JSON."""
    executor = _make_executor()
    content = '{"spawn": [{"agent": "child", "input": "do the thing"}]}'
    result = executor._parse_spawn_requests(content)
    assert result == [[{"agent": "child", "input": "do the thing"}]]


def test_parse_with_preamble_text():
    """Regex / brace-scanner path: LLM adds introductory text before the JSON."""
    executor = _make_executor()
    content = 'Sure, here is my plan:\n\n{"spawn": [{"agent": "child", "input": "task one"}]}'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1   # one wave
    assert result[0][0]["agent"] == "child"


def test_parse_with_trailing_text():
    """Brace-scanner: LLM adds trailing text after the JSON block."""
    executor = _make_executor()
    content = '{"spawn": [{"agent": "child", "input": "foo"}]}\n\nLet me know if you need changes.'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1   # one wave
    assert result[0][0]["input"] == "foo"


def test_parse_with_markdown_code_fence():
    """Fence path: LLM wraps JSON in ```json ... ``` fences."""
    executor = _make_executor()
    content = "```json\n{\"spawn\": [{\"agent\": \"child\", \"input\": \"bar\"}]}\n```"
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1   # one wave
    assert result[0][0]["agent"] == "child"


def test_parse_with_plain_code_fence():
    """Fence path: plain ``` fence without language tag."""
    executor = _make_executor()
    content = "```\n{\"spawn\": [{\"agent\": \"child\", \"input\": \"baz\"}]}\n```"
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1


def test_parse_with_nested_braces_in_input():
    """Critical regression: spawn input contains curly braces (code, templates)."""
    executor = _make_executor()
    content = (
        '{"spawn": [{"agent": "child", "input": '
        '"Implement function render() { return {x: 1}; }"}]}'
    )
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1   # one wave
    assert result[0][0]["agent"] == "child"
    assert "{x: 1}" in result[0][0]["input"]


def test_parse_preamble_and_nested_braces():
    """Brace-scanner: preamble text AND nested braces in spawn input."""
    executor = _make_executor()
    content = (
        "Task decomposition complete.\n\n"
        '{"spawn": [{"agent": "child", "input": "Create class User { id: string; }"},'
        ' {"agent": "child", "input": "Add method foo() { return {a: 1}; }"}]}'
    )
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1    # one wave
    assert len(result[0]) == 2  # two tasks in that wave
    assert result[0][0]["agent"] == "child"
    assert result[0][1]["agent"] == "child"


def test_parse_multiple_spawn_items():
    """Multiple spawn requests are all returned in one wave."""
    executor = _make_executor()
    content = (
        '{"spawn": ['
        '{"agent": "child", "input": "task 1"}, '
        '{"agent": "child", "input": "task 2"}, '
        '{"agent": "child", "input": "task 3"}'
        "]}"
    )
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1    # one wave
    assert len(result[0]) == 3  # three tasks
    assert result[0][2]["input"] == "task 3"


def test_parse_empty_content():
    """Empty string returns empty list."""
    executor = _make_executor()
    assert executor._parse_spawn_requests("") == []


def test_parse_no_spawn_key():
    """Valid JSON without a 'spawn' key returns empty list."""
    executor = _make_executor()
    assert executor._parse_spawn_requests('{"result": "ok"}') == []


def test_parse_gibberish():
    """Unparseable content returns empty list."""
    executor = _make_executor()
    assert executor._parse_spawn_requests("not json at all") == []


def test_parse_spawn_value_not_list():
    """'spawn' key present but value is not a list returns empty list."""
    executor = _make_executor()
    assert executor._parse_spawn_requests('{"spawn": "should be a list"}') == []


# ---------------------------------------------------------------------------
# _extract_json_objects helper tests
# ---------------------------------------------------------------------------


def test_extract_single_object():
    executor = _make_executor()
    result = executor._extract_json_objects('{"a": 1}')
    assert result == ['{"a": 1}']


def test_extract_multiple_top_level_objects():
    executor = _make_executor()
    result = executor._extract_json_objects('{"a": 1} some text {"b": 2}')
    assert len(result) == 2


def test_extract_nested_objects():
    executor = _make_executor()
    result = executor._extract_json_objects('{"outer": {"inner": "val"}}')
    assert len(result) == 1
    assert result[0] == '{"outer": {"inner": "val"}}'


def test_extract_object_with_braces_in_string():
    executor = _make_executor()
    result = executor._extract_json_objects('{"code": "fn() { return {x: 1}; }"}')
    assert len(result) == 1
    assert result[0] == '{"code": "fn() { return {x: 1}; }"}'


def test_extract_no_objects():
    executor = _make_executor()
    assert executor._extract_json_objects("no braces here") == []


# ---------------------------------------------------------------------------
# _execute_spawns — async runtime tests
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_spawns_are_called():
    """Spawned agent runner is invoked for each spawn request."""
    executor = _make_executor()
    call_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        call_log.append(agent_name)
        return {"content": f"done: {input_text}"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "task A"}, {"agent": "child", "input": "task B"}]}'
    context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert call_log == ["child", "child"]


def test_collect_as_populated():
    """collect_as stores successful spawn outputs in context."""
    executor = _make_executor()

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        return {"content": "result from " + input_text}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}]}'
    context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    outputs = context.get_output("results")
    assert isinstance(outputs, list)
    assert len(outputs) == 2
    assert all("result from" in o for o in outputs)


def test_spawned_agents_receive_branched_context():
    """Each spawn gets a context branch, not the parent's ContextManager."""
    executor = _make_executor()
    received_contexts = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_contexts.append(context)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}]}'
    parent_context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=parent_context,
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(received_contexts) == 2
    # Each spawn should receive a different context instance (branched copies)
    assert received_contexts[0] is not parent_context
    assert received_contexts[1] is not parent_context
    assert received_contexts[0] is not received_contexts[1]


def test_spawned_agents_receive_session_none():
    """Spawned agents are called with session=None regardless of parent session."""
    executor = _make_executor()
    received_sessions = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_sessions.append(session)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t"}]}'
    parent_session = MagicMock()  # simulate a non-None parent session

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=parent_session,
        collect_as=None,
    ))

    assert received_sessions == [None]


def test_branch_writes_merged_into_parent():
    """Context writes made by a spawned agent are merged back to the parent context."""
    executor = _make_executor()

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        # Simulate the spawned agent writing to its branched context
        context.set_output("child_result", "written by child")
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t"}]}'
    parent_context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=parent_context,
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert parent_context.get_output("child_result") == "written by child"


def test_one_failure_does_not_kill_siblings():
    """If one spawned agent errors, other siblings still complete."""
    executor = _make_executor()
    call_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        if input_text == "fail":
            raise RuntimeError("intentional error")
        call_log.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "ok1"}, {"agent": "child", "input": "fail"}, {"agent": "child", "input": "ok2"}]}'
    context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    # The two successful spawns should still appear in results
    results = context.get_output("results")
    assert results is not None
    assert len(results) == 2


def test_spawn_started_and_completed_events():
    """SpawnStarted and SpawnCompleted events are emitted for each child."""
    executor = _make_executor()
    started = _captured_events(executor, SpawnStarted)
    completed = _captured_events(executor, SpawnCompleted)

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(started) == 2
    assert all(e.parent_agent == "parent" for e in started)
    assert all(e.child_type == "child" for e in started)
    assert len(completed) == 2


def test_parse_failure_emits_spawn_parse_failed_event():
    """SpawnParseFailed is emitted when content is non-empty but not parseable."""
    executor = _make_executor()
    failures = _captured_events(executor, SpawnParseFailed)

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content="I was supposed to output JSON but forgot.",
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(failures) == 1
    assert failures[0].parent_agent == "parent"
    assert len(failures[0].content_preview) > 0


def test_parse_failure_on_empty_content_no_event():
    """Empty content does not emit SpawnParseFailed (nothing to parse)."""
    executor = _make_executor()
    failures = _captured_events(executor, SpawnParseFailed)

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content="",
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(failures) == 0


def test_spawn_not_permitted_child_type_skipped():
    """A spawn request for a child type not in can_spawn is silently skipped."""
    executor = _make_executor(parent_can_spawn=["child"])  # "stranger" is not allowed
    call_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        call_log.append(agent_name)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "stranger", "input": "t"}]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    assert call_log == []
    assert executor._events  # no crash


def test_max_concurrent_spawns_batching():
    """With max_concurrent_spawns=2, 4 spawns are processed in two batches."""
    executor = _make_executor(max_concurrent_spawns=2)
    start_order = []
    import asyncio as _asyncio

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        start_order.append(input_text)
        await _asyncio.sleep(0)  # yield to let other coroutines start
        return {"content": f"done-{input_text}"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}, {"agent": "child", "input": "t3"}, {"agent": "child", "input": "t4"}]}'
    context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    # All 4 spawns should complete
    results = context.get_output("results")
    assert results is not None
    assert len(results) == 4


# ---------------------------------------------------------------------------
# _parse_spawn_requests — waves format parsing tests
# ---------------------------------------------------------------------------


def test_parse_waves_format():
    """Multi-wave JSON is parsed correctly into a list of lists."""
    executor = _make_executor()
    content = '{"waves": [[{"agent": "child", "input": "w0t1"}], [{"agent": "child", "input": "w1t1"}, {"agent": "child", "input": "w1t2"}]]}'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 2
    assert len(result[0]) == 1
    assert result[0][0]["input"] == "w0t1"
    assert len(result[1]) == 2
    assert result[1][0]["input"] == "w1t1"
    assert result[1][1]["input"] == "w1t2"


def test_parse_waves_single_wave():
    """Waves format with a single wave works correctly."""
    executor = _make_executor()
    content = '{"waves": [[{"agent": "child", "input": "only task"}]]}'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1
    assert result[0][0]["input"] == "only task"


def test_parse_waves_in_markdown_fence():
    """Waves format wrapped in a markdown code fence is parsed correctly."""
    executor = _make_executor()
    content = '```json\n{"waves": [[{"agent": "child", "input": "t1"}], [{"agent": "child", "input": "t2"}]]}\n```'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 2
    assert result[0][0]["input"] == "t1"
    assert result[1][0]["input"] == "t2"


def test_parse_waves_with_preamble():
    """Waves format with LLM preamble text is extracted by the brace scanner."""
    executor = _make_executor()
    content = 'Here is my decomposition:\n\n{"waves": [[{"agent": "child", "input": "t"}]]}'
    result = executor._parse_spawn_requests(content)
    assert len(result) == 1
    assert result[0][0]["input"] == "t"


def test_parse_waves_value_not_list():
    """'waves' key present but value is not a list returns empty."""
    executor = _make_executor()
    assert executor._parse_spawn_requests('{"waves": "bad"}') == []


def test_parse_waves_inner_not_lists():
    """'waves' is a list but inner elements are not lists — not a valid waves format, falls through."""
    executor = _make_executor()
    # This is neither a valid waves format nor a legacy spawn format — should return []
    assert executor._parse_spawn_requests('{"waves": [{"agent": "child"}]}') == []


# ---------------------------------------------------------------------------
# _execute_spawns — wave execution behavior tests
# ---------------------------------------------------------------------------


def test_waves_executed_sequentially():
    """Wave 0 tasks all complete before any wave 1 task starts."""
    executor = _make_executor()
    import asyncio as _asyncio
    execution_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        execution_log.append(("start", input_text))
        await _asyncio.sleep(0)  # yield so other coroutines in same wave can run
        execution_log.append(("end", input_text))
        return {"content": f"done:{input_text}"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "w0t1"}, {"agent": "child", "input": "w0t2"}], [{"agent": "child", "input": "w1t1"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    # Wave 1 input may have prior-wave context prepended, so match by suffix.
    w0t1_end = next(i for i, (op, t) in enumerate(execution_log) if op == "end" and t == "w0t1")
    w0t2_end = next(i for i, (op, t) in enumerate(execution_log) if op == "end" and t == "w0t2")
    w1t1_start = next(i for i, (op, t) in enumerate(execution_log) if op == "start" and t.endswith("w1t1"))
    assert w0t1_end < w1t1_start, "wave 0 task 1 must end before wave 1 starts"
    assert w0t2_end < w1t1_start, "wave 0 task 2 must end before wave 1 starts"


def test_waves_collect_as_flattened():
    """Results from all waves are collected into a single flat list."""
    executor = _make_executor()

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        return {"content": f"result:{input_text}"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "a"}], [{"agent": "child", "input": "b"}, {"agent": "child", "input": "c"}]]}'
    context = ContextManager("both")
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    results = context.get_output("results")
    assert len(results) == 3
    # Wave 0 result is unchanged; wave 1 results may include prior-wave prefix.
    assert any(r == "result:a" for r in results)
    assert any("b" in r for r in results)
    assert any("c" in r for r in results)


def test_wave_context_merges_between_waves():
    """Context writes from wave 0 are visible in wave 1 branches."""
    executor = _make_executor()
    seen_values = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        # Wave 1 inputs may be prefixed with prior-wave context; match by suffix.
        if input_text.strip() == "w0":
            context.set_output("from_wave0", "hello")
        elif input_text.strip().endswith("w1"):
            seen_values.append(context.get_output("from_wave0"))
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "w0"}], [{"agent": "child", "input": "w1"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert seen_values == ["hello"]


def test_wave_events_emitted():
    """SpawnWaveStarted and SpawnWaveCompleted are emitted for each wave."""
    executor = _make_executor()
    wave_started = _captured_events(executor, SpawnWaveStarted)
    wave_completed = _captured_events(executor, SpawnWaveCompleted)

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "t1"}], [{"agent": "child", "input": "t2"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(wave_started) == 2
    assert wave_started[0].wave_index == 0
    assert wave_started[0].wave_count == 2
    assert wave_started[0].spawn_count == 1
    assert wave_started[1].wave_index == 1
    assert len(wave_completed) == 2
    assert wave_completed[0].wave_index == 0
    assert wave_completed[1].wave_index == 1


def test_legacy_spawn_format_still_works():
    """Legacy {"spawn": [...]} continues to execute correctly through the wave machinery."""
    executor = _make_executor()
    call_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        call_log.append(input_text)
        return {"content": f"done:{input_text}"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}]}'
    context = ContextManager("both")
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as="results",
    ))

    assert sorted(call_log) == ["t1", "t2"]
    assert len(context.get_output("results")) == 2


def test_empty_wave_skipped():
    """An empty wave array does not crash and is silently skipped."""
    executor = _make_executor()
    call_log = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        call_log.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[], [{"agent": "child", "input": "t1"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert call_log == ["t1"]


def test_wave_prior_outputs_injected_into_next_wave_input():
    """Wave N+1 spawn inputs are prefixed with wave N text outputs."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": f"output_for:{input_text.split()[-1]}"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "wave0task"}], [{"agent": "child", "input": "wave1task"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(received_inputs) == 2
    # Wave 0 task receives original input unchanged
    assert received_inputs[0] == "wave0task"
    # Wave 1 task input is prefixed with wave 0's output
    assert "--- PRIOR WAVE RESULTS ---" in received_inputs[1]
    assert "output_for:wave0task" in received_inputs[1]
    assert "--- END PRIOR WAVE RESULTS ---" in received_inputs[1]
    assert received_inputs[1].endswith("wave1task")


def test_wave_prior_outputs_accumulate_across_multiple_waves():
    """Wave 2 inputs include outputs from both wave 0 and wave 1."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        # Extract just the last word as the task name for a clean output
        task = input_text.strip().split()[-1]
        return {"content": f"done:{task}"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "w0"}], [{"agent": "child", "input": "w1"}], [{"agent": "child", "input": "w2"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert len(received_inputs) == 3
    assert received_inputs[0] == "w0"
    # Wave 1 sees wave 0's output
    assert "done:w0" in received_inputs[1]
    # Wave 2 sees both wave 0 and wave 1 outputs
    assert "done:w0" in received_inputs[2]
    assert "done:w1" in received_inputs[2]


def test_wave_prior_outputs_not_injected_for_single_wave():
    """Single-wave spawns receive their original input unchanged."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "done"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "only_task"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
    ))

    assert received_inputs == ["only_task"]


# ---------------------------------------------------------------------------
# spawn_context — shared context injection tests
# ---------------------------------------------------------------------------


def test_spawn_context_prepended_to_child_input():
    """spawn_context is prepended to every spawned child's input."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "do task A"}]}'
    context = ContextManager("both")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context="SHARED CONTEXT",
    ))

    assert len(received_inputs) == 1
    assert received_inputs[0].startswith("SHARED CONTEXT")
    assert received_inputs[0].endswith("do task A")


def test_spawn_context_none_leaves_input_unchanged():
    """When spawn_context is None, child input is passed through unchanged."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "original input"}]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context=None,
    ))

    assert received_inputs == ["original input"]


def test_spawn_context_template_resolved_from_parent_context():
    """Template variables in spawn_context are resolved using the parent context."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "task"}]}'
    context = ContextManager("both")
    context.set_output("arch_plan", "Use microservices")

    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=context,
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context="ARCHITECTURE:\n{arch_plan}",
    ))

    assert len(received_inputs) == 1
    assert "Use microservices" in received_inputs[0]
    assert received_inputs[0].endswith("task")


def test_spawn_context_applied_to_all_spawns():
    """spawn_context is prepended to every spawn in a wave, not just the first."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "task1"}, {"agent": "child", "input": "task2"}, {"agent": "child", "input": "task3"}]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context="PREAMBLE",
    ))

    assert len(received_inputs) == 3
    assert all(inp.startswith("PREAMBLE") for inp in received_inputs)


def test_spawn_context_with_waves_ordering():
    """spawn_context appears before prior-wave results and the task input."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "wave0-output"}

    executor.agent_runner.run = fake_run

    content = '{"waves": [[{"agent": "child", "input": "wave0task"}], [{"agent": "child", "input": "wave1task"}]]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context="SHARED CONTEXT",
    ))

    assert len(received_inputs) == 2
    # Wave 0: spawn_context prepended, no prior wave results
    assert received_inputs[0].startswith("SHARED CONTEXT")
    assert received_inputs[0].endswith("wave0task")
    # Wave 1: spawn_context first, then prior wave results, then task input
    wave1 = received_inputs[1]
    ctx_pos = wave1.index("SHARED CONTEXT")
    prior_pos = wave1.index("--- PRIOR WAVE RESULTS ---")
    task_pos = wave1.index("wave1task")
    assert ctx_pos < prior_pos < task_pos


def test_spawn_context_missing_variable_resolves_empty():
    """Unknown template variables in spawn_context resolve to empty string, not an error."""
    executor = _make_executor()
    received_inputs = []

    async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
        received_inputs.append(input_text)
        return {"content": "ok"}

    executor.agent_runner.run = fake_run

    content = '{"spawn": [{"agent": "child", "input": "task"}]}'
    _run(executor._execute_spawns(
        agent_name="parent",
        agent_type=executor.config.agent_types["parent"],
        content=content,
        context=ContextManager("both"),
        default_error_strategy=None,
        session=None,
        collect_as=None,
        spawn_context="PREFIX: {nonexistent_variable} END",
    ))

    # Should not raise; unknown variable resolves to ""
    assert len(received_inputs) == 1
    assert "nonexistent_variable" not in received_inputs[0]


def test_spawn_context_formatted_once_not_per_spawn():
    """spawn_context template is resolved once, not once per spawned child."""
    executor = _make_executor()
    format_call_count = []

    original_format_input = ContextManager.format_input

    def counting_format_input(self, text, **kwargs):
        if text and "{arch}" in text:
            format_call_count.append(1)
        return original_format_input(self, text, **kwargs)

    import elasticity.runtime.context as ctx_module
    ctx_module.ContextManager.format_input = counting_format_input

    try:
        async def fake_run(agent_type, agent_name, input_text, context, session, **kw):
            return {"content": "ok"}

        executor.agent_runner.run = fake_run

        content = '{"spawn": [{"agent": "child", "input": "t1"}, {"agent": "child", "input": "t2"}, {"agent": "child", "input": "t3"}]}'
        context = ContextManager("both")
        context.set_output("arch", "plan content")

        _run(executor._execute_spawns(
            agent_name="parent",
            agent_type=executor.config.agent_types["parent"],
            content=content,
            context=context,
            default_error_strategy=None,
            session=None,
            collect_as=None,
            spawn_context="ARCH: {arch}",
        ))

        # spawn_context template should be formatted exactly once regardless of spawn count
        assert len(format_call_count) == 1
    finally:
        ctx_module.ContextManager.format_input = original_format_input

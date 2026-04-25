"""Tests for Conductor runtime reconfiguration.

Covers:
  - ToolRegistry.unregister()
  - Conductor._rebuild_agent_type() tool list contents
  - Conductor.reload_team() happy path and failure (bad YAML)
  - Conductor.add_team() and remove_team()
  - Management tools are registered and callable
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from elasticity.runtime.tools import ToolRegistry
from elasticity.config.schema import ToolDefinition, ParameterSchema


# ---------------------------------------------------------------------------
# Minimal YAML fixtures
# ---------------------------------------------------------------------------

CONDUCTOR_YAML = """\
agent_types:
  boss:
    model: anthropic/claude-opus-4-5
    system_prompt: You are the boss.

conductor:
  agent: boss

tools:
  file_write:
    builtin: file_write

teams:
  research:
    config: ./research_team.yaml
    orchestration: main
    description: Researches topics.
    input:
      topic: string
    output: report
"""

TEAM_YAML = """\
agent_types:
  worker:
    model: anthropic/claude-opus-4-5
    system_prompt: You are a worker.

tools: {}

orchestrations:
  main:
    mode: batch
    input:
      topic: string
    flow:
      - step: worker
        agent: worker
"""

TEAM_YAML_V2 = """\
agent_types:
  worker:
    model: anthropic/claude-opus-4-5
    system_prompt: You are a smarter worker.

tools: {}

orchestrations:
  main:
    mode: batch
    input:
      topic: string
    flow:
      - step: worker
        agent: worker
"""

BAD_YAML = "{ unclosed: ["


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conductor_dir(tmp_path: Path) -> Path:
    """Temp dir with a minimal conductor YAML and a team YAML."""
    (tmp_path / "conductor.yaml").write_text(CONDUCTOR_YAML)
    (tmp_path / "research_team.yaml").write_text(TEAM_YAML)
    return tmp_path


@pytest.fixture()
def conductor(conductor_dir: Path):
    """Conductor instance built from the fixture directory."""
    from elasticity.conductor import Conductor
    return Conductor(str(conductor_dir / "conductor.yaml"))


# ---------------------------------------------------------------------------
# ToolRegistry.unregister()
# ---------------------------------------------------------------------------


class TestToolRegistryUnregister:
    def test_unregister_removes_tool(self):
        registry = ToolRegistry()
        param = ParameterSchema(type="string", required=True, description="x")
        registry.register_callable("my_tool", "desc", {"x": param}, fn=lambda x: x)

        assert "my_tool" in registry.get_available_tools()
        registry.unregister("my_tool")
        assert "my_tool" not in registry.get_available_tools()

    def test_unregister_nonexistent_is_safe(self):
        registry = ToolRegistry()
        # Should not raise
        registry.unregister("does_not_exist")

    def test_unregister_clears_callable_cache(self):
        registry = ToolRegistry()
        param = ParameterSchema(type="string", required=True, description="x")
        fn = lambda x: x
        registry.register_callable("tool", "desc", {"x": param}, fn=fn)
        registry.unregister("tool")
        # After unregister, _callables entry should also be gone
        assert "tool" not in registry._callables


# ---------------------------------------------------------------------------
# _rebuild_agent_type() — tool list correctness
# ---------------------------------------------------------------------------


class TestRebuildAgentType:
    def test_team_names_in_tools(self, conductor):
        """Team names must appear in the agent type's tools list."""
        assert "research" in conductor._agent_type.tools

    def test_conductor_config_tools_in_tools(self, conductor):
        """Conductor-level tools (e.g. file_write) must appear."""
        assert "file_write" in conductor._agent_type.tools

    def test_management_tools_in_tools(self, conductor):
        """Management tools must always be included."""
        for name in ("reload_team", "add_team", "remove_team"):
            assert name in conductor._agent_type.tools

    def test_no_duplicate_tools(self, conductor):
        tools = conductor._agent_type.tools
        assert len(tools) == len(set(tools))

    def test_manifest_in_system_prompt(self, conductor):
        """Both team manifest and management manifest must be in system prompt."""
        sp = conductor._agent_type.system_prompt
        assert "research" in sp
        assert "reload_team" in sp or "Team Management Tools" in sp


# ---------------------------------------------------------------------------
# reload_team()
# ---------------------------------------------------------------------------


class TestReloadTeam:
    def test_reload_team_happy_path(self, conductor, conductor_dir: Path):
        """reload_team should swap out the Orchestration object."""
        old_orch = conductor._team_orchestrations["research"]
        # Write a new (valid) YAML
        (conductor_dir / "research_team.yaml").write_text(TEAM_YAML_V2)

        result = conductor.reload_team("research")
        assert "successfully" in result
        new_orch = conductor._team_orchestrations["research"]
        assert new_orch is not old_orch

    def test_reload_team_retains_old_on_bad_yaml(self, conductor, conductor_dir: Path):
        """On invalid YAML, the old orchestration must be retained."""
        old_orch = conductor._team_orchestrations["research"]
        (conductor_dir / "research_team.yaml").write_text(BAD_YAML)

        result = conductor.reload_team("research")
        assert "Error" in result
        assert conductor._team_orchestrations["research"] is old_orch

    def test_reload_nonexistent_team(self, conductor):
        result = conductor.reload_team("ghost_team")
        assert "Error" in result

    def test_reload_updates_agent_type(self, conductor, conductor_dir: Path):
        """After reload, _rebuild_agent_type should have been called."""
        (conductor_dir / "research_team.yaml").write_text(TEAM_YAML_V2)
        before_tools = list(conductor._agent_type.tools)
        conductor.reload_team("research")
        # Tools list should still contain research
        assert "research" in conductor._agent_type.tools


# ---------------------------------------------------------------------------
# add_team() and remove_team()
# ---------------------------------------------------------------------------


class TestAddRemoveTeam:
    def test_add_team(self, conductor, conductor_dir: Path):
        (conductor_dir / "writing_team.yaml").write_text(TEAM_YAML)

        result = conductor.add_team(
            team_name="writing",
            config_path="./writing_team.yaml",
            orchestration="main",
            description="Writes polished content.",
            output_key="article",
        )
        assert "successfully" in result
        assert "writing" in conductor._team_orchestrations
        assert "writing" in conductor.config.teams
        assert "writing" in conductor._agent_type.tools
        assert "writing" in conductor.tool_registry.get_available_tools()

    def test_add_team_bad_yaml(self, conductor, conductor_dir: Path):
        (conductor_dir / "bad_team.yaml").write_text(BAD_YAML)

        result = conductor.add_team(
            team_name="bad",
            config_path="./bad_team.yaml",
            orchestration="main",
            description="Should fail.",
        )
        assert "Error" in result
        assert "bad" not in conductor.config.teams

    def test_add_team_reserved_name(self, conductor, conductor_dir: Path):
        (conductor_dir / "reload_team.yaml").write_text(TEAM_YAML)

        result = conductor.add_team(
            team_name="reload_team",
            config_path="./reload_team.yaml",
            orchestration="main",
            description="Should be blocked.",
        )
        assert "Error" in result

    def test_remove_team(self, conductor):
        result = conductor.remove_team("research")
        assert "successfully" in result
        assert "research" not in conductor.config.teams
        assert "research" not in conductor._team_orchestrations
        assert "research" not in conductor.tool_registry.get_available_tools()
        assert "research" not in conductor._agent_type.tools

    def test_remove_nonexistent_team(self, conductor):
        result = conductor.remove_team("ghost")
        assert "Error" in result

    def test_remove_team_updates_manifest(self, conductor):
        conductor.remove_team("research")
        sp = conductor._agent_type.system_prompt
        assert "research" not in sp


# ---------------------------------------------------------------------------
# Management tools are actually registered in the tool registry
# ---------------------------------------------------------------------------


class TestManagementToolsRegistered:
    def test_management_tools_in_registry(self, conductor):
        available = conductor.tool_registry.get_available_tools()
        for name in ("reload_team", "add_team", "remove_team"):
            assert name in available

    def test_management_tools_have_schema(self, conductor):
        for name in ("reload_team", "add_team", "remove_team"):
            schema = conductor.tool_registry.get_tool_schema(name)
            assert schema["function"]["name"] == name

    def test_reload_team_tool_callable(self, conductor, conductor_dir: Path):
        """The registered reload_team tool should be invocable asynchronously."""
        (conductor_dir / "research_team.yaml").write_text(TEAM_YAML_V2)
        result = asyncio.run(
            conductor.tool_registry.invoke_async("reload_team", {"team_name": "research"})
        )
        assert "successfully" in result


# ---------------------------------------------------------------------------
# call_team() — run log integration
# ---------------------------------------------------------------------------


class TestCallTeamRunLog:
    def test_log_path_appended_to_result(self, conductor, tmp_path: Path):
        """After a successful team run, the result should contain the log path."""
        async def fake_run(*args, **kwargs):
            return {"report": "great findings"}

        conductor._team_orchestrations["research"].run = fake_run

        with patch("elasticity.conductor.write_team_run_log", return_value=str(tmp_path / "fake_log.md")):
            result = asyncio.run(
                conductor.tool_registry.invoke_async("research", {"topic": "AI"})
            )

        assert "fake_log.md" in result
        assert "Run log written to" in result

    def test_events_forwarded_to_parent_bus(self, conductor, tmp_path: Path):
        """Events emitted by the child bus must reach the parent bus subscribers."""
        from elasticity.events import AgentStarted

        received: list = []
        conductor._events.subscribe(AgentStarted, lambda e: received.append(e))

        async def fake_run(*args, event_bus=None, **kwargs):
            if event_bus:
                event_bus.emit(AgentStarted(agent_name="worker", step_id="s1"))
            return {"report": "done"}

        conductor._team_orchestrations["research"].run = fake_run

        with patch("elasticity.conductor.write_team_run_log", return_value=""):
            asyncio.run(
                conductor.tool_registry.invoke_async("research", {"topic": "test"})
            )

        assert len(received) == 1
        assert received[0].agent_name == "worker"

    def test_log_path_in_error_result(self, conductor, tmp_path: Path):
        """Even when the team run raises, the log path should appear in the error message."""
        async def failing_run(*args, **kwargs):
            raise RuntimeError("team exploded")

        conductor._team_orchestrations["research"].run = failing_run

        with patch("elasticity.conductor.write_team_run_log", return_value=str(tmp_path / "error_log.md")):
            result = asyncio.run(
                conductor.tool_registry.invoke_async("research", {"topic": "AI"})
            )

        assert "Error" in result
        assert "error_log.md" in result

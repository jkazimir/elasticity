"""Tests for format_run_log() and write_team_run_log()."""

from __future__ import annotations

import json
from datetime import datetime, UTC, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from elasticity.tracing import RunTrace, format_run_log, write_team_run_log


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace(orchestration_name: str = "main") -> RunTrace:
    trace = RunTrace(run_id="abc12345", orchestration_name=orchestration_name, log_to_console=False)
    return trace


def _add_event(trace: RunTrace, event_type: str, **kwargs) -> None:
    """Directly append a synthetic event dict to trace.events."""
    trace.events.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            "step_id": kwargs.pop("step_id", "step_1"),
            **kwargs,
        }
    )


# ---------------------------------------------------------------------------
# format_run_log — header and input rendering
# ---------------------------------------------------------------------------


class TestFormatRunLogHeader:
    def test_header_contains_team_name(self):
        trace = _make_trace("main")
        result = format_run_log(trace, "research", {})
        assert "# Team Run Log: research" in result

    def test_header_contains_orchestration_name(self):
        trace = _make_trace("main_orch")
        result = format_run_log(trace, "research", {})
        assert "main_orch" in result

    def test_header_contains_run_id(self):
        trace = _make_trace()
        result = format_run_log(trace, "research", {})
        assert "abc12345" in result

    def test_duration_shown_when_complete(self):
        trace = _make_trace()
        trace.complete()
        result = format_run_log(trace, "research", {})
        assert "Duration" in result

    def test_duration_absent_when_not_complete(self):
        trace = _make_trace()
        result = format_run_log(trace, "research", {})
        assert "Duration" not in result

    def test_input_args_rendered(self):
        trace = _make_trace()
        result = format_run_log(trace, "research", {"task": "AI safety"})
        assert "task" in result
        assert "AI safety" in result

    def test_input_truncation(self):
        trace = _make_trace()
        long_val = "x" * 500
        result = format_run_log(trace, "research", {"task": long_val})
        assert "[truncated]" in result
        assert "x" * 301 not in result  # must not exceed 300 chars untruncated


# ---------------------------------------------------------------------------
# format_run_log — event rendering
# ---------------------------------------------------------------------------


class TestFormatRunLogEvents:
    def test_agent_token_is_skipped(self):
        trace = _make_trace()
        _add_event(trace, "AgentToken", agent_name="worker", token="hello")
        result = format_run_log(trace, "t", {})
        assert "AgentToken" not in result
        assert "hello" not in result

    def test_agent_started_rendered(self):
        trace = _make_trace()
        _add_event(trace, "AgentStarted", agent_name="researcher", input_text="Research AI")
        result = format_run_log(trace, "t", {})
        assert "researcher" in result
        assert "Research AI" in result

    def test_agent_started_input_truncated(self):
        trace = _make_trace()
        _add_event(trace, "AgentStarted", agent_name="researcher", input_text="x" * 600)
        result = format_run_log(trace, "t", {})
        assert "[truncated]" in result

    def test_agent_completed_rendered(self):
        trace = _make_trace()
        _add_event(trace, "AgentCompleted", agent_name="writer", output="Great essay", duration_ms=1234.0)
        result = format_run_log(trace, "t", {})
        assert "writer" in result
        assert "Great essay" in result
        assert "1234" in result

    def test_agent_completed_output_truncated(self):
        trace = _make_trace()
        _add_event(trace, "AgentCompleted", agent_name="writer", output="y" * 1100, duration_ms=0)
        result = format_run_log(trace, "t", {})
        assert "[truncated]" in result

    def test_agent_error_rendered(self):
        trace = _make_trace()
        _add_event(trace, "AgentErrorEvent", agent_name="worker", error="rate limit")
        result = format_run_log(trace, "t", {})
        assert "Agent Error" in result
        assert "rate limit" in result

    def test_tool_called_rendered(self):
        trace = _make_trace()
        _add_event(trace, "ToolCalled", agent_name="w", tool_name="web_search", arguments={"query": "AI"})
        result = format_run_log(trace, "t", {})
        assert "web_search" in result
        assert "AI" in result

    def test_tool_called_args_truncated(self):
        trace = _make_trace()
        _add_event(trace, "ToolCalled", agent_name="w", tool_name="shell", arguments={"cmd": "z" * 400})
        result = format_run_log(trace, "t", {})
        assert "[truncated]" in result

    def test_tool_result_rendered(self):
        trace = _make_trace()
        _add_event(trace, "ToolResult", agent_name="w", tool_name="file_read", result="file contents", duration_ms=50.0)
        result = format_run_log(trace, "t", {})
        assert "file_read" in result
        assert "file contents" in result

    def test_tool_result_truncated(self):
        trace = _make_trace()
        _add_event(trace, "ToolResult", agent_name="w", tool_name="file_read", result="r" * 600, duration_ms=0)
        result = format_run_log(trace, "t", {})
        assert "[truncated]" in result

    def test_tool_denied_rendered(self):
        trace = _make_trace()
        _add_event(trace, "ToolDenied", agent_name="w", tool_name="shell", reason="policy")
        result = format_run_log(trace, "t", {})
        assert "Tool Denied" in result
        assert "shell" in result
        assert "policy" in result

    def test_node_started_rendered(self):
        trace = _make_trace()
        _add_event(trace, "NodeStarted", step_id="step_a", node_type="AGENT")
        result = format_run_log(trace, "t", {})
        assert "step_a" in result
        assert "AGENT" in result

    def test_node_completed_rendered(self):
        trace = _make_trace()
        _add_event(trace, "NodeCompleted", step_id="step_a")
        result = format_run_log(trace, "t", {})
        assert "Step Done" in result

    def test_node_error_rendered(self):
        trace = _make_trace()
        _add_event(trace, "NodeError", step_id="step_a", error="boom")
        result = format_run_log(trace, "t", {})
        assert "Step Error" in result
        assert "boom" in result

    def test_loop_iteration_rendered(self):
        trace = _make_trace()
        _add_event(trace, "LoopIteration", step_id="loop_1", iteration=3)
        result = format_run_log(trace, "t", {})
        assert "Loop Iteration 3" in result

    def test_route_taken_rendered(self):
        trace = _make_trace()
        _add_event(trace, "RouteTaken", step_id="route_1", case="success")
        result = format_run_log(trace, "t", {})
        assert "Route" in result
        assert "success" in result

    def test_parallel_started_rendered(self):
        trace = _make_trace()
        _add_event(trace, "ParallelStarted", step_id="par_1", branch_count=3)
        result = format_run_log(trace, "t", {})
        assert "Parallel" in result
        assert "3" in result

    def test_parallel_completed_rendered(self):
        trace = _make_trace()
        _add_event(trace, "ParallelCompleted", step_id="par_1")
        result = format_run_log(trace, "t", {})
        assert "Parallel Done" in result

    def test_supervisor_worker_started_rendered(self):
        trace = _make_trace()
        _add_event(
            trace, "SupervisorWorkerStarted",
            worker_id="w1", worker_agent="drafter", attempt=1
        )
        result = format_run_log(trace, "t", {})
        assert "Supervisor Worker" in result
        assert "drafter" in result

    def test_supervisor_review_rendered(self):
        trace = _make_trace()
        _add_event(trace, "SupervisorReview", supervisor="reviewer", worker_id="w1", attempt=1)
        result = format_run_log(trace, "t", {})
        assert "Supervisor Review" in result

    def test_supervisor_accepted_rendered(self):
        trace = _make_trace()
        _add_event(trace, "SupervisorAccepted", supervisor="reviewer", worker_id="w1", attempt=1)
        result = format_run_log(trace, "t", {})
        assert "Supervisor Accepted" in result

    def test_supervisor_rejected_rendered(self):
        trace = _make_trace()
        _add_event(
            trace, "SupervisorRejected",
            supervisor="reviewer", worker_id="w1", attempt=2, feedback="not good enough"
        )
        result = format_run_log(trace, "t", {})
        assert "Supervisor Rejected" in result
        assert "not good enough" in result

    def test_unknown_event_type_not_in_output(self):
        trace = _make_trace()
        _add_event(trace, "OrchestrationStarted", run_id="xyz", orchestration_name="main")
        result = format_run_log(trace, "t", {})
        # Should not error; OrchestrationStarted is silently omitted
        assert "# Team Run Log" in result


# ---------------------------------------------------------------------------
# write_team_run_log — file creation
# ---------------------------------------------------------------------------


class TestWriteTeamRunLog:
    def test_creates_file(self, tmp_path: Path):
        trace = _make_trace()
        with patch("elasticity.tracing.Path") as MockPath:
            # Use real filesystem via tmp_path
            real_log_dir = tmp_path / "run_logs"
            MockPath.return_value = real_log_dir
            # Bypass the mock — call with real path
            pass

        # Use real /tmp or tmp_path; patch the log dir
        trace = _make_trace()
        log_dir = tmp_path / "elasticity" / "run_logs"

        import elasticity.tracing as tracing_mod
        original_path = tracing_mod.Path

        def patched_path(*args):
            val = str(args[0]) if args else ""
            if val == "/tmp/elasticity/run_logs":
                return log_dir
            return original_path(*args)

        with patch.object(tracing_mod, "Path", side_effect=patched_path):
            result = write_team_run_log(trace, "research", {"task": "AI"}, "director")

        # Should have written a file somewhere under tmp_path
        assert result != ""
        assert Path(result).exists()

    def test_filename_contains_team_and_conductor(self, tmp_path: Path):
        trace = _make_trace()
        log_dir = tmp_path / "elasticity" / "run_logs"
        log_dir.mkdir(parents=True)

        import elasticity.tracing as tracing_mod
        original_path = tracing_mod.Path

        def patched_path(*args):
            val = str(args[0]) if args else ""
            if val == "/tmp/elasticity/run_logs":
                return log_dir
            return original_path(*args)

        with patch.object(tracing_mod, "Path", side_effect=patched_path):
            result = write_team_run_log(trace, "general_purpose", {"task": "AI"}, "director")

        fname = Path(result).name
        assert "director" in fname
        assert "general_purpose" in fname
        assert fname.endswith(".md")

    def test_file_contains_team_name(self, tmp_path: Path):
        trace = _make_trace()
        log_dir = tmp_path / "elasticity" / "run_logs"
        log_dir.mkdir(parents=True)

        import elasticity.tracing as tracing_mod
        original_path = tracing_mod.Path

        def patched_path(*args):
            val = str(args[0]) if args else ""
            if val == "/tmp/elasticity/run_logs":
                return log_dir
            return original_path(*args)

        with patch.object(tracing_mod, "Path", side_effect=patched_path):
            result = write_team_run_log(trace, "my_team", {"task": "hello"}, "boss")

        content = Path(result).read_text()
        assert "my_team" in content

    def test_returns_empty_string_on_os_error(self):
        trace = _make_trace()
        import elasticity.tracing as tracing_mod

        class FailingPath:
            def __init__(self, *args):
                self._val = str(args[0]) if args else ""

            def __truediv__(self, other):
                return self

            def mkdir(self, **kwargs):
                raise OSError("no space")

            def __str__(self):
                return self._val

        with patch.object(tracing_mod, "Path", side_effect=lambda *a: FailingPath(*a)):
            result = write_team_run_log(trace, "t", {}, "conductor")

        assert result == ""

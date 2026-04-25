"""Tests for the shell tool."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import elasticity.tools.shell as shell_module
from elasticity.tools.shell import _tool_describe, _tool_init, execute
from elasticity.config.schema import ToolDefinition
from elasticity.runtime.tools import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(stdout="", stderr="", returncode=0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# _tool_init
# ---------------------------------------------------------------------------

class TestToolInit:
    def setup_method(self):
        shell_module._mode = "direct"

    def teardown_method(self):
        shell_module._mode = "direct"

    def test_default_mode_is_direct(self):
        assert shell_module._mode == "direct"

    def test_init_sets_bash_mode(self):
        _tool_init({"mode": "bash"})
        assert shell_module._mode == "bash"

    def test_init_sets_direct_mode_explicitly(self):
        shell_module._mode = "bash"
        _tool_init({"mode": "direct"})
        assert shell_module._mode == "direct"

    def test_init_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid shell mode"):
            _tool_init({"mode": "zsh"})

    def test_init_empty_config(self):
        _tool_init({})
        assert shell_module._mode == "direct"

    def test_init_none_config(self):
        _tool_init(None)
        assert shell_module._mode == "direct"


# ---------------------------------------------------------------------------
# _tool_describe
# ---------------------------------------------------------------------------

class TestToolDescribe:
    def test_direct_mode_description(self):
        desc = _tool_describe({"mode": "direct"})
        assert "no pipes" in desc.lower() or "single process" in desc.lower()

    def test_bash_mode_description(self):
        desc = _tool_describe({"mode": "bash"})
        assert "bash" in desc.lower()
        assert "pipe" in desc.lower()

    def test_empty_config_returns_direct_description(self):
        assert _tool_describe({}) == _tool_describe({"mode": "direct"})

    def test_none_config_returns_direct_description(self):
        assert _tool_describe(None) == _tool_describe({"mode": "direct"})

    def test_bash_differs_from_direct(self):
        assert _tool_describe({"mode": "bash"}) != _tool_describe({"mode": "direct"})


# ---------------------------------------------------------------------------
# execute() in direct mode
# ---------------------------------------------------------------------------

class TestDirectMode:
    def setup_method(self):
        shell_module._mode = "direct"

    def teardown_method(self):
        shell_module._mode = "direct"

    def test_simple_command_splits_with_shlex(self):
        with patch("subprocess.run", return_value=_make_run("hello\n")) as mock_run:
            result = execute("echo hello")
        args = mock_run.call_args[0][0]
        assert args == ["echo", "hello"]
        assert "hello" in result

    def test_pipe_becomes_literal_args(self):
        """In direct mode pipes are just extra arguments, not interpreted."""
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo hello | grep h")
        args = mock_run.call_args[0][0]
        assert args == ["echo", "hello", "|", "grep", "h"]

    def test_custom_timeout_forwarded(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo hi", timeout=30)
        assert mock_run.call_args[1]["timeout"] == 30

    def test_stderr_included_in_output(self):
        with patch("subprocess.run", return_value=_make_run(stdout="out", stderr="err")):
            result = execute("cmd")
        assert "[stderr]" in result
        assert "err" in result

    def test_nonzero_exit_code_shown(self):
        with patch("subprocess.run", return_value=_make_run(returncode=1)):
            result = execute("false")
        assert "[exit code: 1]" in result

    def test_zero_exit_code_not_shown(self):
        with patch("subprocess.run", return_value=_make_run(stdout="ok", returncode=0)):
            result = execute("true")
        assert "[exit code" not in result

    def test_timeout_raises_timeout_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
            with pytest.raises(TimeoutError, match="timed out"):
                execute("sleep 999")


# ---------------------------------------------------------------------------
# execute() in bash mode
# ---------------------------------------------------------------------------

class TestBashMode:
    def setup_method(self):
        shell_module._mode = "bash"

    def teardown_method(self):
        shell_module._mode = "direct"

    def test_command_wrapped_in_bash_c(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo hello")
        args = mock_run.call_args[0][0]
        assert args == ["bash", "-c", "echo hello"]

    def test_pipe_preserved_in_command_string(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo hello | grep h")
        args = mock_run.call_args[0][0]
        assert args == ["bash", "-c", "echo hello | grep h"]

    def test_chaining_preserved(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("cd /tmp && ls")
        args = mock_run.call_args[0][0]
        assert args == ["bash", "-c", "cd /tmp && ls"]

    def test_redirect_preserved(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo hello > /tmp/test.txt")
        args = mock_run.call_args[0][0]
        assert args == ["bash", "-c", "echo hello > /tmp/test.txt"]

    def test_variable_expansion_preserved(self):
        with patch("subprocess.run", return_value=_make_run()) as mock_run:
            execute("echo $HOME")
        args = mock_run.call_args[0][0]
        assert args == ["bash", "-c", "echo $HOME"]

    def test_stderr_and_exit_code_handling_unchanged(self):
        with patch("subprocess.run", return_value=_make_run(stdout="out", stderr="err", returncode=2)):
            result = execute("cmd")
        assert "[stderr]" in result
        assert "err" in result
        assert "[exit code: 2]" in result

    def test_timeout_raises_timeout_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 120)):
            with pytest.raises(TimeoutError, match="timed out"):
                execute("sleep 999")


# ---------------------------------------------------------------------------
# Integration: registration with mode changes description
# ---------------------------------------------------------------------------

class TestRegistrationWithMode:
    def test_bash_mode_changes_description(self):
        registry = ToolRegistry()
        definition = ToolDefinition(builtin="shell", config={"mode": "bash"})
        registry.register("shell", definition)
        schema = registry.get_tool_schema("shell")
        desc = schema["function"]["description"]
        assert "bash" in desc.lower()
        assert "pipe" in desc.lower()

    def test_no_mode_keeps_default_description(self):
        registry = ToolRegistry()
        definition = ToolDefinition(builtin="shell")
        registry.register("shell", definition)
        schema = registry.get_tool_schema("shell")
        desc = schema["function"]["description"]
        assert "single process" in desc.lower() or "no pipes" in desc.lower()

    def test_user_description_override_takes_precedence(self):
        """Explicit YAML description overrides _tool_describe."""
        registry = ToolRegistry()
        definition = ToolDefinition(
            builtin="shell",
            description="Custom description",
            config={"mode": "bash"},
        )
        registry.register("shell", definition)
        schema = registry.get_tool_schema("shell")
        assert schema["function"]["description"] == "Custom description"

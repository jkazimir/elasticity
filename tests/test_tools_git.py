"""Tests for elasticity.tools.git (merge, pull, push)."""

from unittest.mock import MagicMock, patch

import pytest

from elasticity.tools.git import merge, pull, push


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_run():
    """Mock subprocess.run so tests don't need a real git repo."""
    with patch("elasticity.tools.git.subprocess.run") as mock:
        result = MagicMock()
        result.stdout = "Success"
        result.stderr = ""
        result.returncode = 0
        mock.return_value = result
        yield mock


# ---------------------------------------------------------------------------
# merge()
# ---------------------------------------------------------------------------


class TestMerge:
    def test_basic_merge(self, mock_run: MagicMock) -> None:
        merge("feature/login")
        args = mock_run.call_args[0][0]
        assert args == ["git", "merge", "--no-ff", "feature/login", "-m", "merge: feature/login"]

    def test_custom_message(self, mock_run: MagicMock) -> None:
        merge("feature/login", message="my merge commit")
        args = mock_run.call_args[0][0]
        assert "-m" in args
        assert args[args.index("-m") + 1] == "my merge commit"

    def test_no_ff_false_omits_flag(self, mock_run: MagicMock) -> None:
        merge("feature/login", no_ff=False)
        args = mock_run.call_args[0][0]
        assert "--no-ff" not in args

    def test_no_ff_false_empty_message_no_m(self, mock_run: MagicMock) -> None:
        merge("feature/login", no_ff=False)
        args = mock_run.call_args[0][0]
        assert "-m" not in args

    def test_blocks_shell_substitution_in_branch(self) -> None:
        result = merge("$(rm -rf .)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_backtick_in_branch(self) -> None:
        result = merge("`rm -rf .`")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_shell_substitution_in_message(self) -> None:
        result = merge("feature/x", message="$(bad)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_flags_in_branch(self) -> None:
        result = merge("--abort")
        assert result.startswith("Error:") and "--abort" in result

    def test_blocks_strategy_option_flag(self) -> None:
        result = merge("--strategy-option=theirs")
        assert result.startswith("Error:")

    def test_empty_branch_returns_error(self) -> None:
        result = merge("")
        assert result.startswith("Error:") and "empty" in result

    def test_long_message_returns_error(self) -> None:
        result = merge("feature/x", message="x" * 501)
        assert result.startswith("Error:") and "500" in result

    def test_repo_path_forwarded(self, mock_run: MagicMock) -> None:
        merge("feature/x", repo_path="/tmp/myrepo")
        kwargs = mock_run.call_args[1]
        assert kwargs.get("cwd") == "/tmp/myrepo"


# ---------------------------------------------------------------------------
# pull()
# ---------------------------------------------------------------------------


class TestPull:
    def test_default_pull(self, mock_run: MagicMock) -> None:
        pull()
        args = mock_run.call_args[0][0]
        assert args == ["git", "pull", "origin"]

    def test_pull_with_branch(self, mock_run: MagicMock) -> None:
        pull(branch="main")
        args = mock_run.call_args[0][0]
        assert args == ["git", "pull", "origin", "main"]

    def test_pull_custom_remote(self, mock_run: MagicMock) -> None:
        pull(remote="upstream", branch="dev")
        args = mock_run.call_args[0][0]
        assert args == ["git", "pull", "upstream", "dev"]

    def test_blocks_shell_substitution_in_remote(self) -> None:
        result = pull(remote="$(evil)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_shell_substitution_in_branch(self) -> None:
        result = pull(branch="$(evil)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_flags_in_remote(self) -> None:
        result = pull(remote="--force")
        assert result.startswith("Error:") and "--force" in result

    def test_blocks_flags_in_branch(self) -> None:
        result = pull(branch="--rebase")
        assert result.startswith("Error:") and "--rebase" in result

    def test_repo_path_forwarded(self, mock_run: MagicMock) -> None:
        pull(repo_path="/tmp/myrepo")
        kwargs = mock_run.call_args[1]
        assert kwargs.get("cwd") == "/tmp/myrepo"

    def test_timeout_is_60(self, mock_run: MagicMock) -> None:
        pull()
        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 60


# ---------------------------------------------------------------------------
# push()
# ---------------------------------------------------------------------------


class TestPush:
    def test_default_push(self, mock_run: MagicMock) -> None:
        push()
        args = mock_run.call_args[0][0]
        assert args == ["git", "push", "origin"]

    def test_push_with_branch(self, mock_run: MagicMock) -> None:
        push(branch="main")
        args = mock_run.call_args[0][0]
        assert args == ["git", "push", "origin", "main"]

    def test_push_custom_remote(self, mock_run: MagicMock) -> None:
        push(remote="upstream", branch="release")
        args = mock_run.call_args[0][0]
        assert args == ["git", "push", "upstream", "release"]

    def test_blocks_shell_substitution_in_remote(self) -> None:
        result = push(remote="$(evil)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_shell_substitution_in_branch(self) -> None:
        result = push(branch="$(evil)")
        assert result.startswith("Error:") and "shell substitution" in result

    def test_blocks_force_flag(self) -> None:
        result = push(branch="--force")
        assert result.startswith("Error:") and "--force" in result

    def test_blocks_f_flag(self) -> None:
        result = push(branch="-f")
        assert result.startswith("Error:")

    def test_blocks_force_with_lease(self) -> None:
        result = push(branch="--force-with-lease")
        assert result.startswith("Error:") and "--force" in result

    def test_blocks_flags_in_remote(self) -> None:
        result = push(remote="--mirror")
        assert result.startswith("Error:") and "--mirror" in result

    def test_repo_path_forwarded(self, mock_run: MagicMock) -> None:
        push(repo_path="/tmp/myrepo")
        kwargs = mock_run.call_args[1]
        assert kwargs.get("cwd") == "/tmp/myrepo"

    def test_timeout_is_60(self, mock_run: MagicMock) -> None:
        push()
        kwargs = mock_run.call_args[1]
        assert kwargs.get("timeout") == 60


# ---------------------------------------------------------------------------
# Builtin registration
# ---------------------------------------------------------------------------


class TestBuiltinRegistration:
    def test_git_merge_registered(self) -> None:
        from elasticity.tools.builtins import BUILTIN_TOOLS

        assert "git_merge" in BUILTIN_TOOLS
        assert BUILTIN_TOOLS["git_merge"].callable == "elasticity.tools.git.merge"

    def test_git_pull_registered(self) -> None:
        from elasticity.tools.builtins import BUILTIN_TOOLS

        assert "git_pull" in BUILTIN_TOOLS
        assert BUILTIN_TOOLS["git_pull"].callable == "elasticity.tools.git.pull"

    def test_git_push_registered(self) -> None:
        from elasticity.tools.builtins import BUILTIN_TOOLS

        assert "git_push" in BUILTIN_TOOLS
        assert BUILTIN_TOOLS["git_push"].callable == "elasticity.tools.git.push"

"""Git operation tools with safety guardrails.

These functions are registered as builtin tools so that agent configs can
reference them with ``builtin: git_*`` instead of using the raw shell tool.
Safety rules are enforced at the Python layer so agents cannot accidentally
perform destructive git operations (force push, hard reset, etc.).
"""

import shlex
import subprocess
from typing import Optional


def _run(args: list, cwd: str, timeout: int = 30) -> str:
    """Run a git command and return combined stdout/stderr with exit code."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
        timeout=timeout,
    )
    out = result.stdout
    if result.stderr:
        out += result.stderr if out.endswith("\n") or not out else f"\n{result.stderr}"
    if result.returncode != 0:
        out += f"\n[exit code: {result.returncode}]"
    return out.strip() or "(no output)"


def status(path: str = ".") -> str:
    """Show working tree status (git status --short)."""
    return _run(["git", "status", "--short"], cwd=path)


_SAFE_DIFF_FLAGS = frozenset({"--cached", "--staged", "--stat", "--name-only", "--name-status"})


def diff(path: str = ".", ref: str = "") -> str:
    """Show diff of working changes or between refs.

    Args:
        path: Repository root path.
        ref: Optional ref, e.g. ``HEAD`` or ``main..HEAD``. If empty, shows
             unstaged working-tree diff. Arbitrary ``--flag`` options are blocked
             to prevent flag injection (e.g. ``--output=/path``); only a known
             safe subset is permitted.
    """
    args = ["git", "diff"]
    if ref:
        for token in ref.split():
            if token.startswith("-"):
                if token not in _SAFE_DIFF_FLAGS:
                    return (
                        f"Error: flag '{token}' is not allowed in git_diff. "
                        f"Allowed flags: {', '.join(sorted(_SAFE_DIFF_FLAGS))}."
                    )
        args.extend(ref.split())
    return _run(args, cwd=path)


def log(path: str = ".", n: int = 10) -> str:
    """Show recent commit log (one line per commit with graph decoration)."""
    return _run(["git", "log", f"-{n}", "--oneline", "--graph", "--decorate"], cwd=path)


def create_branch(branch: str, path: str = ".") -> str:
    """Create and checkout a new branch.

    Branch name must start with ``feature/``, ``fix/``, or ``chore/`` to
    prevent accidental creation of branches with misleading names.

    Args:
        branch: Branch name, e.g. ``feature/add-login``.
        path: Repository root path.
    """
    allowed_prefixes = ("feature/", "fix/", "chore/", "docs/", "test/", "refactor/")
    if not any(branch.startswith(p) for p in allowed_prefixes):
        return (
            f"Error: branch name '{branch}' must start with one of: "
            + ", ".join(allowed_prefixes)
            + ". This prevents accidental creation of ambiguously-named branches."
        )
    return _run(["git", "checkout", "-b", branch], cwd=path)


def checkout(ref: str, path: str = ".") -> str:
    """Checkout an existing branch or file.

    Refuses refs that contain destructive flags (``--force``, ``-f``,
    ``--hard``) to prevent accidental data loss.

    Args:
        ref: Branch name or commit ref.
        path: Repository root path.
    """
    blocked = ("--force", "-f", "--hard", "--no-verify")
    for flag in blocked:
        if flag in ref.split():
            return (
                f"Error: '{flag}' is not allowed via git_checkout. "
                "Use the shell tool directly if you intentionally need this flag."
            )
    # Prevent checkout of ref strings that look like they're trying to run
    # subcommands (e.g. "$(rm -rf .)").
    if "$(" in ref or "`" in ref:
        return "Error: shell substitution is not allowed in ref names."
    return _run(["git", "checkout", ref], cwd=path)


def add(paths: str, repo_path: str = ".") -> str:
    """Stage files for commit.

    Args:
        paths: Space-separated file paths to stage. Use ``.`` to stage all
               changes, but be intentional — avoid staging unrelated files.
        repo_path: Repository root path.
    """
    path_list = shlex.split(paths)
    return _run(["git", "add", "--"] + path_list, cwd=repo_path)


def worktree_add(path: str, branch: str, repo_path: str = ".") -> str:
    """Create a git worktree with a new branch.

    Each worktree is a separate working directory backed by the same repo.
    Use this when multiple agents need to work on different branches
    concurrently without stepping on each other's checkout.

    Args:
        path: Directory path for the worktree (e.g. ``./workspaces/task1``).
        branch: Branch name, must follow naming conventions.
        repo_path: Repository root path.
    """
    allowed_prefixes = ("feature/", "fix/", "chore/", "docs/", "test/", "refactor/")
    if not any(branch.startswith(p) for p in allowed_prefixes):
        return (
            f"Error: branch name '{branch}' must start with one of: "
            + ", ".join(allowed_prefixes)
            + ". This prevents accidental creation of ambiguously-named branches."
        )
    if "$(" in branch or "`" in branch or "$(" in path or "`" in path:
        return "Error: shell substitution is not allowed in branch names or paths."
    return _run(["git", "worktree", "add", path, "-b", branch], cwd=repo_path)


def worktree_remove(path: str, repo_path: str = ".") -> str:
    """Remove a git worktree after work is complete.

    The branch created by the worktree is preserved — only the working
    directory is removed.

    Args:
        path: Directory path of the worktree to remove.
        repo_path: Repository root path.
    """
    if "$(" in path or "`" in path:
        return "Error: shell substitution is not allowed in paths."
    return _run(["git", "worktree", "remove", path], cwd=repo_path)


def commit(message: str, repo_path: str = ".") -> str:
    """Create a commit with a conventional commit message.

    Refuses empty messages or messages longer than 500 characters to catch
    accidental misuse.

    Args:
        message: Commit message. Use conventional commits format:
                 ``feat: add login endpoint`` or ``fix: handle null token``.
        repo_path: Repository root path.
    """
    if not message.strip():
        return "Error: commit message must not be empty."
    if len(message) > 500:
        return (
            f"Error: commit message is {len(message)} characters, which is unusually "
            "long (limit: 500). Please shorten it."
        )
    # Refuse messages that look like they're trying to inject shell commands
    if "$(" in message or "`" in message:
        return "Error: shell substitution is not allowed in commit messages."
    return _run(["git", "commit", "-m", message], cwd=repo_path)


def merge(branch: str, message: str = "", no_ff: bool = True, repo_path: str = ".") -> str:
    """Merge a branch into the current branch.

    Uses ``--no-ff`` by default to always create a merge commit.
    When ``message`` is empty and ``no_ff`` is True, auto-generates
    ``merge: <branch>`` to avoid opening an interactive editor.

    Args:
        branch: Branch name to merge.
        message: Merge commit message. Auto-generated if empty.
        no_ff: Use ``--no-ff`` to always create a merge commit (default: True).
        repo_path: Repository root path.
    """
    if not branch.strip():
        return "Error: branch name must not be empty."
    if "$(" in branch or "`" in branch:
        return "Error: shell substitution is not allowed in branch names."
    for token in branch.split():
        if token.startswith("-"):
            return (
                f"Error: '{token}' looks like a flag and is not allowed in the branch "
                "argument. Pass options via the no_ff parameter instead."
            )
    if "$(" in message or "`" in message:
        return "Error: shell substitution is not allowed in merge messages."
    if len(message) > 500:
        return (
            f"Error: merge message is {len(message)} characters (limit: 500). "
            "Please shorten it."
        )
    args = ["git", "merge"]
    if no_ff:
        args.append("--no-ff")
    args.append(branch)
    effective_message = message.strip() or (f"merge: {branch}" if no_ff else "")
    if effective_message:
        args.extend(["-m", effective_message])
    return _run(args, cwd=repo_path)


def pull(remote: str = "origin", branch: str = "", repo_path: str = ".") -> str:
    """Pull changes from a remote repository.

    Refuses any flag-like tokens in ``remote`` or ``branch`` to prevent
    unintended operations (``--force``, ``--rebase``, etc.).

    Args:
        remote: Remote name, e.g. ``origin``.
        branch: Branch to pull. Empty pulls the current tracking branch.
        repo_path: Repository root path.
    """
    if "$(" in remote or "`" in remote:
        return "Error: shell substitution is not allowed in remote names."
    for token in remote.split():
        if token.startswith("-"):
            return f"Error: '{token}' is not allowed in the remote argument."
    if "$(" in branch or "`" in branch:
        return "Error: shell substitution is not allowed in branch names."
    for token in branch.split():
        if token.startswith("-"):
            return f"Error: '{token}' is not allowed in the branch argument."
    args = ["git", "pull", remote]
    if branch.strip():
        args.append(branch.strip())
    return _run(args, cwd=repo_path, timeout=60)


def push(remote: str = "origin", branch: str = "", repo_path: str = ".") -> str:
    """Push commits to a remote repository.

    Force push (``--force``, ``-f``, ``--force-with-lease``) and other
    destructive flags are blocked to prevent data loss.

    Args:
        remote: Remote name, e.g. ``origin``.
        branch: Branch to push. Empty pushes the current branch.
        repo_path: Repository root path.
    """
    if "$(" in remote or "`" in remote:
        return "Error: shell substitution is not allowed in remote names."
    for token in remote.split():
        if token.startswith("-"):
            return f"Error: '{token}' is not allowed in the remote argument."
    if "$(" in branch or "`" in branch:
        return "Error: shell substitution is not allowed in branch names."
    for token in branch.split():
        if token.startswith("-"):
            return (
                f"Error: '{token}' is not allowed in the branch argument. "
                "Force push (--force, -f, --force-with-lease) is blocked to prevent data loss."
            )
    args = ["git", "push", remote]
    if branch.strip():
        args.append(branch.strip())
    return _run(args, cwd=repo_path, timeout=60)

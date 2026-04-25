"""Built-in tool registry."""

from typing import Dict
from ..config.schema import ToolDefinition, ParameterSchema


# Registry mapping short names to ToolDefinition objects
BUILTIN_TOOLS: Dict[str, ToolDefinition] = {
    "file_read": ToolDefinition(
        description="Read the contents of a file, optionally restricted to a line range",
        callable="elasticity.tools.filesystem.read",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Path to the file to read",
            ),
            "start_line": ParameterSchema(
                type="integer",
                required=False,
                default=0,
                description="First line to return, 1-based. 0 means start of file.",
            ),
            "end_line": ParameterSchema(
                type="integer",
                required=False,
                default=0,
                description="Last line to return, 1-based inclusive. 0 means end of file.",
            ),
        },
    ),
    "file_write": ToolDefinition(
        description="Write content to a file",
        callable="elasticity.tools.filesystem.write",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Path to the file to write",
            ),
            "content": ParameterSchema(
                type="string",
                required=True,
                description="Content to write to the file",
            ),
        },
    ),
    "file_edit": ToolDefinition(
        description="Replace an exact string in a file (must appear exactly once)",
        callable="elasticity.tools.filesystem.edit",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Path to the file to edit",
            ),
            "old_string": ParameterSchema(
                type="string",
                required=True,
                description="Exact text to find and replace. Must appear exactly once in the file.",
            ),
            "new_string": ParameterSchema(
                type="string",
                required=True,
                description="Text to replace old_string with",
            ),
        },
    ),
    "file_list": ToolDefinition(
        description="List files and directories in a directory",
        callable="elasticity.tools.filesystem.list_dir",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Path to the directory to list",
            ),
        },
    ),
    "file_grep": ToolDefinition(
        description="Search file contents for a pattern (regex supported)",
        callable="elasticity.tools.filesystem.grep",
        parameters={
            "pattern": ParameterSchema(
                type="string",
                required=True,
                description="Search pattern (regex)",
            ),
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Directory or file to search",
            ),
            "glob": ParameterSchema(
                type="string",
                required=False,
                default="",
                description="File glob filter, e.g. '*.py'",
            ),
        },
    ),
    "http_request": ToolDefinition(
        description="Make an HTTP request to a URL",
        callable="elasticity.tools.http.request",
        parameters={
            "url": ParameterSchema(
                type="string",
                required=True,
                description="URL to request",
            ),
            "method": ParameterSchema(
                type="string",
                required=False,
                default="GET",
                description="HTTP method (GET, POST, PUT, DELETE, etc.)",
            ),
            "body": ParameterSchema(
                type="string",
                required=False,
                description="Request body (for POST/PUT)",
            ),
            "headers": ParameterSchema(
                type="string",
                required=False,
                description="JSON string of headers to include",
            ),
        },
    ),
    "shell": ToolDefinition(
        description="Execute a shell command (single process, no pipes/redirects/chaining)",
        callable="elasticity.tools.shell.execute",
        parameters={
            "command": ParameterSchema(
                type="string",
                required=True,
                description="Shell command to execute",
            ),
            "timeout": ParameterSchema(
                type="integer",
                required=False,
                default=120,
                description="Timeout in seconds",
            ),
        },
    ),
    "memory_store": ToolDefinition(
        description="Store a key-value pair in memory",
        callable="elasticity.tools.memory.store",
        parameters={
            "key": ParameterSchema(
                type="string",
                required=True,
                description="Memory key",
            ),
            "value": ParameterSchema(
                type="string",
                required=True,
                description="Memory value",
            ),
        },
    ),
    "memory_retrieve": ToolDefinition(
        description="Retrieve memories by query",
        callable="elasticity.tools.memory.retrieve",
        parameters={
            "query": ParameterSchema(
                type="string",
                required=True,
                description="Search query",
            ),
        },
    ),
    "web_search": ToolDefinition(
        description="Search the web using a configured search provider",
        callable="elasticity.tools.web_search.search",
        parameters={
            "query": ParameterSchema(
                type="string",
                required=True,
                description="Search query",
            ),
        },
    ),
    "ask_user": ToolDefinition(
        description="Ask the user a clarifying question and return their answer",
        callable="elasticity.tools.ask_user.ask",
        parameters={
            "question": ParameterSchema(
                type="string",
                required=True,
                description="The question to ask the user",
            ),
        },
    ),
    "file_glob": ToolDefinition(
        description="Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts')",
        callable="elasticity.tools.filesystem.glob",
        parameters={
            "pattern": ParameterSchema(
                type="string",
                required=True,
                description="Glob pattern, e.g. '**/*.py' or 'tests/test_*.py'",
            ),
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Base directory to search from",
            ),
        },
    ),
    "file_delete": ToolDefinition(
        description="Delete a file or empty directory",
        callable="elasticity.tools.filesystem.delete",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Path to the file or empty directory to delete",
            ),
        },
    ),
    "file_move": ToolDefinition(
        description="Move or rename a file or directory",
        callable="elasticity.tools.filesystem.move",
        parameters={
            "source": ParameterSchema(
                type="string",
                required=True,
                description="Source path",
            ),
            "destination": ParameterSchema(
                type="string",
                required=True,
                description="Destination path",
            ),
        },
    ),
    "git_status": ToolDefinition(
        description="Show working tree status (git status --short)",
        callable="elasticity.tools.git.status",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_diff": ToolDefinition(
        description="Show diff of working changes or between refs",
        callable="elasticity.tools.git.diff",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
            "ref": ParameterSchema(
                type="string",
                required=False,
                default="",
                description="Optional ref, e.g. 'HEAD' or 'main..HEAD'. Empty shows unstaged diff.",
            ),
        },
    ),
    "git_log": ToolDefinition(
        description="Show recent commit log (one line per commit with graph)",
        callable="elasticity.tools.git.log",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
            "n": ParameterSchema(
                type="integer",
                required=False,
                default=10,
                description="Number of commits to show",
            ),
        },
    ),
    "git_create_branch": ToolDefinition(
        description=(
            "Create and checkout a new branch. Branch name must start with "
            "feature/, fix/, chore/, docs/, test/, or refactor/."
        ),
        callable="elasticity.tools.git.create_branch",
        parameters={
            "branch": ParameterSchema(
                type="string",
                required=True,
                description="Branch name, e.g. 'feature/add-login' or 'fix/null-token'",
            ),
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_checkout": ToolDefinition(
        description=(
            "Checkout an existing branch or commit ref. "
            "Refuses destructive flags (--force, --hard) to prevent data loss."
        ),
        callable="elasticity.tools.git.checkout",
        parameters={
            "ref": ParameterSchema(
                type="string",
                required=True,
                description="Branch name or commit ref to checkout",
            ),
            "path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_add": ToolDefinition(
        description="Stage files for commit",
        callable="elasticity.tools.git.add",
        parameters={
            "paths": ParameterSchema(
                type="string",
                required=True,
                description="Space-separated file paths to stage, or '.' to stage all changes",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_commit": ToolDefinition(
        description=(
            "Create a commit with a conventional commit message. "
            "Use format: 'feat: description', 'fix: description', etc."
        ),
        callable="elasticity.tools.git.commit",
        parameters={
            "message": ParameterSchema(
                type="string",
                required=True,
                description="Commit message in conventional commits format",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_merge": ToolDefinition(
        description=(
            "Merge a branch into the current branch. "
            "Uses --no-ff by default to always create a merge commit. "
            "Refuses flag injection in branch names to prevent destructive operations."
        ),
        callable="elasticity.tools.git.merge",
        parameters={
            "branch": ParameterSchema(
                type="string",
                required=True,
                description="Branch name to merge, e.g. 'feature/add-login'",
            ),
            "message": ParameterSchema(
                type="string",
                required=False,
                default="",
                description="Merge commit message. If empty, auto-generates 'merge: <branch>'.",
            ),
            "no_ff": ParameterSchema(
                type="boolean",
                required=False,
                default=True,
                description="Use --no-ff to always create a merge commit (default: true)",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_pull": ToolDefinition(
        description=(
            "Pull changes from a remote repository. "
            "Refuses flags (--force, --rebase, etc.) to prevent unintended operations."
        ),
        callable="elasticity.tools.git.pull",
        parameters={
            "remote": ParameterSchema(
                type="string",
                required=False,
                default="origin",
                description="Remote name, e.g. 'origin'",
            ),
            "branch": ParameterSchema(
                type="string",
                required=False,
                default="",
                description="Branch to pull. Empty pulls the current tracking branch.",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_push": ToolDefinition(
        description=(
            "Push commits to a remote repository. "
            "Force push (--force, -f, --force-with-lease) is blocked to prevent data loss."
        ),
        callable="elasticity.tools.git.push",
        parameters={
            "remote": ParameterSchema(
                type="string",
                required=False,
                default="origin",
                description="Remote name, e.g. 'origin'",
            ),
            "branch": ParameterSchema(
                type="string",
                required=False,
                default="",
                description="Branch to push. Empty pushes the current branch.",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_worktree_add": ToolDefinition(
        description=(
            "Create an isolated git worktree with a new branch. "
            "Use this when agents work on different branches concurrently "
            "to avoid checkout races on the shared working tree. "
            "Branch name must start with feature/, fix/, chore/, docs/, test/, or refactor/."
        ),
        callable="elasticity.tools.git.worktree_add",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Directory path for the worktree, e.g. './workspaces/task1'",
            ),
            "branch": ParameterSchema(
                type="string",
                required=True,
                description="Branch name, e.g. 'feature/add-login'",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
    "git_worktree_remove": ToolDefinition(
        description=(
            "Remove a git worktree after work is complete. "
            "The branch is preserved — only the working directory is removed."
        ),
        callable="elasticity.tools.git.worktree_remove",
        parameters={
            "path": ParameterSchema(
                type="string",
                required=True,
                description="Directory path of the worktree to remove",
            ),
            "repo_path": ParameterSchema(
                type="string",
                required=False,
                default=".",
                description="Repository root path",
            ),
        },
    ),
}


def get_builtin_tool(name: str) -> ToolDefinition:
    """Get a built-in tool definition by name.

    Args:
        name: Built-in tool name

    Returns:
        ToolDefinition for the built-in tool

    Raises:
        KeyError: If the built-in tool name is not found
    """
    if name not in BUILTIN_TOOLS:
        raise KeyError(f"Unknown built-in tool: {name}")
    return BUILTIN_TOOLS[name]


def list_builtin_tools() -> list[str]:
    """List all available built-in tool names."""
    return list(BUILTIN_TOOLS.keys())

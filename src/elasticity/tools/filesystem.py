"""Filesystem tools for reading, writing, listing, editing, and searching files."""

import fnmatch
import re
import shutil
from pathlib import Path
from typing import Dict, Any, Optional

# Optional workspace root.  When set (via _tool_init), all path arguments are
# resolved and checked to ensure they remain inside this directory.
_workspace_root: Optional[Path] = None


def _tool_init(config: Dict[str, Any]) -> None:
    """Called once by ToolRegistry when this module is first loaded."""
    global _workspace_root
    root = config.get("workspace_root") if config else None
    if root:
        _workspace_root = Path(root).resolve()
    else:
        _workspace_root = None


def _check_path(path: Path) -> Path:
    """Resolve *path* and enforce workspace_root restriction if configured.

    Returns the resolved Path if it is allowed.

    Raises:
        PermissionError: If workspace_root is set and path is outside it.
    """
    resolved = path.resolve()
    if _workspace_root is not None:
        try:
            resolved.relative_to(_workspace_root)
        except ValueError:
            raise PermissionError(
                f"Path '{path}' is outside the allowed workspace '{_workspace_root}'. "
                "Set workspace_root in the tool config to change the restriction."
            )
    return resolved


def read(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read the contents of a file, optionally restricted to a line range.

    Args:
        path: Path to the file to read
        start_line: First line to return, 1-based. 0 means start of file.
        end_line: Last line to return, 1-based inclusive. 0 means end of file.

    Returns:
        File contents (or selected lines) as a string. When a range is
        specified lines are prefixed with their 1-based line number.

    Raises:
        FileNotFoundError: If the file does not exist
        ValueError: If the path is a directory or the range is invalid
    """
    file_path = _check_path(Path(path))
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")

    content = file_path.read_text(encoding="utf-8")

    if start_line == 0 and end_line == 0:
        return content

    lines = content.splitlines(keepends=True)
    total = len(lines)

    # Normalise to 1-based, clamped to [1, total]
    s = max(1, start_line) if start_line > 0 else 1
    e = min(total, end_line) if end_line > 0 else total

    if s > e:
        raise ValueError(
            f"Invalid range: start_line={start_line} is after end_line={end_line}"
        )

    selected = lines[s - 1 : e]
    return "".join(f"{s + i}\t{line}" for i, line in enumerate(selected))


def write(path: str, content: str) -> str:
    """Write content to a file.

    Args:
        path: Path to the file to write
        content: Content to write to the file

    Returns:
        Success message

    Raises:
        PermissionError: If the file cannot be written
    """
    file_path = _check_path(Path(path))
    # Create parent directories if they don't exist
    file_path.parent.mkdir(parents=True, exist_ok=True)

    file_path.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content)} bytes to {path}"


def edit(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string in a file.

    Finds *old_string* in the file exactly once and replaces it with
    *new_string*, then writes the file back. The replacement is reported
    with the (1-based) line number where the match starts.

    Args:
        path: Path to the file to edit
        old_string: Exact text to find. Must appear exactly once.
        new_string: Text to replace it with.

    Returns:
        Success message including the line number of the replacement.

    Raises:
        FileNotFoundError: If the file does not exist
        ValueError: If old_string is not found, or appears more than once
    """
    file_path = _check_path(Path(path))
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")

    content = file_path.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in {path}")
    if count > 1:
        raise ValueError(
            f"old_string appears {count} times in {path} — must be unique. "
            "Provide more context to make it unambiguous."
        )

    # Determine the 1-based line number of the match
    match_pos = content.index(old_string)
    line_number = content[:match_pos].count("\n") + 1

    new_content = content.replace(old_string, new_string, 1)
    file_path.write_text(new_content, encoding="utf-8")
    return f"Replaced at line {line_number} in {path}"


def list_dir(path: str) -> str:
    """List files and directories in a directory.

    Args:
        path: Path to the directory to list

    Returns:
        Newline-separated list of files and directories

    Raises:
        FileNotFoundError: If the directory does not exist
        NotADirectoryError: If the path is not a directory
    """
    dir_path = _check_path(Path(path))
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {path}")

    items = []
    for item in sorted(dir_path.iterdir()):
        item_type = "DIR" if item.is_dir() else "FILE"
        items.append(f"{item_type}\t{item.name}")

    return "\n".join(items) if items else "(empty directory)"


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".tox", ".venv", "venv"}
_MAX_MATCHES = 200


def grep(pattern: str, path: str = ".", glob: str = "") -> str:
    """Search file contents recursively for a regex pattern.

    Args:
        pattern: Regular expression to search for
        path: Directory (or file) to search from
        glob: Optional filename glob filter, e.g. '*.py'

    Returns:
        Matching lines formatted as 'file:line_number:content', up to 200 matches.
        Returns a message if no matches are found.
    """
    search_path = _check_path(Path(path))
    if not search_path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc

    matches: list[str] = []

    def _search_file(file_path: Path) -> None:
        try:
            text = file_path.read_bytes()
            # Skip binary files: look for null bytes in the first 8 KB
            if b"\x00" in text[:8192]:
                return
            content = text.decode("utf-8", errors="replace")
        except (OSError, PermissionError):
            return

        for lineno, line in enumerate(content.splitlines(), start=1):
            if compiled.search(line):
                matches.append(f"{file_path}:{lineno}:{line}")
                if len(matches) >= _MAX_MATCHES:
                    return

    def _walk(dir_path: Path) -> None:
        for item in sorted(dir_path.iterdir()):
            if len(matches) >= _MAX_MATCHES:
                return
            if item.is_dir():
                if item.name not in _SKIP_DIRS:
                    _walk(item)
            elif item.is_file():
                if glob and not fnmatch.fnmatch(item.name, glob):
                    continue
                _search_file(item)

    if search_path.is_file():
        _search_file(search_path)
    else:
        _walk(search_path)

    if not matches:
        return f"No matches found for pattern '{pattern}'"

    result = "\n".join(matches)
    if len(matches) >= _MAX_MATCHES:
        result += f"\n(results truncated at {_MAX_MATCHES} matches)"
    return result


_MAX_GLOB_RESULTS = 500


def glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern, e.g. '**/*.py' or 'tests/test_*.py'
        path: Base directory to search from (default: current directory)

    Returns:
        Newline-separated list of matching paths, up to 500 results.
        Returns a message if no matches are found.

    Raises:
        FileNotFoundError: If the base path does not exist
    """
    base = _check_path(Path(path))
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    matches = []
    for match in sorted(base.glob(pattern)):
        relative = match.relative_to(base)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        matches.append(str(match))
        if len(matches) >= _MAX_GLOB_RESULTS:
            break

    if not matches:
        return f"No files matching '{pattern}' in {path}"

    result = "\n".join(matches)
    if len(matches) >= _MAX_GLOB_RESULTS:
        result += f"\n(results truncated at {_MAX_GLOB_RESULTS} matches)"
    return result


def delete(path: str) -> str:
    """Delete a file or empty directory.

    For safety, non-empty directories are rejected. Use the shell tool
    for recursive directory deletion.

    Args:
        path: Path to the file or empty directory to delete

    Returns:
        Success message

    Raises:
        FileNotFoundError: If the path does not exist
        ValueError: If the path is a non-empty directory
    """
    p = _check_path(Path(path))
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if p.is_dir():
        try:
            p.rmdir()
        except OSError as exc:
            raise ValueError(
                f"Cannot delete non-empty directory '{path}'. "
                "Use the shell tool for recursive deletion."
            ) from exc
    else:
        p.unlink()
    return f"Deleted {path}"


def move(source: str, destination: str) -> str:
    """Move or rename a file or directory.

    Parent directories of the destination are created automatically.

    Args:
        source: Source path
        destination: Destination path

    Returns:
        Success message

    Raises:
        FileNotFoundError: If the source does not exist
    """
    src = _check_path(Path(source))
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    dst = _check_path(Path(destination))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {source} to {destination}"

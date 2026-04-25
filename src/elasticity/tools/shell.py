"""Shell command execution tool."""

import logging
import subprocess
import shlex
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Module-level execution mode: "direct" (shlex.split) or "bash" (bash -c)
_mode: str = "direct"

_DESCRIPTIONS: Dict[str, str] = {
    "direct": "Execute a shell command (single process, no pipes/redirects/chaining)",
    "bash": (
        "Execute a command via bash. Supports full shell syntax: "
        "pipes (|), redirects (>, >>), chaining (&&, ||), "
        "variable expansion, cd, and all bash features."
    ),
}


def _tool_init(config: Dict[str, Any]) -> None:
    """Called once by ToolRegistry when this module is first loaded."""
    global _mode
    if config:
        mode = config.get("mode", "direct")
        if mode not in ("direct", "bash"):
            raise ValueError(f"Invalid shell mode '{mode}'. Must be 'direct' or 'bash'.")
        _mode = mode


def _tool_describe(config: Dict[str, Any]) -> str:
    """Return the tool description appropriate for the configured mode."""
    mode = (config or {}).get("mode", "direct")
    return _DESCRIPTIONS.get(mode, _DESCRIPTIONS["direct"])


def execute(command: str, timeout: int = 120) -> str:
    """Execute a shell command.

    Args:
        command: Shell command to execute
        timeout: Timeout in seconds

    Returns:
        Command output (stdout) as a string

    Raises:
        subprocess.TimeoutExpired: If the command times out
        subprocess.CalledProcessError: If the command returns a non-zero exit code
    """
    logger.info("shell_execute", command=command, timeout=timeout)
    try:
        if _mode == "bash":
            args = ["bash", "-c", command]
        else:
            # Use shlex to safely split the command
            args = shlex.split(command)
        
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # Don't raise on non-zero exit codes, return stderr instead
        )
        
        # Combine stdout and stderr
        output = result.stdout
        if result.stderr:
            output = f"{output}\n[stderr]\n{result.stderr}"
        
        # Include exit code in output
        if result.returncode != 0:
            output = f"{output}\n[exit code: {result.returncode}]"
        
        return output
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Command timed out after {timeout} seconds: {command}") from e
    except Exception as e:
        raise Exception(f"Command execution failed: {e}") from e

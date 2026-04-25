"""MCP client adapter using the official MCP Python SDK.

Connects to a single MCP server, discovers its tools, and executes tool calls.
"""

import os
import re
from typing import Any, Dict, List, Optional

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Tool as MCPTool
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from ..errors import ToolError


def _interpolate_env_vars(value: str) -> str:
    """Replace ${VAR} patterns with values from os.environ."""
    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    return re.sub(r"\$\{([^}]+)\}", replace, value)


class MCPClient:
    """Manages a connection to a single MCP server via stdio transport.

    Lifecycle:
    1. Call ``connect()`` to start the server process and discover tools.
    2. Use ``tools`` to get discovered tool definitions.
    3. Call ``call_tool(name, arguments)`` to invoke a tool.
    4. Call ``close()`` to shut down the server process.
    """

    def __init__(
        self,
        server_name: str,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
    ):
        if not MCP_AVAILABLE:
            raise ToolError(
                "The 'mcp' package is required for MCP server support.\n"
                "Install it with: pip install elasticity[mcp]"
            )
        self.server_name = server_name
        self.command = command
        self._raw_env = env or {}
        self._session: Optional[Any] = None
        self._exit_stack: Optional[Any] = None
        self._tools: List[MCPTool] = []

    @property
    def tools(self) -> List[Any]:
        """List of MCP Tool objects discovered from the server."""
        return list(self._tools)

    # Minimal set of env vars that most subprocess tools expect.
    _PASS_THROUGH_ENV = frozenset({"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP"})

    async def connect(self) -> None:
        """Start the MCP server and discover its tools."""
        import contextlib

        # Resolve environment variables from the config.
        resolved_env = {k: _interpolate_env_vars(v) for k, v in self._raw_env.items()}

        # Build a minimal environment: only pass-through vars + explicitly configured vars.
        # This avoids leaking API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) to
        # MCP server subprocesses that don't need them.
        minimal_env = {k: v for k, v in os.environ.items() if k in self._PASS_THROUGH_ENV}
        full_env = {**minimal_env, **resolved_env}

        server_params = StdioServerParameters(
            command=self.command[0],
            args=self.command[1:],
            env=full_env,
        )

        self._exit_stack = contextlib.AsyncExitStack()
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

        # Discover tools
        result = await self._session.list_tools()
        self._tools = result.tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string.

        Args:
            tool_name: The bare tool name (without server prefix)
            arguments: Tool arguments

        Returns:
            String result from the tool

        Raises:
            ToolError: If the session is not connected or the tool call fails
        """
        if self._session is None:
            raise ToolError(f"MCP server '{self.server_name}' is not connected")

        try:
            result = await self._session.call_tool(tool_name, arguments)
            # Extract text content from result
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                else:
                    parts.append(str(content))
            return "\n".join(parts) if parts else ""
        except Exception as e:
            raise ToolError(f"MCP tool '{tool_name}' on server '{self.server_name}' failed: {e}") from e

    async def close(self) -> None:
        """Shut down the MCP server process."""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

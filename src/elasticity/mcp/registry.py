"""MCP Registry: manages multiple MCP server connections and tool discovery.

MCPRegistry is the bridge between MCP servers and Elasticity's ToolRegistry.
It starts servers, discovers tools, and registers them under dotted names
(e.g., ``github.search_repositories``).
"""

import json
import re
from typing import Any, Dict, List, Optional

from ..config.schema import MCPServerDefinition, ParameterSchema, ToolDefinition
from ..errors import ToolError
from elasticity.mcp.client import MCPClient

# Separator between server name and tool name in the registry
_SEP = "."


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Compose the full tool name as ``server_name.tool_name``."""
    return f"{server_name}{_SEP}{tool_name}"


def _json_schema_to_param_schemas(
    json_schema: Optional[Dict[str, Any]]
) -> Dict[str, ParameterSchema]:
    """Convert a JSON Schema object (properties/required) to Elasticity ParameterSchema dicts."""
    if not json_schema:
        return {}

    properties = json_schema.get("properties", {})
    required_fields = set(json_schema.get("required", []))
    result = {}

    type_map = {
        "string": "string",
        "integer": "integer",
        "number": "float",
        "boolean": "boolean",
    }

    for prop_name, prop_schema in properties.items():
        json_type = prop_schema.get("type", "string")
        elasticity_type = type_map.get(json_type, "string")
        result[prop_name] = ParameterSchema(
            type=elasticity_type,
            required=prop_name in required_fields,
            description=prop_schema.get("description"),
        )

    return result


class MCPRegistry:
    """Manages the lifecycle of MCP servers and exposes their tools.

    Usage::

        registry = MCPRegistry(config.mcp_servers)
        await registry.start()

        # Register tools into Elasticity's ToolRegistry
        registry.register_tools(tool_registry)

        # ... run orchestration ...

        await registry.stop()
    """

    def __init__(self, server_definitions: Dict[str, MCPServerDefinition]):
        self._definitions = server_definitions
        self._clients: Dict[str, MCPClient] = {}
        # Maps full tool name -> (server_name, bare_tool_name)
        self._tool_map: Dict[str, tuple[str, str]] = {}

    @property
    def tool_names(self) -> List[str]:
        """All registered MCP tool names (as ``server.tool`` strings)."""
        return list(self._tool_map.keys())

    async def start(self) -> None:
        """Start all configured MCP servers and discover their tools."""
        for server_name, defn in self._definitions.items():
            client = MCPClient(
                server_name=server_name,
                command=defn.command,
                env=defn.env,
            )
            try:
                await client.connect()
                self._clients[server_name] = client

                for mcp_tool in client.tools:
                    full_name = _mcp_tool_name(server_name, mcp_tool.name)
                    self._tool_map[full_name] = (server_name, mcp_tool.name)
            except Exception as e:
                # Log but don't crash -- server failures should not prevent startup
                import structlog
                structlog.get_logger(__name__).warning(
                    "Failed to connect to MCP server",
                    server=server_name,
                    error=str(e),
                )

    async def stop(self) -> None:
        """Shut down all connected MCP servers."""
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self._tool_map.clear()

    def register_tools(self, tool_registry: Any) -> None:
        """Register all discovered MCP tools into a ToolRegistry.

        Each tool is registered under ``server_name.tool_name`` with a special
        ``_mcp_server`` config field that the ToolRegistry uses to route
        invocations back through this registry.
        """
        for server_name, client in self._clients.items():
            for mcp_tool in client.tools:
                full_name = _mcp_tool_name(server_name, mcp_tool.name)
                input_schema = getattr(mcp_tool, "inputSchema", None) or {}
                params = _json_schema_to_param_schemas(input_schema)

                tool_def = ToolDefinition(
                    description=getattr(mcp_tool, "description", f"MCP tool: {full_name}") or f"MCP tool: {full_name}",
                    callable="_mcp_",  # sentinel; invocation routed via MCPRegistry
                    parameters=params,
                    config={"_mcp_server": server_name, "_mcp_tool": mcp_tool.name},
                )
                # Bypass validation of the sentinel callable
                tool_registry.register_mcp(full_name, tool_def, self)

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool on the named MCP server.

        Args:
            server_name: Name of the MCP server (e.g., ``"github"``)
            tool_name: Bare tool name (e.g., ``"search_repositories"``)
            arguments: Tool arguments dict

        Returns:
            Tool result as a string

        Raises:
            ToolError: If the server is not connected or the call fails
        """
        client = self._clients.get(server_name)
        if client is None:
            raise ToolError(
                f"MCP server '{server_name}' is not connected. "
                "Make sure it is listed in mcp_servers and started successfully."
            )
        return await client.call_tool(tool_name, arguments)

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Return True if the tool name belongs to an MCP server."""
        return tool_name in self._tool_map

    def get_tool_server(self, full_name: str) -> Optional[tuple[str, str]]:
        """Return (server_name, bare_tool_name) for a full MCP tool name."""
        return self._tool_map.get(full_name)

"""Tests for MCP integration."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from elasticity.config.schema import MCPServerDefinition
from elasticity.mcp.registry import MCPRegistry, _json_schema_to_param_schemas, _mcp_tool_name
from elasticity.mcp.client import _interpolate_env_vars
from elasticity.runtime.tools import ToolRegistry
from elasticity.errors import ToolError


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def test_mcp_tool_name_format():
    assert _mcp_tool_name("github", "search") == "github.search"
    assert _mcp_tool_name("fs", "read_file") == "fs.read_file"


def test_interpolate_env_vars(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    assert _interpolate_env_vars("${MY_TOKEN}") == "secret123"
    assert _interpolate_env_vars("prefix_${MY_TOKEN}_suffix") == "prefix_secret123_suffix"


def test_interpolate_env_vars_missing_var():
    """Missing env vars should leave the placeholder as-is."""
    result = _interpolate_env_vars("${MISSING_VAR_ABC}")
    assert result == "${MISSING_VAR_ABC}"


def test_json_schema_to_param_schemas_basic():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "description": "Max results"},
        },
        "required": ["query"],
    }
    params = _json_schema_to_param_schemas(schema)
    assert "query" in params
    assert "limit" in params
    assert params["query"].required is True
    assert params["limit"].required is False
    assert params["query"].type == "string"
    assert params["limit"].type == "integer"


def test_json_schema_to_param_schemas_number_maps_to_float():
    schema = {
        "properties": {"score": {"type": "number"}},
        "required": [],
    }
    params = _json_schema_to_param_schemas(schema)
    assert params["score"].type == "float"


def test_json_schema_to_param_schemas_empty():
    assert _json_schema_to_param_schemas(None) == {}
    assert _json_schema_to_param_schemas({}) == {}


# ---------------------------------------------------------------------------
# MCPServerDefinition schema
# ---------------------------------------------------------------------------


def test_mcp_server_definition_defaults():
    defn = MCPServerDefinition(command=["npx", "server"])
    assert defn.transport == "stdio"
    assert defn.env == {}
    assert defn.url is None


def test_mcp_server_definition_sse():
    defn = MCPServerDefinition(
        command=[],
        transport="sse",
        url="http://localhost:3000/sse",
    )
    assert defn.transport == "sse"
    assert defn.url == "http://localhost:3000/sse"


# ---------------------------------------------------------------------------
# MCPRegistry with mocked clients
# ---------------------------------------------------------------------------


def _make_mock_mcp_tool(name: str, description: str = "", schema: dict = None):
    tool = MagicMock()
    tool.name = name
    tool.description = description or f"Tool: {name}"
    tool.inputSchema = schema or {"type": "object", "properties": {}, "required": []}
    return tool


def _make_mock_client(server_name: str, tools: list):
    client = AsyncMock()
    client.server_name = server_name
    client.tools = tools
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.call_tool = AsyncMock(return_value="tool result")
    return client


@pytest.mark.asyncio
async def test_registry_start_discovers_tools():
    """MCPRegistry.start() should populate tool_names from all servers."""
    mock_tool_a = _make_mock_mcp_tool("search")
    mock_tool_b = _make_mock_mcp_tool("read_file")
    mock_client_a = _make_mock_client("github", [mock_tool_a])
    mock_client_b = _make_mock_client("fs", [mock_tool_b])

    defns = {
        "github": MCPServerDefinition(command=["npx", "github-server"]),
        "fs": MCPServerDefinition(command=["npx", "fs-server"]),
    }
    registry = MCPRegistry(defns)

    with patch("elasticity.mcp.registry.MCPClient") as MockClient:
        MockClient.side_effect = [mock_client_a, mock_client_b]
        await registry.start()

    assert "github.search" in registry.tool_names
    assert "fs.read_file" in registry.tool_names


@pytest.mark.asyncio
async def test_registry_start_handles_server_failure_gracefully():
    """A server that fails to connect should not prevent other servers from starting."""
    mock_tool = _make_mock_mcp_tool("search")
    failing_client = AsyncMock()
    failing_client.connect = AsyncMock(side_effect=RuntimeError("connection refused"))
    good_client = _make_mock_client("good_server", [mock_tool])
    good_client.connect = AsyncMock()

    defns = {
        "bad": MCPServerDefinition(command=["bad-server"]),
        "good_server": MCPServerDefinition(command=["good-server"]),
    }
    registry = MCPRegistry(defns)

    with patch("elasticity.mcp.registry.MCPClient") as MockClient:
        MockClient.side_effect = [failing_client, good_client]
        await registry.start()

    assert "good_server.search" in registry.tool_names


@pytest.mark.asyncio
async def test_registry_call_tool():
    """MCPRegistry.call_tool() should delegate to the correct client."""
    mock_tool = _make_mock_mcp_tool("search")
    mock_client = _make_mock_client("github", [mock_tool])
    mock_client.call_tool = AsyncMock(return_value="search results")

    defns = {"github": MCPServerDefinition(command=["npx", "github-server"])}
    registry = MCPRegistry(defns)

    with patch("elasticity.mcp.registry.MCPClient", return_value=mock_client):
        await registry.start()

    result = await registry.call_tool("github", "search", {"query": "test"})
    assert result == "search results"
    mock_client.call_tool.assert_called_once_with("search", {"query": "test"})


@pytest.mark.asyncio
async def test_registry_call_tool_missing_server():
    """Calling a tool on a non-connected server should raise ToolError."""
    registry = MCPRegistry({})
    with pytest.raises(ToolError, match="not connected"):
        await registry.call_tool("missing", "search", {})


@pytest.mark.asyncio
async def test_registry_stop_closes_clients():
    mock_tool = _make_mock_mcp_tool("search")
    mock_client = _make_mock_client("github", [mock_tool])

    defns = {"github": MCPServerDefinition(command=["npx", "github-server"])}
    registry = MCPRegistry(defns)

    with patch("elasticity.mcp.registry.MCPClient", return_value=mock_client):
        await registry.start()

    await registry.stop()
    mock_client.close.assert_called_once()
    assert registry.tool_names == []


# ---------------------------------------------------------------------------
# ToolRegistry MCP integration
# ---------------------------------------------------------------------------


def test_register_mcp_tool_is_available_in_registry():
    """Tools registered via register_mcp() appear in get_available_tools()."""
    mock_tool = _make_mock_mcp_tool("search", schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "required": ["query"],
    })
    mock_mcp_registry = MagicMock()

    tool_registry = ToolRegistry()
    mock_mcp_server_registry = MCPRegistry.__new__(MCPRegistry)
    mock_mcp_server_registry._clients = {"github": _make_mock_client("github", [mock_tool])}
    mock_mcp_server_registry._tool_map = {"github.search": ("github", "search")}

    mock_mcp_server_registry.register_tools(tool_registry)
    assert "github.search" in tool_registry.get_available_tools()


def test_register_mcp_tool_schema_accessible():
    """get_tool_schema() should return a valid function-calling schema for MCP tools."""
    from elasticity.config.schema import ToolDefinition, ParameterSchema

    tool_registry = ToolRegistry()
    mock_mcp = MagicMock()

    tool_def = ToolDefinition(
        description="Search GitHub repositories",
        callable="_mcp_",
        parameters={"query": ParameterSchema(type="string", description="Search query")},
        config={"_mcp_server": "github", "_mcp_tool": "search_repositories"},
    )
    tool_registry.register_mcp("github.search_repositories", tool_def, mock_mcp)

    schema = tool_registry.get_tool_schema("github.search_repositories")
    assert schema["function"]["name"] == "github.search_repositories"
    assert "query" in schema["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_invoke_async_mcp_tool():
    """invoke_async() should route MCP tools through the MCPRegistry."""
    from elasticity.config.schema import ToolDefinition, ParameterSchema

    tool_registry = ToolRegistry()
    mock_mcp = AsyncMock()
    mock_mcp.call_tool = AsyncMock(return_value="repo list")

    tool_def = ToolDefinition(
        description="Search GitHub",
        callable="_mcp_",
        parameters={"query": ParameterSchema(type="string")},
        config={"_mcp_server": "github", "_mcp_tool": "search"},
    )
    tool_registry.register_mcp("github.search", tool_def, mock_mcp)

    result = await tool_registry.invoke_async("github.search", {"query": "elasticity"})
    assert result == "repo list"
    mock_mcp.call_tool.assert_called_once_with("github", "search", {"query": "elasticity"})


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_config_parses_mcp_servers_section():
    """Config.model_validate() should accept mcp_servers."""
    from elasticity.config.schema import Config

    data = {
        "agent_types": {
            "worker": {"model": "openai/gpt-4o", "system_prompt": "You are a worker."}
        },
        "tools": {},
        "orchestrations": {
            "test": {
                "flow": [{"agent": "worker", "input": "hello"}]
            }
        },
        "mcp_servers": {
            "github": {
                "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            }
        },
    }
    config = Config.model_validate(data)
    assert "github" in config.mcp_servers
    assert config.mcp_servers["github"].command == ["npx", "-y", "@modelcontextprotocol/server-github"]
    assert config.mcp_servers["github"].env == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}


def test_config_mcp_servers_optional():
    """Configs without mcp_servers should load fine."""
    from elasticity.config.schema import Config

    data = {
        "agent_types": {
            "worker": {"model": "openai/gpt-4o", "system_prompt": "Worker."}
        },
        "tools": {},
        "orchestrations": {
            "test": {"flow": [{"agent": "worker", "input": "hello"}]}
        },
    }
    config = Config.model_validate(data)
    assert config.mcp_servers == {}

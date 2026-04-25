"""MCP (Model Context Protocol) integration for Elasticity.

Provides MCPRegistry which manages MCP server lifecycle and tool discovery.
MCP tools are registered in the ToolRegistry as ``server_name.tool_name``.

Usage in config::

    mcp_servers:
      github:
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_TOKEN: "${GITHUB_TOKEN}"

    agent_types:
      researcher:
        tools: [github.search_repositories]
"""

from .registry import MCPRegistry

__all__ = ["MCPRegistry"]

"""Pydantic models for the global application configuration schema."""

from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, Field

from .schema import MCPServerDefinition


class SandboxConfig(BaseModel):
    """Sandbox / execution environment configuration.

    The ``provider`` field selects the execution backend. Provider-specific
    settings are captured in ``settings`` and will be validated by the
    relevant sandbox provider when it is initialized.
    """

    provider: str = Field(
        "local",
        description="Execution provider: 'local' (no isolation), 'docker', 'daytona', etc.",
    )
    settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific settings passed to the sandbox backend.",
    )


class StorageConfig(BaseModel):
    """Storage path configuration."""

    session_db: Optional[str] = Field(
        None,
        description=(
            "Path to the SQLite session database. "
            "Defaults to the XDG data directory (~/. local/share/elasticity/sessions.db). "
            "Supports ~ expansion."
        ),
    )


class LoggingConfig(BaseModel):
    """Logging / chat-log configuration."""

    chat_log: Optional[Union[str, bool]] = Field(
        None,
        description=(
            "Chat log file path, or false to disable. "
            "Defaults to the XDG data directory (~/. local/share/elasticity/chat.log). "
            "Supports ~ expansion."
        ),
    )


class GlobalConfig(BaseModel):
    """Root model for the global Elasticity application configuration.

    Loaded from ``~/.config/elasticity/config.yaml`` (or the path resolved
    by ``ELASTICITY_CONFIG``). The file is optional; sensible defaults are
    used when it is absent.
    """

    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    mcp_servers: Dict[str, MCPServerDefinition] = Field(
        default_factory=dict,
        description=(
            "Global MCP server definitions available to all orchestrations. "
            "Per-orchestration mcp_servers take precedence on name conflicts."
        ),
    )

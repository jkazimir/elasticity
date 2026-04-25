"""Pydantic models for conductor configuration."""

import logging as _logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .schema import AgentTypeDefinition, CognitiveContextConfig, ToolDefinition, _expand_tool_groups

_logger = _logging.getLogger(__name__)


class TeamDefinition(BaseModel):
    """A team (sub-orchestration) the conductor can delegate work to."""

    config: str = Field(
        ...,
        description="Path to the team's orchestration YAML, relative to the conductor config file",
    )
    orchestration: str = Field(
        ...,
        description="Name of the orchestration within the team config to run",
    )
    description: str = Field(
        ...,
        description="Description of what this team does, injected into the conductor's system prompt",
    )
    input: Dict[str, str] = Field(
        default_factory=dict,
        description="Input parameter names mapped to their types (e.g. topic: string)",
    )
    output_as: Optional[str] = Field(
        None,
        description="Context key to extract from the team result as the tool return value; "
        "if omitted the full result dict is serialized as JSON",
    )
    output: Optional[str] = Field(
        None,
        description="Deprecated. Use 'output_as' instead.",
    )
    output_schema: Optional[Dict[str, Any]] = Field(
        None,
        description="Expected fields in the team output. When set, the conductor receives "
        "a structured JSON object with only these keys instead of the raw output string. "
        "Each key maps to a type hint (e.g. 'boolean', 'string') for documentation only.",
    )

    @model_validator(mode="after")
    def normalize_output_alias(self) -> "TeamDefinition":
        """Normalize deprecated 'output' field to 'output_as'."""
        if self.output is not None and self.output_as is not None:
            raise ValueError("TeamDefinition cannot set both 'output' and 'output_as'")
        if self.output is not None and self.output_as is None:
            _logger.warning(
                "TeamDefinition field 'output' is deprecated; use 'output_as' instead"
            )
            object.__setattr__(self, "output_as", self.output)
        return self


class ConductorDefinition(BaseModel):
    """Identifies which agent_type acts as the conductor."""

    agent: str = Field(
        ...,
        description="Name of the agent_type (defined in this file) to use as the conductor",
    )
    max_concurrent_tools: Optional[int] = Field(
        None,
        description="Cap on how many team tool calls run concurrently. Overrides the conductor agent type's max_concurrent_tools when set.",
    )
    context_strategy: Optional[CognitiveContextConfig] = Field(
        None,
        description=(
            "Cognitive context strategy configuration for the conductor. "
            "When set, enables RAG-based context assembly instead of simple "
            "sliding-window history for the conductor's conversation."
        ),
    )


class ConductorConfig(BaseModel):
    """Top-level conductor configuration."""

    conductor: ConductorDefinition
    agent_types: Dict[str, AgentTypeDefinition] = Field(default_factory=dict)
    tools: Dict[str, ToolDefinition] = Field(default_factory=dict)
    teams: Dict[str, TeamDefinition] = Field(default_factory=dict)
    tool_groups: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "Named groups of tools that agents can reference with @group_name syntax. "
            "Example: '@filesystem' expands to all filesystem tools."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def expand_tool_groups(cls, data: Any) -> Any:
        """Expand @group_name references in agent tool lists."""
        if not isinstance(data, dict):
            return data
        tool_groups = data.get("tool_groups") or {}
        if tool_groups and isinstance(tool_groups, dict):
            agent_types = data.get("agent_types") or {}
            if agent_types and isinstance(agent_types, dict):
                data = data.copy()
                data["agent_types"] = _expand_tool_groups(agent_types, tool_groups)
        return data

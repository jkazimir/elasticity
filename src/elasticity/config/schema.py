"""Pydantic models for configuration schema."""

import logging as _logging
from typing import Any, Dict, List, Optional, Union, Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, Tag, Discriminator

ToolPolicy = Literal["allow", "deny", "ask"]

_logger = _logging.getLogger(__name__)


class ParameterSchema(BaseModel):
    """Schema for a tool parameter."""

    type: str  # 'string', 'integer', 'float', 'boolean', etc.
    required: bool = True
    default: Optional[Any] = None
    description: Optional[str] = None


class ToolDefinition(BaseModel):
    """Definition of a tool."""

    description: Optional[str] = None
    builtin: Optional[str] = Field(None, description="Built-in tool name (alternative to callable)")
    callable: Optional[str] = Field(None, description="Python dotted path to callable")
    parameters: Dict[str, ParameterSchema] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("callable")
    @classmethod
    def validate_callable_or_builtin(cls, v: Optional[str], info) -> Optional[str]:
        """Ensure either builtin or callable is provided."""
        if info.data.get("builtin"):
            return v  # builtin is set, callable is optional
        if not v:
            raise ValueError("Either 'builtin' or 'callable' must be provided")
        return v

    @model_validator(mode="after")
    def validate_description_for_callable(self):
        """Require description when using a custom callable (no builtin)."""
        if self.callable and not self.builtin:
            if not self.description:
                raise ValueError(
                    "Tool with 'callable' must provide a 'description' field. "
                    "Built-in tools can omit 'description' as it comes from the builtin definition."
                )
        return self


class AgentTypeDefinition(BaseModel):
    """Definition of an agent type."""

    model: str = Field(
        ...,
        description="LLM model identifier in format 'provider/model-name' (e.g., 'openai/gpt-4o', 'anthropic/claude-sonnet-4-6')",
    )
    system_prompt: str
    rules: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list, description="Tool names this agent can use")
    can_spawn: List[str] = Field(
        default_factory=list, description="Agent type names this agent can spawn"
    )
    max_concurrent_spawns: Optional[int] = Field(
        None, description="Maximum concurrent child agents"
    )
    max_tokens: int = Field(
        16384, description="Maximum tokens in the response"
    )
    max_tool_rounds: int = Field(
        10, description="Maximum number of tool calling rounds before returning"
    )
    max_concurrent_tools: Optional[int] = Field(
        None, description="Maximum number of tool calls to execute concurrently within a single round. None means unlimited."
    )
    output_schema: Optional[Dict[str, Any]] = Field(
        None, description="Optional schema for structured outputs"
    )
    tool_policies: Dict[str, ToolPolicy] = Field(
        default_factory=dict,
        description=(
            "Per-tool execution policy. Keys are tool names from the 'tools' list. "
            "Values: 'allow' (default, execute immediately), 'deny' (always block), "
            "'ask' (prompt the user at runtime; denied automatically in batch mode)."
        ),
    )


class ErrorStrategy(BaseModel):
    """Error handling strategy."""

    strategy: Literal["retry", "skip", "fallback", "fail"] = "retry"
    max_retries: int = 3
    backoff: Literal["exponential", "linear", "fixed"] = "exponential"
    fallback_agent: Optional[str] = Field(None, description="Agent to use with fallback strategy")


_KNOWN_STEP_INPUT_FIELDS = frozenset(
    {"agent", "input", "output_as", "on_error", "spawn_strategy", "collect_as", "spawn_context"}
)


class StepInput(BaseModel):
    """Input for a step."""

    # Note: extra="allow" to allow discriminated union to work properly.
    # Extra fields will be caught by validation errors from other step types.
    model_config = ConfigDict(extra="allow")

    agent: Optional[str] = None
    input: Optional[str] = None
    output_as: Optional[str] = None
    on_error: Optional[ErrorStrategy] = None
    spawn_strategy: Optional[Literal["dynamic"]] = None
    collect_as: Optional[str] = None
    spawn_context: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def warn_unknown_fields(cls, values: Any) -> Any:
        """Emit a warning for any field not in the known set (likely a typo)."""
        if isinstance(values, dict):
            unknown = set(values) - _KNOWN_STEP_INPUT_FIELDS
            if unknown:
                _logger.warning(
                    "StepInput has unrecognised field(s) %s — possible typo in config",
                    sorted(unknown),
                )
        return values


class ParallelStep(BaseModel):
    """Parallel execution step."""

    parallel: List["FlowStep"] = Field(..., description="Steps to run in parallel")


# ---------------------------------------------------------------------------
# Typed inner config models for structured step validation
# ---------------------------------------------------------------------------

class LoopConfig(BaseModel):
    """Configuration for a loop step."""

    max_iterations: Optional[int] = Field(None, ge=1, description="Maximum loop iterations")
    until: Optional[str] = Field(
        None,
        description=(
            "Stop condition expression. Supports comparisons (e.g. 'score >= 0.9', "
            "'done == true') or a bare variable name for a truthy check."
        ),
    )
    # body contains FlowStep objects at runtime; typed as Any to avoid recursive type issues
    body: Optional[List[Any]] = Field(None, description="Flow steps to execute each iteration")


class SuperviseWorker(BaseModel):
    """A single worker in a supervise step."""

    model_config = ConfigDict(extra="allow")

    agent: str = Field(..., description="Agent type name to run as a worker")
    input: Optional[str] = Field(None, description="Input template for the worker")
    task: Optional[str] = Field(None, description="Deprecated alias for 'input'. Use 'input' instead.")
    output_as: Optional[str] = Field(None, description="Context variable name to store worker output")

    @model_validator(mode="after")
    def normalize_task_alias(self) -> "SuperviseWorker":
        """Normalize deprecated 'task' field to 'input'."""
        if self.task is not None and self.input is not None:
            raise ValueError("SuperviseWorker cannot set both 'task' and 'input'")
        if self.task is not None and self.input is None:
            _logger.warning(
                "SuperviseWorker field 'task' is deprecated; use 'input' instead"
            )
            object.__setattr__(self, "input", self.task)
        return self


class SuperviseConfig(BaseModel):
    """Configuration for a supervise step."""

    supervisor: str = Field(..., description="Agent type name to act as supervisor")
    workers: List[SuperviseWorker] = Field(..., description="Worker agent definitions")
    on_reject: Literal["retry_with_feedback", "fail"] = Field(
        "retry_with_feedback",
        description="What to do when the supervisor rejects worker output",
    )
    max_retries: int = Field(3, ge=1, description="Maximum supervisor rejection retries")


class IntervalConfig(BaseModel):
    """Configuration for an interval step."""

    every: str = Field(..., description="Interval duration, e.g. '30s', '1m'")
    agent: str = Field(..., description="Agent type name to run on each interval")
    input: Optional[str] = Field(None, description="Input template for the agent")
    output_as: str = Field(..., description="Context variable name to store agent output")
    until: Optional[str] = Field(
        None,
        description=(
            "Stop condition. Use 'orchestration.complete' to stop when the orchestration finishes, "
            "or a comparison expression like 'score >= 0.9'."
        ),
    )


class ApproveConfig(BaseModel):
    """Configuration for a human-in-the-loop approval step."""

    content: str = Field(..., description="Content to display for review (template string)")
    message: Optional[str] = Field(None, description="Prompt message shown to the reviewer")
    output_as: Optional[str] = Field(None, description="Context variable to store the approved content")
    on_reject: Literal["retry_previous", "fail"] = Field(
        "retry_previous",
        description="What to do on rejection: retry the preceding step or fail the orchestration",
    )
    max_retries: int = Field(3, ge=1, description="Maximum rejection retries before failing")


class LoadContextEntry(BaseModel):
    """A single entry in a load_context step."""

    key: str = Field(..., description="Exact memory key to retrieve")
    as_: str = Field(..., alias="as", description="Context variable name to store the value under")
    read_file: bool = Field(False, description="If true and value is a file path, read the file contents")

    model_config = ConfigDict(populate_by_name=True)


class LoadContextConfig(BaseModel):
    """Configuration for a load_context step."""

    memory_tool: str = Field(..., description="Name of the memory_retrieve tool to use")
    load: List[LoadContextEntry] = Field(..., description="Memory keys to load into context variables")
    output_as: Optional[str] = Field(
        None,
        description="Combine all loaded values into this single context variable",
    )


class SaveContextConfig(BaseModel):
    """Configuration for a save_context step."""

    memory_tool: str = Field(..., description="Name of the memory_store tool to use")
    save: Dict[str, str] = Field(
        ...,
        description="Mapping of memory keys to template strings, e.g. '\"task:branch\": \"{branch_name}\"'",
    )


class ToolCallEntry(BaseModel):
    """A single tool invocation in a tool_call step."""

    tool: str = Field(..., description="Name of the registered tool to invoke")
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool parameters. String values support {variable} template interpolation.",
    )
    output_as: Optional[str] = Field(
        None, description="Context variable name to store the tool result",
    )


class ToolCallConfig(BaseModel):
    """Configuration for a tool_call step."""

    # Single-tool shorthand fields
    tool: Optional[str] = Field(None, description="Tool name (single-tool shorthand)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Parameters (single-tool shorthand)")
    # Multi-tool form
    calls: Optional[List[ToolCallEntry]] = Field(None, description="Sequence of tool calls")
    # Shared
    output_as: Optional[str] = Field(
        None, description="Context variable for the result (single-tool or last call result)",
    )
    on_error: Literal["fail", "skip"] = Field(
        "fail", description="Error handling: 'fail' raises, 'skip' logs warning and continues",
    )

    @model_validator(mode="after")
    def validate_tool_or_calls(self) -> "ToolCallConfig":
        if self.tool and self.calls:
            raise ValueError("tool_call cannot specify both 'tool' and 'calls'")
        if not self.tool and not self.calls:
            raise ValueError("tool_call must specify either 'tool' or 'calls'")
        return self


class LoopStep(BaseModel):
    """Loop execution step."""

    loop: LoopConfig = Field(
        ...,
        description="Loop configuration with max_iterations, until, body",
    )


class RouteCase(BaseModel):
    """Case in a route step."""

    cases: Dict[str, List["FlowStep"]]
    default: Optional[List["FlowStep"]] = None
    condition: str = Field(..., description="Context variable name to route on (braces are optional: 'var' or '{var}')")


class RouteStep(BaseModel):
    """Route execution step."""

    route: RouteCase


class SuperviseStep(BaseModel):
    """Supervise execution step."""

    supervise: SuperviseConfig = Field(
        ...,
        description="Supervisor configuration with supervisor, workers, on_reject, max_retries",
    )


class IntervalStep(BaseModel):
    """Interval execution step."""

    interval: IntervalConfig = Field(
        ...,
        description="Interval configuration with every, agent, input, output_as, until",
    )


class ApproveStep(BaseModel):
    """Human-in-the-loop approval step."""

    approve: ApproveConfig = Field(
        ...,
        description="Approval configuration with content, message, output_as, on_reject, max_retries",
    )


class LoadContextStep(BaseModel):
    """Zero-LLM-cost step that loads memory keys into context variables."""

    load_context: LoadContextConfig = Field(
        ...,
        description=(
            "Configuration with 'memory_tool' (tool name), 'load' (list of key/as/read_file entries), "
            "and optional 'output_as' (combine all loaded values into one context variable)"
        ),
    )


class SaveContextStep(BaseModel):
    """Zero-LLM-cost step that writes context variables to persistent memory."""

    save_context: SaveContextConfig = Field(
        ...,
        description=(
            "Configuration with 'memory_tool' (tool name) and 'save' (dict mapping memory keys "
            "to template strings, e.g. '\"task:branch\": \"{branch_name}\"')"
        ),
    )


class ToolCallStep(BaseModel):
    """Zero-LLM-cost step that invokes registered tools directly from the flow."""

    tool_call: ToolCallConfig = Field(
        ...,
        description=(
            "Configuration with 'tool' + 'parameters' (single tool) or 'calls' (list of tools). "
            "String parameters support {variable} template interpolation."
        ),
    )


# Recursive type for flow steps
# Note: Discriminator removed due to Pydantic limitation with recursive Union types.
# Discrimination is handled manually in OrchestrationDefinition.parse_flow_steps validator.
FlowStep = Union[
    ParallelStep,
    LoopStep,
    RouteStep,
    SuperviseStep,
    IntervalStep,
    ApproveStep,
    LoadContextStep,
    SaveContextStep,
    ToolCallStep,
    StepInput,
]

# Force models with "FlowStep" forward references to pick up the updated union
_ns = {"FlowStep": FlowStep}
ParallelStep.model_rebuild(force=True, _types_namespace=_ns)
RouteCase.model_rebuild(force=True, _types_namespace=_ns)
RouteStep.model_rebuild(force=True, _types_namespace=_ns)
LoopStep.model_rebuild(force=True, _types_namespace=_ns)


InputHandlingMode = Literal["queue", "interrupt", "ignore"]
InterruptBehavior = Literal["cancel", "graceful"]
InterruptDelivery = Literal["event", "context", "agent"]


class InputHandlingConfig(BaseModel):
    """Configuration for handling user input during orchestration execution."""

    mode: InputHandlingMode = Field(
        "queue",
        description=(
            "How to handle input received during execution: "
            "'queue' (buffer for next turn), 'interrupt' (handle immediately), 'ignore' (discard)"
        ),
    )
    interrupt_behavior: Optional[InterruptBehavior] = Field(
        None,
        description="For interrupt mode: 'cancel' (stop execution) or 'graceful' (inject for orchestration to handle)",
    )
    interrupt_delivery: Optional[List[InterruptDelivery]] = Field(
        None,
        description="For graceful interrupts: how to deliver - event, context, agent (at least one required)",
    )
    queue_limit: int = Field(
        10,
        ge=1,
        le=100,
        description="Maximum queued messages when mode is queue",
    )

    @model_validator(mode="after")
    def validate_interrupt_config(self) -> "InputHandlingConfig":
        """interrupt_behavior and interrupt_delivery only apply when mode is interrupt."""
        config = self
        if self.mode == "interrupt" and self.interrupt_behavior is None:
            config = self.model_copy(update={"interrupt_behavior": "cancel"})
        if config.interrupt_behavior == "graceful" and not config.interrupt_delivery:
            raise ValueError(
                "interrupt_delivery must specify at least one of: event, context, agent"
            )
        return config


def _parse_flow_step_recursive(step: Any) -> Any:
    """Recursively parse flow steps, handling nested structures.

    This works around a Pydantic limitation where callable discriminators
    don't work properly with recursive Union types.
    """
    if not isinstance(step, dict):
        return step

    # Check for structural step types by their unique keys
    if "parallel" in step:
        parsed_parallel = [_parse_flow_step_recursive(s) for s in step["parallel"]]
        return ParallelStep(parallel=parsed_parallel)

    if "loop" in step:
        loop_data = step["loop"].copy()
        if "body" in loop_data and isinstance(loop_data["body"], list):
            loop_data["body"] = [_parse_flow_step_recursive(s) for s in loop_data["body"]]
        return LoopStep(loop=LoopConfig.model_validate(loop_data))

    if "route" in step:
        route_data = step["route"]
        parsed_cases = {}
        for case_key, case_steps in route_data.get("cases", {}).items():
            parsed_cases[case_key] = [_parse_flow_step_recursive(s) for s in case_steps]
        parsed_default = None
        if "default" in route_data and route_data["default"]:
            parsed_default = [_parse_flow_step_recursive(s) for s in route_data["default"]]
        return RouteStep(route=RouteCase(
            condition=route_data["condition"],
            cases=parsed_cases,
            default=parsed_default,
        ))

    if "supervise" in step:
        return SuperviseStep(supervise=SuperviseConfig.model_validate(step["supervise"]))

    if "interval" in step:
        return IntervalStep(interval=IntervalConfig.model_validate(step["interval"]))

    if "approve" in step:
        return ApproveStep(approve=ApproveConfig.model_validate(step["approve"]))

    if "load_context" in step:
        return LoadContextStep(load_context=LoadContextConfig.model_validate(step["load_context"]))

    if "save_context" in step:
        return SaveContextStep(save_context=SaveContextConfig.model_validate(step["save_context"]))

    if "tool_call" in step:
        return ToolCallStep(tool_call=ToolCallConfig.model_validate(step["tool_call"]))

    # Default to StepInput
    return StepInput.model_validate(step)


def _expand_tool_groups(agent_types: Dict[str, Any], tool_groups: Dict[str, List[str]]) -> Dict[str, Any]:
    """Expand @group_name references in agent tool lists.

    Args:
        agent_types: Raw agent_types dict from YAML
        tool_groups: Mapping of group name to list of tool names

    Returns:
        agent_types dict with @group references expanded to individual tool names
    """
    expanded = {}
    for agent_name, agent_data in agent_types.items():
        if not isinstance(agent_data, dict):
            expanded[agent_name] = agent_data
            continue
        tools = agent_data.get("tools") or []
        if not any(isinstance(t, str) and t.startswith("@") for t in tools):
            expanded[agent_name] = agent_data
            continue
        expanded_tools = []
        for tool in tools:
            if isinstance(tool, str) and tool.startswith("@"):
                group_name = tool[1:]
                if group_name not in tool_groups:
                    raise ValueError(
                        f"Agent '{agent_name}' references undefined tool group '@{group_name}'. "
                        f"Available groups: {sorted(tool_groups)}"
                    )
                expanded_tools.extend(tool_groups[group_name])
            else:
                expanded_tools.append(tool)
        agent_data_copy = agent_data.copy()
        agent_data_copy["tools"] = expanded_tools
        expanded[agent_name] = agent_data_copy
    return expanded


class CognitiveContextConfig(BaseModel):
    """Configuration for the cognitive context strategy.

    When attached to an orchestration, enables RAG-based context assembly
    instead of simple sliding-window history.
    """

    recent_turns: int = Field(
        3, ge=1, description="Number of recent turns always kept in working memory"
    )
    similarity_threshold: float = Field(
        0.35,
        ge=0.0,
        le=1.0,
        description="Cosine similarity threshold below which a topic shift is detected",
    )
    max_recalled_memories: int = Field(
        5, ge=0, description="Maximum memories to recall via RAG per turn"
    )
    embedding_provider: str = Field(
        "local/all-MiniLM-L6-v2",
        description="Embedding provider in provider/model format (local, voyage, openai)",
    )
    topic_detection: Literal["embedding", "llm"] = Field(
        "embedding",
        description="How to detect topic shifts: embedding similarity or LLM classification",
    )
    memory_db_path: Optional[str] = Field(
        None,
        description="Path to cognitive memory SQLite DB (default: ~/.local/share/elasticity/cognitive.db)",
    )
    summary_model: Optional[str] = Field(
        None,
        description="Model for topic summarisation in provider/model format (None = use agent's model)",
    )
    consolidation_min_length: int = Field(
        200,
        ge=0,
        description=(
            "Minimum combined length (user + assistant chars) for a turn to be considered "
            "for active consolidation into long-term memory"
        ),
    )
    consolidation_novelty_threshold: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine similarity below this threshold triggers novelty-based consolidation: "
            "turns that diverge strongly from the current topic are promoted to long-term"
        ),
    )
    consolidation_length_threshold: int = Field(
        1000,
        ge=0,
        description=(
            "Combined length (user + assistant chars) above this threshold triggers "
            "length-based consolidation into long-term memory"
        ),
    )


class OrchestrationDefinition(BaseModel):
    """Definition of an orchestration."""

    description: Optional[str] = None
    input: Optional[Dict[str, str]] = Field(
        None, description="Input schema (parameter name -> type)"
    )
    mode: Literal["batch", "conversational"] = Field(
        "batch", description="Execution mode: batch (one-shot) or conversational (multi-turn)"
    )
    response_key: Optional[str] = Field(
        None, description="Which output_as variable becomes the user-facing response (for conversational mode)"
    )
    communication: Literal["shared_context", "message_passing", "both"] = "message_passing"
    error_strategy: Optional[ErrorStrategy] = None
    input_handling: Optional[InputHandlingConfig] = Field(
        None,
        description="For conversational mode: how to handle input during execution (queue, interrupt, ignore)",
    )
    context_strategy: Optional[CognitiveContextConfig] = Field(
        None,
        description=(
            "Cognitive context strategy configuration. When set, enables RAG-based "
            "context assembly instead of simple sliding-window history."
        ),
    )
    flow: List[FlowStep] = Field(..., description="List of orchestration primitives")

    @model_validator(mode="before")
    @classmethod
    def parse_flow_steps(cls, data: Any) -> Any:
        """Pre-parse flow steps to work around Pydantic discriminator limitation."""
        if isinstance(data, dict) and "flow" in data:
            flow_data = data["flow"]
            if isinstance(flow_data, list):
                data = data.copy()
                data["flow"] = [_parse_flow_step_recursive(step) for step in flow_data]
        return data


class MCPServerDefinition(BaseModel):
    """Definition of an MCP server that provides tools to agents.

    Tools discovered from this server are registered as ``server_name.tool_name``
    in the tool registry, allowing agents to reference them in their ``tools`` list.
    """

    command: List[str] = Field(..., description="Subprocess command to start the MCP server")
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables (supports ${VAR} interpolation from os.environ)",
    )
    transport: Literal["stdio", "sse"] = Field(
        "stdio", description="Transport type: stdio (subprocess) or sse (HTTP)"
    )
    url: Optional[str] = Field(None, description="Server URL for SSE transport")


class Config(BaseModel):
    """Root configuration model."""

    agent_types: Dict[str, AgentTypeDefinition] = Field(default_factory=dict)
    tools: Dict[str, ToolDefinition] = Field(default_factory=dict)
    orchestrations: Dict[str, OrchestrationDefinition] = Field(default_factory=dict)
    mcp_servers: Dict[str, MCPServerDefinition] = Field(
        default_factory=dict,
        description="MCP server definitions; tools discovered as server_name.tool_name",
    )
    tool_groups: Dict[str, List[str]] = Field(
        default_factory=dict,
        description=(
            "Named groups of tools that agents can reference with @group_name syntax. "
            "Example: '@filesystem' expands to all filesystem tools."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def expand_tool_groups_and_parse_flows(cls, data: Any) -> Any:
        """Expand @group references in agent tools and pre-parse orchestration flow steps."""
        if not isinstance(data, dict):
            return data

        # Expand tool groups first
        tool_groups = data.get("tool_groups") or {}
        if tool_groups and isinstance(tool_groups, dict):
            agent_types = data.get("agent_types") or {}
            if agent_types and isinstance(agent_types, dict):
                data = data.copy()
                data["agent_types"] = _expand_tool_groups(agent_types, tool_groups)

        # Pre-parse orchestration flow steps
        orch_data = data.get("orchestrations")
        if isinstance(orch_data, dict):
            data = data.copy() if not isinstance(data.get("agent_types"), dict) else data
            parsed_orchestrations = {}
            for name, orch in orch_data.items():
                if isinstance(orch, dict) and "flow" in orch and isinstance(orch["flow"], list):
                    parsed_orch = orch.copy()
                    parsed_orch["flow"] = [_parse_flow_step_recursive(step) for step in orch["flow"]]
                    parsed_orchestrations[name] = parsed_orch
                else:
                    parsed_orchestrations[name] = orch
            data["orchestrations"] = parsed_orchestrations

        return data

    @model_validator(mode="after")
    def validate_non_empty(self) -> "Config":
        """Warn if the config defines no orchestrations, agent types, or tools."""
        if not self.agent_types and not self.tools and not self.orchestrations:
            _logger.warning(
                "Config defines no agent_types, tools, or orchestrations — it is effectively empty"
            )
        return self

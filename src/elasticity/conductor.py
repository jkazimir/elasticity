"""Conductor: a meta-orchestrator that directs multiple team orchestrations."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config.conductor_schema import ConductorConfig, TeamDefinition
from .config.conductor_loader import load_conductor_config
from .config.schema import ParameterSchema
from .orchestration import Orchestration
from .runtime.tools import ToolRegistry
from .runtime.agent import AgentRunner, ApprovalFn
from .runtime.context import ContextManager
from .runtime.context_strategy import ContextStrategy, CognitiveStrategy
from .runtime.executor import HumanApprovalFn, HumanApprovalResult
from .runtime.session import Session
from .events import Event, EventBus
from .tracing import RunTrace, write_chat_turn_log, write_team_run_log

# Names reserved for the conductor's self-management tools
_MANAGEMENT_TOOL_NAMES = ("reload_team", "add_team", "remove_team", "reload_conductor")

# Max characters stored per team/tool result in _turn_tool_calls.
# Longer outputs are truncated here; event display keeps its own limit.
_TOOL_RESULT_TRUNCATION = 2000


def _extract_output_schema(output: str, schema: Dict[str, Any]) -> str:
    """Extract declared schema fields from a team output string.

    Tries JSON parsing first, then falls back to key: value pattern matching.
    Returns a compact JSON object with only the declared fields so the conductor
    always receives a predictable structure regardless of how the team formatted
    its output.

    Args:
        output: Raw output string from the team.
        schema: Dict mapping field names to type hints (for documentation only).

    Returns:
        JSON string with extracted fields, or the original output if extraction fails.
    """
    import re

    field_names = list(schema.keys())

    # --- Attempt 1: parse output as JSON and extract fields ---
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            extracted = {k: parsed[k] for k in field_names if k in parsed}
            if extracted:
                return json.dumps(extracted, indent=2)
    except (json.JSONDecodeError, ValueError):
        pass

    # --- Attempt 1.5: markdown fence extraction (```json ... ```) ---
    fence_pattern = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
    for fence_match in fence_pattern.finditer(output):
        try:
            parsed = json.loads(fence_match.group(1).strip())
            if isinstance(parsed, dict):
                extracted = {k: parsed[k] for k in field_names if k in parsed}
                if extracted:
                    return json.dumps(extracted, indent=2)
        except (json.JSONDecodeError, ValueError):
            continue

    # --- Attempt 2: search for nested JSON objects inside the text ---
    json_block_pattern = r'\{[^{}]*\}'
    for match in re.finditer(json_block_pattern, output, re.DOTALL):
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                extracted = {k: parsed[k] for k in field_names if k in parsed}
                if len(extracted) >= max(1, len(field_names) // 2):
                    return json.dumps(extracted, indent=2)
        except (json.JSONDecodeError, ValueError):
            continue

    # --- Attempt 3: regex extraction of "key": value patterns ---
    extracted = {}
    for field_name in field_names:
        # Match both quoted and unquoted values
        pattern = rf'["\']?{re.escape(field_name)}["\']?\s*:\s*(["\']?)(.+?)\1(?:\s*[,\n\r}}]|$)'
        match = re.search(pattern, output, re.IGNORECASE | re.MULTILINE)
        if match:
            value: Any = match.group(2).strip().rstrip('",}')
            # Coerce boolean strings
            if value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            elif value.lstrip("-").isdigit():
                value = int(value)
            extracted[field_name] = value

    if extracted:
        return json.dumps(extracted, indent=2)

    # Fallback: return original output unchanged
    return output


class Conductor:
    """Directs a set of team orchestrations toward a stakeholder's goal.

    The conductor is a single LLM agent whose tools are entire orchestrations.
    At startup, each team's description and I/O schema are appended to the
    conductor agent's system prompt so it always knows what it can delegate.

    Usage::

        conductor = Conductor("conductor.yaml")
        result = await conductor.run("Write a blog post about AI safety")

        # Or interactively:
        session = Session()
        reply = await conductor.chat("Research quantum computing", session=session)
        reply = await conductor.chat("Now write a summary", session=session)
    """

    def __init__(self, config_path: str):
        self.config_path = Path(config_path).resolve()
        self.config: ConductorConfig = load_conductor_config(self.config_path)

        # Resolve conductor agent name early — needed by _rebuild_agent_type
        conductor_agent_name = self.config.conductor.agent
        if conductor_agent_name not in self.config.agent_types:
            raise ValueError(
                f"Conductor agent '{conductor_agent_name}' not found in agent_types"
            )
        self._conductor_name = conductor_agent_name

        # Load each team's orchestration from its config file
        self._team_orchestrations: Dict[str, Orchestration] = {}
        for team_name, team_def in self.config.teams.items():
            team_path = self.config_path.parent / team_def.config
            self._team_orchestrations[team_name] = Orchestration(team_path)

        # Build tool registry: conductor's own tools + one tool per team
        self.tool_registry = ToolRegistry()
        for tool_name, tool_def in self.config.tools.items():
            self.tool_registry.register(tool_name, tool_def)

        # Implicitly register any builtin tool referenced by conductor agents but not explicitly defined
        from .tools.builtins import list_builtin_tools as _list_builtin_tools
        from .config.schema import ToolDefinition as _ToolDefinition
        _valid_builtins = set(_list_builtin_tools())
        _explicit = set(self.config.tools.keys())
        _implicit: set[str] = set()
        for agent_def in self.config.agent_types.values():
            for t in agent_def.tools:
                if t in _valid_builtins and t not in _explicit:
                    _implicit.add(t)
        for t in _implicit:
            self.tool_registry.register(t, _ToolDefinition(builtin=t))
        for team_name, team_def in self.config.teams.items():
            self._register_team_tool(
                team_name, team_def, self._team_orchestrations[team_name]
            )

        # Register self-management tools (reload_team, add_team, remove_team)
        self._register_management_tools()

        # Build agent type with manifest injected into system prompt
        self._rebuild_agent_type()

        # Effective concurrency cap: conductor-level overrides agent-type-level
        _cap = (
            self.config.conductor.max_concurrent_tools
            if self.config.conductor.max_concurrent_tools is not None
            else self._agent_type.max_concurrent_tools
        )
        self._semaphore_cap: Optional[int] = _cap

        self._events = EventBus()

        # Build cognitive context strategy if configured on the conductor
        self._context_strategy: Optional[ContextStrategy] = None
        if self.config.conductor.context_strategy is not None:
            self._context_strategy = self._build_context_strategy(
                self.config.conductor.context_strategy
            )

        self.agent_runner = AgentRunner(
            self.tool_registry, self._events,
            context_strategy=self._context_strategy,
        )
        self._approval_fn: Optional[ApprovalFn] = None
        self._turn_tool_calls: Optional[list] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_context_strategy(
        self,
        config: Any,
        event_bus: Optional[EventBus] = None,
    ) -> ContextStrategy:
        """Build a :class:`CognitiveStrategy` for the conductor."""
        from .memory.embeddings import resolve_embedding_provider
        from .memory.vector_store import VectorStore

        db_path = config.memory_db_path
        if db_path is None:
            import platformdirs

            db_path = str(
                Path(platformdirs.user_data_dir("elasticity")) / "conductor_cognitive.db"
            )

        embedding_provider = resolve_embedding_provider(config.embedding_provider)
        vector_store = VectorStore(db_path)

        async def _llm_fn(model: str, messages: list, max_tokens: int) -> str:
            from .backends.registry import resolve_backend
            backend, model_name = resolve_backend(model)
            response = await backend.complete(model_name, messages, max_tokens=max_tokens)
            return response.content

        return CognitiveStrategy(
            config=config,
            vector_store=vector_store,
            embedding_provider=embedding_provider,
            event_bus=event_bus or self._events,
            llm_fn=_llm_fn,
        )

    def _rebuild_agent_type(self) -> None:
        """(Re)build self._agent_type from config, injecting the current manifest.

        Called at init and after any team mutation so that the LLM's tool list
        and system prompt always reflect the current team roster.
        """
        base_agent_type = self.config.agent_types[self._conductor_name]
        manifest = self._build_manifest() + self._management_manifest()

        # Union of: declared agent-type tools, conductor-level tools, team
        # names, and self-management tool names.
        all_tool_names = (
            list(base_agent_type.tools)
            + list(self.config.tools.keys())
            + list(self.config.teams.keys())
            + list(_MANAGEMENT_TOOL_NAMES)
        )
        # Deduplicate while preserving order
        seen: set = set()
        unique_tools = []
        for t in all_tool_names:
            if t not in seen:
                seen.add(t)
                unique_tools.append(t)

        self._agent_type = base_agent_type.model_copy(
            update={
                "system_prompt": base_agent_type.system_prompt + manifest,
                "tools": unique_tools,
            }
        )

    def _make_conductor_approval_fn(self) -> HumanApprovalFn:
        """Return an approval function that routes plan reviews back to the conductor LLM.

        When a sub-orchestration hits a human-in-the-loop approve node, the
        conductor agent is asked to review the content and returns approve,
        reject (with feedback), or edit (with revised content).
        """
        async def conductor_approval_fn(message: str, content: str) -> HumanApprovalResult:
            prompt = (
                f"A sub-orchestration you delegated needs your review before it proceeds.\n\n"
                f"**{message}**\n\n"
                f"{content}\n\n"
                f"Review the above and respond with exactly one of:\n"
                f'- "approve" — proceed as-is\n'
                f'- "reject: <your feedback>" — send back for revision with your notes\n'
                f'- "edit:\\n<revised full content>" — replace the content with your version'
            )
            context = ContextManager("approval_context")
            result = await self.agent_runner.run(
                self._agent_type,
                self._conductor_name,
                prompt,
                context,
            )
            response = result.get("content", "").strip()
            lower = response.lower()

            if lower.startswith("reject:"):
                feedback = response[7:].strip()
                return HumanApprovalResult(decision="reject", feedback=feedback or None)

            if lower.startswith("edit:"):
                edited = response[5:].strip()
                return HumanApprovalResult(decision="edit", edited_content=edited or None)

            # "approve" or anything unrecognised → approve
            return HumanApprovalResult(decision="approve")

        return conductor_approval_fn

    def _register_team_tool(
        self,
        team_name: str,
        team_def: TeamDefinition,
        orchestration: Orchestration,
    ) -> None:
        """Register a team orchestration as a callable tool on the registry."""
        parameters = {
            param_name: ParameterSchema(
                type=param_type,
                required=True,
                description=f"The {param_name} input for the {team_name} team",
            )
            for param_name, param_type in team_def.input.items()
        }

        orch_name = team_def.orchestration
        output_key = team_def.output_as  # output_as is the canonical field (output is deprecated)
        output_schema = team_def.output_schema

        async def call_team(**kwargs: Any) -> str:
            child_bus = EventBus()
            trace = RunTrace(
                run_id=uuid.uuid4().hex[:8],
                orchestration_name=orch_name,
                log_to_console=False,
            )
            trace.subscribe_to(child_bus)
            child_bus.subscribe(Event, lambda e: self._events.emit(e))

            try:
                result = await orchestration.run(
                    orch_name,
                    input_data=kwargs,
                    event_bus=child_bus,
                    stream_responses=self.agent_runner.stream_responses,
                    approval_fn=self._approval_fn,
                    human_approval_fn=self._make_conductor_approval_fn(),
                )
                if output_key:
                    # Try top-level result key first, then the messages sub-dict
                    if output_key in result:
                        output = str(result[output_key])
                    else:
                        messages = result.get("messages", {})
                        if isinstance(messages, dict) and output_key in messages:
                            output = str(messages[output_key])
                        else:
                            output = json.dumps(result, indent=2)
                else:
                    output = json.dumps(result, indent=2)
                # If output_schema is declared, extract only the specified fields
                # so the conductor always receives a clean, predictable JSON object.
                if output_schema:
                    output = _extract_output_schema(output, output_schema)
                log_path = write_team_run_log(trace, team_name, kwargs, self._conductor_name)
                if log_path:
                    output += f"\n\n[Run log written to: {log_path}]"
                if self._turn_tool_calls is not None:
                    self._turn_tool_calls.append(
                        {"team": team_name, "args": kwargs, "result": output[:_TOOL_RESULT_TRUNCATION]}
                    )
                return output
            except Exception as e:
                error_msg = f"Error running team '{team_name}': {e}"
                log_path = write_team_run_log(trace, team_name, kwargs, self._conductor_name)
                if log_path:
                    error_msg += f"\n\n[Run log written to: {log_path}]"
                if self._turn_tool_calls is not None:
                    self._turn_tool_calls.append(
                        {"team": team_name, "args": kwargs, "error": str(e)}
                    )
                return error_msg

        self.tool_registry.register_callable(
            name=team_name,
            description=team_def.description,
            parameters=parameters,
            fn=call_team,
        )

    def _register_management_tools(self) -> None:
        """Register reload_team, add_team, and remove_team on the tool registry."""

        # --- reload_team ---
        async def _reload_team(team_name: str) -> str:
            return self.reload_team(team_name)

        self.tool_registry.register_callable(
            name="reload_team",
            description=(
                "Reload a team's orchestration from its YAML config file on disk. "
                "Call this after using file_write or file_edit to modify the team's config. "
                "The reloaded orchestration is used starting from the next tool call."
            ),
            parameters={
                "team_name": ParameterSchema(
                    type="string",
                    required=True,
                    description="Name of the team to reload",
                ),
            },
            fn=_reload_team,
        )

        # --- add_team ---
        async def _add_team(
            team_name: str,
            config_path: str,
            orchestration: str,
            description: str,
            output_key: str = "",
        ) -> str:
            return self.add_team(
                team_name=team_name,
                config_path=config_path,
                orchestration=orchestration,
                description=description,
                output_key=output_key or None,
            )

        self.tool_registry.register_callable(
            name="add_team",
            description=(
                "Register a new team orchestration so it can be delegated to. "
                "The team is immediately available as a tool for future tool calls."
            ),
            parameters={
                "team_name": ParameterSchema(
                    type="string",
                    required=True,
                    description="Unique name for the new team (becomes the tool name)",
                ),
                "config_path": ParameterSchema(
                    type="string",
                    required=True,
                    description="Path to the team's orchestration YAML, relative to the conductor config",
                ),
                "orchestration": ParameterSchema(
                    type="string",
                    required=True,
                    description="Name of the orchestration within the team config to run",
                ),
                "description": ParameterSchema(
                    type="string",
                    required=True,
                    description="Description of what this team does",
                ),
                "output_key": ParameterSchema(
                    type="string",
                    required=False,
                    description="Context key to extract from the team result (optional)",
                ),
            },
            fn=_add_team,
        )

        # --- remove_team ---
        async def _remove_team(team_name: str) -> str:
            return self.remove_team(team_name)

        self.tool_registry.register_callable(
            name="remove_team",
            description="Unregister a team so it is no longer available for delegation.",
            parameters={
                "team_name": ParameterSchema(
                    type="string",
                    required=True,
                    description="Name of the team to remove",
                ),
            },
            fn=_remove_team,
        )

        # --- reload_conductor ---
        async def _reload_conductor() -> str:
            return self.reload_conductor()

        self.tool_registry.register_callable(
            name="reload_conductor",
            description=(
                "Reload the conductor's own config from its YAML file on disk. "
                "Call this after using file_write or file_edit to modify the conductor config "
                "(e.g., updating the conductor agent's system prompt, adjusting tools, or "
                "changing team definitions). The updated config takes effect on the next response."
            ),
            parameters={},
            fn=_reload_conductor,
        )

    def _build_manifest(self) -> str:
        """Build the team manifest appended to the conductor's system prompt."""
        if not self.config.teams:
            return ""

        lines = [
            "",
            "",
            "## Available Teams",
            "",
            "Delegate work by calling team names as tools. "
            "Each team runs a full orchestration and returns its result.",
            "",
        ]
        for team_name, team_def in self.config.teams.items():
            team_abs_path = self.config_path.parent / team_def.config
            lines.append(f"### {team_name}")
            lines.append(team_def.description)
            lines.append(f"**Config file:** `{team_abs_path.resolve()}`")
            if team_def.input:
                lines.append("")
                lines.append("**Input parameters:**")
                for param, ptype in team_def.input.items():
                    lines.append(f"  - `{param}` ({ptype})")
            if team_def.output_as:
                lines.append("")
                lines.append(f"**Returns:** the `{team_def.output_as}` value from the result")
            lines.append("")

        return "\n".join(lines)

    def _management_manifest(self) -> str:
        """Build the management tools section appended to the conductor's system prompt."""
        config_dir = self.config_path.parent
        return (
            "\n\n## Team Management Tools\n\n"
            f"**Config directory (absolute path):** `{config_dir}`\n\n"
            f"**Conductor config (absolute path):** `{self.config_path}`\n\n"
            "All team config files live in the config directory. When using `file_write`, "
            "`file_edit`, or `file_read` with config files, always use absolute paths "
            "anchored to this directory — never relative paths.\n\n"
            "You can reconfigure teams at runtime:\n\n"
            "- Use `file_write` or `file_edit` to modify a team's YAML config on disk, "
            "then call `reload_team` with that team's name. "
            "The next time you delegate to that team it will use the updated config.\n"
            "- Use `add_team` to register a new orchestration YAML as a team.\n"
            "- Use `remove_team` to unregister a team you no longer need.\n"
            "- Use `reload_conductor` after modifying the conductor's own config file "
            "to update your system prompt, tools, or team definitions.\n"
        )

    # ------------------------------------------------------------------
    # Team mutation API
    # ------------------------------------------------------------------

    def reload_team(self, team_name: str) -> str:
        """Reload a team's orchestration from its config file on disk.

        If the YAML is invalid the old orchestration is retained and an error
        message is returned so the conductor can report the problem.

        Args:
            team_name: Name of the team to reload.

        Returns:
            Human-readable status string.
        """
        if team_name not in self.config.teams:
            return f"Error: team '{team_name}' is not registered."

        team_def = self.config.teams[team_name]
        team_path = self.config_path.parent / team_def.config
        try:
            new_orch = Orchestration(team_path)
        except Exception as e:
            return (
                f"Error reloading team '{team_name}' from '{team_def.config}': {e}. "
                f"The previous orchestration remains active."
            )

        self._team_orchestrations[team_name] = new_orch
        self._register_team_tool(team_name, team_def, new_orch)
        self._rebuild_agent_type()
        return f"Team '{team_name}' reloaded successfully from '{team_def.config}'."

    def add_team(
        self,
        team_name: str,
        config_path: str,
        orchestration: str,
        description: str,
        output_key: Optional[str] = None,
        input: Optional[Dict[str, str]] = None,
    ) -> str:
        """Register a new team.

        Args:
            team_name: Unique name for the team (becomes the tool name).
            config_path: Path to the team's orchestration YAML, relative to the
                conductor config file.
            orchestration: Name of the orchestration within the team config to run.
            description: Description shown in the manifest and tool schema.
            output_key: Optional context key to extract from the team result.
            input: Optional dict of input parameter names → types.

        Returns:
            Human-readable status string.
        """
        if team_name in _MANAGEMENT_TOOL_NAMES:
            return f"Error: '{team_name}' is a reserved management tool name."

        team_def = TeamDefinition(
            config=config_path,
            orchestration=orchestration,
            description=description,
            input=input or {},
            output=output_key,
        )
        team_path = self.config_path.parent / config_path
        try:
            new_orch = Orchestration(team_path)
        except Exception as e:
            return f"Error loading team '{team_name}' from '{config_path}': {e}."

        self.config.teams[team_name] = team_def
        self._team_orchestrations[team_name] = new_orch
        self._register_team_tool(team_name, team_def, new_orch)
        self._rebuild_agent_type()
        return f"Team '{team_name}' added successfully from '{config_path}'."

    def remove_team(self, team_name: str) -> str:
        """Unregister a team.

        Args:
            team_name: Name of the team to remove.

        Returns:
            Human-readable status string.
        """
        if team_name not in self.config.teams:
            return f"Error: team '{team_name}' is not registered."

        del self.config.teams[team_name]
        del self._team_orchestrations[team_name]
        self.tool_registry.unregister(team_name)
        self._rebuild_agent_type()
        return f"Team '{team_name}' removed successfully."

    def reload_conductor(self) -> str:
        """Reload the conductor's own config from disk.

        Re-reads the conductor YAML, updates the conductor agent type
        (system prompt, tools, model), and re-registers any new or changed
        teams. Existing team orchestrations that haven't changed are kept.

        Returns:
            Human-readable status string.
        """
        try:
            new_config = load_conductor_config(self.config_path)
        except Exception as e:
            return f"Error reloading conductor config: {e}. Previous config retained."

        # Update conductor config
        old_teams = set(self.config.teams.keys())
        self.config = new_config

        new_teams = set(new_config.teams.keys())

        # Remove teams that no longer exist
        for removed in old_teams - new_teams:
            if removed in self._team_orchestrations:
                del self._team_orchestrations[removed]
            self.tool_registry.unregister(removed)

        # Add or update teams
        for team_name in new_teams:
            team_def = new_config.teams[team_name]
            team_path = self.config_path.parent / team_def.config
            try:
                new_orch = Orchestration(team_path)
                self._team_orchestrations[team_name] = new_orch
                self._register_team_tool(team_name, team_def, new_orch)
            except Exception as e:
                if team_name in self._team_orchestrations:
                    # Keep old orchestration if reload fails
                    self._register_team_tool(
                        team_name, team_def, self._team_orchestrations[team_name]
                    )
                else:
                    return (
                        f"Error loading team '{team_name}' from '{team_def.config}': {e}. "
                        f"Conductor reload aborted."
                    )

        self._rebuild_agent_type()
        return "Conductor config reloaded successfully. Updated config active for next response."

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        goal: str,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
    ) -> str:
        """Run the conductor with a single goal and return its final response.

        Args:
            goal: The stakeholder's goal or task description.
            event_bus: Optional EventBus to receive runtime events.
            stream_responses: Whether to stream tokens as they arrive.
            approval_fn: Optional callback for tool-level approval prompts.

        Returns:
            The conductor's final synthesized response.
        """
        if event_bus:
            self._events = event_bus
            self.agent_runner._events = event_bus
        self.agent_runner.stream_responses = stream_responses
        self._approval_fn = approval_fn
        if self._semaphore_cap is not None and self.agent_runner._semaphore is None:
            self.agent_runner._semaphore = asyncio.Semaphore(self._semaphore_cap)

        context = ContextManager("shared_context")
        result = await self.agent_runner.run(
            self._agent_type,
            self._conductor_name,
            goal,
            context,
        )
        return result.get("content", "")

    def run_sync(self, goal: str) -> str:
        """Run the conductor synchronously (convenience method)."""
        return asyncio.run(self.run(goal))

    async def chat(
        self,
        message: str,
        session: Optional[Session] = None,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
    ) -> str:
        """Send a message to the conductor in an ongoing conversation.

        Args:
            message: The stakeholder's message or updated goal.
            session: Conversation session; created fresh if not provided.
            event_bus: Optional EventBus to receive runtime events.
            stream_responses: Whether to stream tokens as they arrive.
            approval_fn: Optional callback for tool-level approval prompts.

        Returns:
            The conductor's response.
        """
        if session is None:
            session = Session()
        if event_bus:
            self._events = event_bus
            self.agent_runner._events = event_bus
        self.agent_runner.stream_responses = stream_responses
        self._approval_fn = approval_fn
        if self._semaphore_cap is not None and self.agent_runner._semaphore is None:
            self.agent_runner._semaphore = asyncio.Semaphore(self._semaphore_cap)

        self._turn_tool_calls = []
        context = ContextManager("shared_context")
        result = await self.agent_runner.run(
            self._agent_type,
            self._conductor_name,
            message,
            context,
            session=session,
        )
        response = result.get("content", "")
        session.add_turn(message, response)

        # Notify cognitive context strategy of the completed turn
        if self._context_strategy is not None:
            await self._context_strategy.on_turn_complete(
                session, message, response,
                tool_calls=self._turn_tool_calls or None,
            )

        tool_calls = self._turn_tool_calls
        write_chat_turn_log(
            session_id=session.id,
            conversation_turns=len(session.message_history) // 2,
            orchestration=self.config_path.stem,
            message=message,
            response_length=len(response),
            result={"tool_calls": tool_calls} if tool_calls else None,
        )

        return response

    async def end_session(self, session: Session) -> None:
        """Notify the context strategy that the chat session has ended.

        Triggers session-end promotion of medium-term memories to long-term.
        No-op if no context strategy is configured.
        """
        if self._context_strategy is not None:
            await self._context_strategy.on_session_end(session)

    def chat_sync(self, message: str, session: Optional[Session] = None) -> str:
        """Chat with the conductor synchronously (convenience method)."""
        return asyncio.run(self.chat(message, session=session))

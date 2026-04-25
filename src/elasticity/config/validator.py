"""Cross-reference validation for configuration."""

from typing import Set
from .schema import (
    Config,
    FlowStep,
    StepInput,
    ParallelStep,
    LoopStep,
    RouteStep,
    SuperviseStep,
    IntervalStep,
    ApproveStep,
    LoadContextStep,
    SaveContextStep,
    ToolCallStep,
)
from ..errors import ValidationError, ConfigReferenceError
from ..tools.builtins import list_builtin_tools


_KNOWN_PROVIDERS = {"openai", "anthropic"}


def validate_references(config: Config) -> None:
    """Validate all cross-references in the configuration.

    Checks:
    - Agent type model strings use the required 'provider/model-name' format
    - Agent types referenced in orchestrations exist
    - Tools referenced in agent types exist in config.tools OR are known builtin names
    - Agent types referenced in can_spawn exist
    - Fallback agents referenced in error strategies exist

    Args:
        config: Configuration to validate

    Raises:
        ConfigReferenceError: If any reference is invalid
    """
    errors: list[str] = []

    # Collect all defined names
    defined_agent_types = set(config.agent_types.keys())
    defined_tools = set(config.tools.keys())
    valid_builtin_tools = set(list_builtin_tools())

    # All resolvable tool names: explicitly defined + known builtins (implicit registration)
    resolvable_tools = defined_tools | valid_builtin_tools

    # Validate agent types
    for agent_name, agent_def in config.agent_types.items():
        # Validate model string format: 'provider/model-name'
        model = agent_def.model
        if "/" not in model:
            errors.append(
                f"Agent type '{agent_name}' has invalid model format '{model}'. "
                "Expected 'provider/model-name' (e.g., 'openai/gpt-4o')."
            )
        else:
            provider, model_name = model.split("/", 1)
            if not provider or not model_name:
                errors.append(
                    f"Agent type '{agent_name}' has invalid model format '{model}': "
                    "provider and model name cannot be empty."
                )
            elif provider not in _KNOWN_PROVIDERS:
                errors.append(
                    f"Agent type '{agent_name}' uses unknown provider '{provider}'. "
                    f"Supported providers: {', '.join(sorted(_KNOWN_PROVIDERS))}."
                )

        # Check referenced tools — allow implicit builtin registration
        for tool_name in agent_def.tools:
            if tool_name not in resolvable_tools:
                errors.append(
                    f"Agent type '{agent_name}' references undefined tool '{tool_name}'"
                )

        # Check referenced spawnable agent types
        for spawnable_name in agent_def.can_spawn:
            if spawnable_name not in defined_agent_types:
                errors.append(
                    f"Agent type '{agent_name}' can_spawn references undefined agent type '{spawnable_name}'"
                )

    # Check for cycles in the can_spawn adjacency graph.
    # A cycle means agent A can spawn B which can (transitively) spawn A again.
    def _has_spawn_cycle(start: str, current: str, visited: Set[str]) -> bool:
        agent_def = config.agent_types.get(current)
        if agent_def is None:
            return False
        for child in agent_def.can_spawn:
            if child == start:
                return True
            if child not in visited:
                visited.add(child)
                if _has_spawn_cycle(start, child, visited):
                    return True
        return False

    for agent_name in defined_agent_types:
        if _has_spawn_cycle(agent_name, agent_name, {agent_name}):
            errors.append(
                f"Agent type '{agent_name}' has a cycle in its can_spawn graph "
                f"(an agent reachable via spawning can spawn back to '{agent_name}')"
            )

        # Validate tool_policies keys reference tools in the agent's tools list
        for policy_tool_name in agent_def.tool_policies:
            if policy_tool_name not in agent_def.tools:
                errors.append(
                    f"Agent type '{agent_name}' tool_policies references tool '{policy_tool_name}' "
                    f"which is not in the agent's tools list"
                )

    # Validate tools
    for tool_name, tool_def in config.tools.items():
        # Check builtin references
        if tool_def.builtin and tool_def.builtin not in valid_builtin_tools:
            errors.append(
                f"Tool '{tool_name}' references unknown built-in tool '{tool_def.builtin}'"
            )

    # Validate orchestrations
    for orch_name, orch_def in config.orchestrations.items():
        # Check error strategy fallback agent
        if orch_def.error_strategy and orch_def.error_strategy.fallback_agent:
            fallback = orch_def.error_strategy.fallback_agent
            if fallback not in defined_agent_types:
                errors.append(
                    f"Orchestration '{orch_name}' error_strategy references undefined agent type '{fallback}'"
                )

        # Validate flow steps
        _validate_flow_steps(orch_name, orch_def.flow, defined_agent_types, resolvable_tools, errors)

    if errors:
        raise ConfigReferenceError(f"Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def _validate_flow_steps(
    context: str,
    steps: list[FlowStep],
    defined_agent_types: Set[str],
    defined_tools: Set[str],
    errors: list[str],
) -> None:
    """Recursively validate flow steps."""
    for step in steps:
        if isinstance(step, StepInput):
            if step.agent and step.agent not in defined_agent_types:
                errors.append(
                    f"{context}: Step references undefined agent type '{step.agent}'"
                )
            if step.spawn_context and not step.spawn_strategy:
                errors.append(
                    f"{context}: Step has 'spawn_context' but no 'spawn_strategy' — "
                    "spawn_context only applies to steps with spawn_strategy: dynamic"
                )
            if step.on_error and step.on_error.fallback_agent:
                fallback = step.on_error.fallback_agent
                if fallback not in defined_agent_types:
                    errors.append(
                        f"{context}: Step error_strategy references undefined agent type '{fallback}'"
                    )
        elif isinstance(step, ParallelStep):
            _validate_flow_steps(context, step.parallel, defined_agent_types, defined_tools, errors)
        elif isinstance(step, LoopStep):
            # Loop body is in step.loop.body (now typed as LoopConfig)
            if step.loop.body:
                _validate_flow_steps(
                    context, step.loop.body, defined_agent_types, defined_tools, errors
                )
        elif isinstance(step, RouteStep):
            for case_steps in step.route.cases.values():
                _validate_flow_steps(context, case_steps, defined_agent_types, defined_tools, errors)
            if step.route.default:
                _validate_flow_steps(context, step.route.default, defined_agent_types, defined_tools, errors)
        elif isinstance(step, SuperviseStep):
            # SuperviseConfig is now typed; supervisor and workers are proper attributes
            if step.supervise.supervisor not in defined_agent_types:
                errors.append(
                    f"{context}: Supervise step references undefined agent type '{step.supervise.supervisor}'"
                )
            for worker in step.supervise.workers:
                if worker.agent not in defined_agent_types:
                    errors.append(
                        f"{context}: Supervise worker references undefined agent type '{worker.agent}'"
                    )
        elif isinstance(step, IntervalStep):
            # IntervalConfig is now typed
            if step.interval.agent not in defined_agent_types:
                errors.append(
                    f"{context}: Interval step references undefined agent type '{step.interval.agent}'"
                )
        elif isinstance(step, ApproveStep):
            # ApproveConfig is now typed; Pydantic validates content/on_reject/max_retries at load time
            pass
        elif isinstance(step, LoadContextStep):
            if step.load_context.memory_tool not in defined_tools:
                errors.append(
                    f"{context}: load_context step references undefined tool '{step.load_context.memory_tool}'"
                )
        elif isinstance(step, SaveContextStep):
            if step.save_context.memory_tool not in defined_tools:
                errors.append(
                    f"{context}: save_context step references undefined tool '{step.save_context.memory_tool}'"
                )
        elif isinstance(step, ToolCallStep):
            tc = step.tool_call
            tool_names: list[str] = []
            if tc.tool:
                tool_names.append(tc.tool)
            if tc.calls:
                tool_names.extend(entry.tool for entry in tc.calls)
            for tool_name in tool_names:
                if tool_name not in defined_tools:
                    errors.append(
                        f"{context}: tool_call step references undefined tool '{tool_name}'"
                    )

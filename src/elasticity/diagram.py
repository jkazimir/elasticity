"""Generate Mermaid sequence diagrams from orchestration configs."""

from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .config.schema import (
    ApproveStep,
    Config,
    FlowStep,
    IntervalStep,
    LoopStep,
    ParallelStep,
    RouteStep,
    StepInput,
    SuperviseStep,
)


def generate_sequence_diagram(config: Config, orchestration_name: str) -> str:
    """Generate a Mermaid sequence diagram for an orchestration.

    Args:
        config: The configuration object
        orchestration_name: Name of the orchestration to diagram

    Returns:
        Mermaid sequence diagram syntax as a string

    Raises:
        KeyError: If orchestration_name is not found in config
    """
    if orchestration_name not in config.orchestrations:
        raise KeyError(f"Orchestration '{orchestration_name}' not found in config")

    orch_def = config.orchestrations[orchestration_name]
    flow = orch_def.flow

    # Collect all agents used in the flow, preserving order of first appearance
    agents = _collect_agents(flow)
    # Preserve order of first appearance by collecting in order
    agent_list = _collect_agents_ordered(flow)

    # Build the diagram
    lines: List[str] = []
    lines.append("sequenceDiagram")

    # Add participants
    for agent in agent_list:
        lines.append(f"    participant {agent}")

    lines.append("")

    # Render the flow steps
    state = {"last_agent": None, "last_output_as": None}
    _render_steps(flow, lines, state, indent=4)

    # Add note for final output if there is one
    if state.get("last_output_as"):
        final_agent = state.get("last_agent")
        final_output = state.get("last_output_as")
        if final_agent and final_output:
            lines.append(f"    Note over {final_agent}: {final_output}")

    return "\n".join(lines)


def _collect_agents(steps: List[Any]) -> Set[str]:
    """Recursively collect all agent names from flow steps."""
    agents: Set[str] = set()

    for step_data in steps:
        step = _parse_step(step_data)
        
        if isinstance(step, StepInput) and step.agent:
            agents.add(step.agent)
        elif isinstance(step, ParallelStep):
            agents.update(_collect_agents(step.parallel))
        elif isinstance(step, LoopStep):
            body = step.loop.body or []
            if body:
                agents.update(_collect_agents(body))
        elif isinstance(step, RouteStep):
            for case_steps in step.route.cases.values():
                agents.update(_collect_agents(case_steps))
            if step.route.default:
                agents.update(_collect_agents(step.route.default))
        elif isinstance(step, SuperviseStep):
            agents.add(step.supervise.supervisor)
            for worker in step.supervise.workers:
                agents.add(worker.agent)
        elif isinstance(step, IntervalStep):
            agents.add(step.interval.agent)
        elif isinstance(step, ApproveStep):
            agents.add("Human")

    return agents


def _collect_agents_ordered(steps: List[Any]) -> List[str]:
    """Collect agents in order of first appearance."""
    seen: Set[str] = set()
    ordered: List[str] = []

    def _collect_from_step_data(step_data: Any):
        step = _parse_step(step_data)
        
        if isinstance(step, StepInput) and step.agent and step.agent not in seen:
            seen.add(step.agent)
            ordered.append(step.agent)
        elif isinstance(step, ParallelStep):
            for branch_step in step.parallel:
                _collect_from_step_data(branch_step)
        elif isinstance(step, LoopStep):
            body = step.loop.body or []
            if body:
                for body_step in body:
                    _collect_from_step_data(body_step)
        elif isinstance(step, RouteStep):
            for case_steps in step.route.cases.values():
                for case_step in case_steps:
                    _collect_from_step_data(case_step)
            if step.route.default:
                for default_step in step.route.default:
                    _collect_from_step_data(default_step)
        elif isinstance(step, SuperviseStep):
            supervisor = step.supervise.supervisor
            if supervisor not in seen:
                seen.add(supervisor)
                ordered.append(supervisor)
            for worker in step.supervise.workers:
                agent = worker.agent
                if agent not in seen:
                    seen.add(agent)
                    ordered.append(agent)
        elif isinstance(step, IntervalStep):
            agent = step.interval.agent
            if agent not in seen:
                    seen.add(agent)
                    ordered.append(agent)
        elif isinstance(step, ApproveStep):
            if "Human" not in seen:
                seen.add("Human")
                ordered.append("Human")

    for step_data in steps:
        _collect_from_step_data(step_data)

    return ordered


def _parse_step(step_data: Any) -> FlowStep:
    """Parse a step (which might be a FlowStep object or a raw dict) into a FlowStep."""
    if isinstance(step_data, FlowStep):
        return step_data
    elif isinstance(step_data, dict):
        # Try to parse as a FlowStep using Pydantic
        # Check which type it is by looking for keys
        if "parallel" in step_data:
            # Parse parallel steps recursively
            parallel_steps = [_parse_step(s) for s in step_data["parallel"]]
            return ParallelStep(parallel=parallel_steps)
        elif "loop" in step_data:
            from .config.schema import LoopConfig
            return LoopStep(loop=LoopConfig.model_validate(step_data["loop"]))
        elif "route" in step_data:
            return RouteStep(route=step_data["route"])
        elif "supervise" in step_data:
            from .config.schema import SuperviseConfig
            return SuperviseStep(supervise=SuperviseConfig.model_validate(step_data["supervise"]))
        elif "interval" in step_data:
            from .config.schema import IntervalConfig
            return IntervalStep(interval=IntervalConfig.model_validate(step_data["interval"]))
        elif "approve" in step_data:
            from .config.schema import ApproveConfig
            return ApproveStep(approve=ApproveConfig.model_validate(step_data["approve"]))
        elif "agent" in step_data:
            return StepInput.model_validate(step_data)
        else:
            # Default to StepInput (might be empty dict or unknown structure)
            try:
                return StepInput.model_validate(step_data)
            except Exception:
                # If it fails, return an empty StepInput
                return StepInput()
    else:
        raise ValueError(f"Unexpected step type: {type(step_data)}")


def _render_steps(
    steps: List[Any],
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent: int = 4,
) -> None:
    """Recursively render flow steps to Mermaid syntax.

    Args:
        steps: List of flow steps to render (may be FlowStep objects or raw dicts)
        lines: List to append Mermaid lines to
        state: Dict tracking 'last_agent' and 'last_output_as' for arrow drawing
        indent: Current indentation level (spaces)
    """
    indent_str = " " * indent

    for step_data in steps:
        step = _parse_step(step_data)
        
        if isinstance(step, StepInput):
            _render_step_input(step, lines, state, indent_str)
        elif isinstance(step, ParallelStep):
            _render_parallel(step, lines, state, indent_str)
        elif isinstance(step, LoopStep):
            _render_loop(step, lines, state, indent_str)
        elif isinstance(step, RouteStep):
            _render_route(step, lines, state, indent_str)
        elif isinstance(step, SuperviseStep):
            _render_supervise(step, lines, state, indent_str)
        elif isinstance(step, IntervalStep):
            _render_interval(step, lines, state, indent_str)
        elif isinstance(step, ApproveStep):
            _render_approve(step, lines, state, indent_str)


def _render_step_input(
    step: StepInput,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render a StepInput (agent call)."""
    if not step.agent:
        return

    agent = step.agent
    last_agent = state.get("last_agent")
    last_output_as = state.get("last_output_as")
    is_parallel_context = state.get("_is_parallel", False)

    # Check if we're coming from a parallel block
    parallel_outputs = state.get("_parallel_outputs")
    if parallel_outputs:
        # Draw arrows from all parallel branch agents
        for branch_agent, branch_output_as in parallel_outputs:
            if branch_agent and branch_output_as and branch_agent != agent:
                lines.append(f"{indent_str}{branch_agent}->>{agent}: {branch_output_as}")
        # Clear parallel outputs after using them
        state.pop("_parallel_outputs", None)
    elif last_agent and last_output_as and not is_parallel_context and last_agent != agent:
        # Draw arrow from previous agent (or note if first step)
        # Skip self-arrows
        lines.append(f"{indent_str}{last_agent}->>{agent}: {last_output_as}")
    elif last_agent == agent and last_output_as:
        # Same agent consuming its own output - skip arrow, just show note if needed
        pass
    else:
        # First step or after parallel/route - use note
        if last_output_as and not is_parallel_context:
            lines.append(f"{indent_str}Note over {agent}: {last_output_as}")
        else:
            # Very first step or in parallel context - show initial input if available
            if step.input and not is_parallel_context:
                summary = _summarize_input(step.input)
                lines.append(f"{indent_str}Note over {agent}: {summary}")

    # Handle spawn strategy
    if step.spawn_strategy == "dynamic":
        if step.collect_as:
            lines.append(
                f"{indent_str}Note over {agent}: spawns dynamically, collects as {step.collect_as}"
            )
        else:
            lines.append(f"{indent_str}Note over {agent}: spawns dynamically")

    # Show output_as as a note if in parallel context
    if step.output_as and is_parallel_context:
        lines.append(f"{indent_str}Note over {agent}: {step.output_as}")

    # Update state with this agent's output
    if step.output_as:
        state["last_agent"] = agent
        state["last_output_as"] = step.output_as
    else:
        # No output_as means this is a terminal step, but still update last_agent
        state["last_agent"] = agent
        state["last_output_as"] = None


def _render_parallel(
    step: ParallelStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render a ParallelStep."""
    lines.append(f"{indent_str}par")

    # Track outputs from each parallel branch
    branch_outputs: List[Tuple[Optional[str], Optional[str]]] = []

    for i, branch_step in enumerate(step.parallel):
        if i > 0:
            lines.append(f"{indent_str}and")

        # For parallel branches, they don't have a single predecessor
        # So we reset the state for each branch and mark as parallel context
        branch_state = {
            "last_agent": None,
            "last_output_as": None,
            "_is_parallel": True,
        }

        # Render branch
        _render_steps([branch_step], lines, branch_state, indent=len(indent_str) + 4)

        # Collect branch output
        branch_outputs.append((branch_state.get("last_agent"), branch_state.get("last_output_as")))

    lines.append(f"{indent_str}end")

    # Store parallel branch outputs for use by next sequential step
    state["_parallel_outputs"] = branch_outputs

    # After parallel block, track all branch outputs
    # The next sequential step will receive all outputs via template variables
    # We'll use the first non-None output for state tracking
    for agent, output_as in branch_outputs:
        if agent and output_as:
            state["last_agent"] = agent
            state["last_output_as"] = output_as
            break


def _render_loop(
    step: LoopStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render a LoopStep."""
    loop_config = step.loop
    max_iterations = loop_config.max_iterations
    until = loop_config.until
    body = loop_config.body or []

    # Build loop label
    label_parts = []
    if until:
        label_parts.append(f"until {until}")
    if max_iterations:
        label_parts.append(f"max {max_iterations}")
    label = " (" + ", ".join(label_parts) + ")" if label_parts else ""

    # If there's a pre-loop handoff to a different agent, draw it before the loop
    # block so the arrow visually crosses the loop border rather than expanding
    # the box to include the pre-loop agent's column.
    pre_loop_agent = state.get("last_agent")
    pre_loop_output = state.get("last_output_as")
    first_body_agent = None
    for body_step_data in body:
        body_step = _parse_step(body_step_data)
        if isinstance(body_step, StepInput) and body_step.agent:
            first_body_agent = body_step.agent
            break

    drew_pre_loop_arrow = False
    if pre_loop_agent and pre_loop_output and first_body_agent and pre_loop_agent != first_body_agent:
        lines.append(f"{indent_str}{pre_loop_agent}->>{first_body_agent}: {pre_loop_output}")
        drew_pre_loop_arrow = True

    lines.append(f"{indent_str}loop{label}")

    # If we already drew the handoff arrow, tell the body renderer the first agent
    # already received it so it doesn't re-render.  Otherwise, don't carry
    # last_agent in — that would draw the arrow *inside* the box, expanding it.
    if drew_pre_loop_arrow:
        loop_state = {"last_agent": first_body_agent, "last_output_as": pre_loop_output}
    else:
        loop_state = {"last_agent": None, "last_output_as": pre_loop_output}

    # Render loop body
    _render_steps(body, lines, loop_state, indent=len(indent_str) + 4)

    lines.append(f"{indent_str}end")

    # For loops, track the output that persists across iterations
    # Typically this is the first output_as in the loop body that gets updated
    # Find the first output_as in the body that's likely the loop's output
    loop_output_agent = None
    loop_output_as = None
    for body_step_data in body:
        body_step = _parse_step(body_step_data)
        if isinstance(body_step, StepInput) and body_step.output_as:
            # Use the first output_as as the loop's output
            # In practice, this is usually the variable that persists across iterations
            loop_output_agent = body_step.agent
            loop_output_as = body_step.output_as
            break

    # Update state with loop's output (prefer the first output_as, or last if not found)
    if loop_output_agent and loop_output_as:
        state["last_agent"] = loop_output_agent
        state["last_output_as"] = loop_output_as
    elif loop_state.get("last_agent") and loop_state.get("last_output_as"):
        state["last_agent"] = loop_state["last_agent"]
        state["last_output_as"] = loop_state["last_output_as"]


def _render_route(
    step: RouteStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render a RouteStep."""
    route = step.route

    cases = list(route.cases.items())
    has_default = route.default is not None

    if not cases and not has_default:
        return

    # Render first case as alt
    case_name, case_steps = cases[0]
    lines.append(f"{indent_str}alt {case_name}")

    # Save state before case
    case_state = {"last_agent": state.get("last_agent"), "last_output_as": state.get("last_output_as")}

    _render_steps(case_steps, lines, case_state, indent=len(indent_str) + 4)

    # Render remaining cases as else
    for case_name, case_steps in cases[1:]:
        lines.append(f"{indent_str}else {case_name}")

        case_state = {"last_agent": state.get("last_agent"), "last_output_as": state.get("last_output_as")}
        _render_steps(case_steps, lines, case_state, indent=len(indent_str) + 4)

    # Render default if present
    default_state = None
    if has_default:
        lines.append(f"{indent_str}else default")

        default_state = {"last_agent": state.get("last_agent"), "last_output_as": state.get("last_output_as")}
        _render_steps(route.default, lines, default_state, indent=len(indent_str) + 4)

    lines.append(f"{indent_str}end")

    # Use the last case's output (or default if present)
    final_state = default_state if has_default else case_state
    if final_state and final_state.get("last_agent") and final_state.get("last_output_as"):
        state["last_agent"] = final_state["last_agent"]
        state["last_output_as"] = final_state["last_output_as"]


def _render_supervise(
    step: SuperviseStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render a SuperviseStep."""
    sup_cfg = step.supervise
    supervisor = sup_cfg.supervisor
    on_reject = sup_cfg.on_reject
    max_retries = sup_cfg.max_retries

    # Draw arrow from previous agent to supervisor
    last_agent = state.get("last_agent")
    last_output_as = state.get("last_output_as")

    if last_agent and last_output_as:
        lines.append(f"{indent_str}{last_agent}->>{supervisor}: {last_output_as}")
    else:
        lines.append(f"{indent_str}Note over {supervisor}: supervise")

    # Render each worker
    for worker in sup_cfg.workers:
        lines.append(f"{indent_str}{supervisor}->>{worker.agent}: task")
        if worker.output_as:
            lines.append(f"{indent_str}{worker.agent}-->>{supervisor}: {worker.output_as}")

    # Add note about reject policy
    if on_reject:
        lines.append(f"{indent_str}Note over {supervisor}: on_reject={on_reject}, max_retries={max_retries}")

    # Update state with supervisor's output (if any)
    # In practice, supervise step might not have explicit output_as, so we keep previous
    # But if there's a common pattern, we could track it


def _render_interval(
    step: IntervalStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render an IntervalStep."""
    int_cfg = step.interval
    agent = int_cfg.agent
    every = int_cfg.every
    until = int_cfg.until

    # Build loop label
    label_parts = [f"every {every}"]
    if until:
        label_parts.append(f"until {until}")
    label = " (" + ", ".join(label_parts) + ")"

    lines.append(f"{indent_str}loop{label}")

    # Draw arrow from previous agent (or note)
    last_agent = state.get("last_agent")
    last_output_as = state.get("last_output_as")

    if last_agent and last_output_as:
        lines.append(f"{indent_str}    {last_agent}->>{agent}: {last_output_as}")
    else:
        if int_cfg.input:
            summary = _summarize_input(int_cfg.input)
            lines.append(f"{indent_str}    Note over {agent}: {summary}")

    # Show output if present
    if int_cfg.output_as:
        lines.append(f"{indent_str}    Note over {agent}: {int_cfg.output_as}")

    lines.append(f"{indent_str}end")

    # Update state
    if int_cfg.output_as:
        state["last_agent"] = agent
        state["last_output_as"] = int_cfg.output_as


def _render_approve(
    step: ApproveStep,
    lines: List[str],
    state: Dict[str, Optional[str]],
    indent_str: str,
) -> None:
    """Render an ApproveStep (human-in-the-loop approval gate)."""
    app_cfg = step.approve
    message = app_cfg.message or "Review and approve"
    output_as = app_cfg.output_as
    on_reject = app_cfg.on_reject
    max_retries = app_cfg.max_retries

    last_agent = state.get("last_agent")
    last_output_as = state.get("last_output_as")

    # Hand-off arrow from previous agent to Human
    if last_agent and last_output_as:
        lines.append(f"{indent_str}{last_agent}->>Human: {last_output_as}")

    # Show the approval prompt
    truncated_message = message.split("\n")[0].strip()
    if len(truncated_message) > 60:
        truncated_message = truncated_message[:57] + "..."
    lines.append(f"{indent_str}Note over Human: {truncated_message}")

    # Show reject policy if non-default
    policy_parts = []
    if on_reject:
        policy_parts.append(f"on_reject={on_reject}")
    if max_retries is not None:
        policy_parts.append(f"max_retries={max_retries}")
    if policy_parts:
        lines.append(f"{indent_str}Note over Human: {', '.join(policy_parts)}")

    # Return arrow back to previous agent with the approval result
    if output_as:
        if last_agent:
            lines.append(f"{indent_str}Human-->>{last_agent}: {output_as}")
            state["last_agent"] = last_agent
        state["last_output_as"] = output_as
    else:
        state["last_output_as"] = None


def _summarize_input(input_text: str) -> str:
    """Summarize input text for use in diagram labels."""
    if not input_text:
        return ""

    # Take first line, truncate to ~60 chars
    first_line = input_text.split("\n")[0].strip()
    if len(first_line) > 60:
        return first_line[:57] + "..."
    return first_line

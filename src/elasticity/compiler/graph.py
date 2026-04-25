"""Execution graph representation and building."""

from typing import Any, Dict, List, Optional, Set
from enum import Enum
from dataclasses import dataclass, field

from ..config.schema import (
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
    OrchestrationDefinition,
)


class NodeType(Enum):
    """Type of execution graph node."""

    AGENT = "agent"
    PARALLEL = "parallel"
    LOOP = "loop"
    ROUTE = "route"
    SPAWN = "spawn"
    SUPERVISE = "supervise"
    INTERVAL = "interval"
    APPROVE = "approve"
    LOAD_CONTEXT = "load_context"
    SAVE_CONTEXT = "save_context"
    TOOL_CALL = "tool_call"


@dataclass
class GraphNode:
    """A node in the execution graph."""

    node_id: str
    node_type: NodeType
    config: Dict[str, Any] = field(default_factory=dict)
    children: List[str] = field(default_factory=list)  # Child node IDs
    next: Optional[str] = None  # Next node ID (for sequence)
    parent: Optional[str] = None  # Parent node ID


@dataclass
class ExecutionGraph:
    """Execution graph for an orchestration."""

    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    entry_node: Optional[str] = None  # Entry point node ID


class GraphBuilder:
    """Builds execution graphs from orchestration definitions."""

    def __init__(self, config: Config):
        self.config = config
        self._node_counter = 0

    def build(self, orchestration_name: str) -> ExecutionGraph:
        """Build execution graph for an orchestration.

        Args:
            orchestration_name: Name of orchestration to build graph for

        Returns:
            Execution graph
        """
        if orchestration_name not in self.config.orchestrations:
            raise ValueError(f"Orchestration '{orchestration_name}' not found")

        orch = self.config.orchestrations[orchestration_name]
        graph = ExecutionGraph()

        if not orch.flow:
            return graph

        # Build graph from flow steps
        entry_node_id = self._build_flow_steps(graph, orch.flow, None)
        graph.entry_node = entry_node_id

        return graph

    def _new_node_id(self) -> str:
        """Generate a new unique node ID."""
        node_id = f"node_{self._node_counter}"
        self._node_counter += 1
        return node_id

    def _build_flow_steps(
        self, graph: ExecutionGraph, steps: List[FlowStep], parent_id: Optional[str]
    ) -> Optional[str]:
        """Build graph nodes from a list of flow steps.

        Returns the ID of the first node created, or None if no steps.
        """
        if not steps:
            return None

        first_node_id = None
        prev_node_id = None

        for step in steps:
            node_id = self._build_step(graph, step, parent_id)

            if first_node_id is None:
                first_node_id = node_id

            # Link sequential steps
            if prev_node_id is not None:
                graph.nodes[prev_node_id].next = node_id

            # Find the last node in this step's subgraph for chaining
            prev_node_id = self._get_last_node(graph, node_id)

        return first_node_id

    def _parse_step(self, step: Any) -> FlowStep:
        """Parse a raw dict (e.g. from loop body) into a FlowStep."""
        if not isinstance(step, dict):
            return step
        # Discriminate by top-level key
        if "parallel" in step:
            return ParallelStep.model_validate(step)
        if "loop" in step:
            return LoopStep.model_validate(step)
        if "route" in step:
            return RouteStep.model_validate(step)
        if "supervise" in step:
            return SuperviseStep.model_validate(step)
        if "interval" in step:
            return IntervalStep.model_validate(step)
        if "approve" in step:
            return ApproveStep.model_validate(step)
        if "load_context" in step:
            return LoadContextStep.model_validate(step)
        if "save_context" in step:
            return SaveContextStep.model_validate(step)
        if "tool_call" in step:
            return ToolCallStep.model_validate(step)
        # Default: agent step
        return StepInput.model_validate(step)

    def _build_step(
        self, graph: ExecutionGraph, step: FlowStep, parent_id: Optional[str]
    ) -> str:
        """Build a graph node from a flow step."""
        step = self._parse_step(step)
        if isinstance(step, StepInput):
            return self._build_agent_step(graph, step, parent_id)
        elif isinstance(step, ParallelStep):
            return self._build_parallel_step(graph, step, parent_id)
        elif isinstance(step, LoopStep):
            return self._build_loop_step(graph, step, parent_id)
        elif isinstance(step, RouteStep):
            return self._build_route_step(graph, step, parent_id)
        elif isinstance(step, SuperviseStep):
            return self._build_supervise_step(graph, step, parent_id)
        elif isinstance(step, IntervalStep):
            return self._build_interval_step(graph, step, parent_id)
        elif isinstance(step, ApproveStep):
            return self._build_approve_step(graph, step, parent_id)
        elif isinstance(step, LoadContextStep):
            return self._build_load_context_step(graph, step, parent_id)
        elif isinstance(step, SaveContextStep):
            return self._build_save_context_step(graph, step, parent_id)
        elif isinstance(step, ToolCallStep):
            return self._build_tool_call_step(graph, step, parent_id)
        else:
            raise ValueError(f"Unknown step type: {type(step)}")

    def _build_agent_step(
        self, graph: ExecutionGraph, step: StepInput, parent_id: Optional[str]
    ) -> str:
        """Build an agent execution node."""
        node_id = self._new_node_id()
        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.AGENT,
            config={
                "agent": step.agent,
                "input": step.input,
                "output_as": step.output_as,
                "on_error": step.on_error.model_dump() if step.on_error else None,
                "spawn_strategy": step.spawn_strategy,
                "collect_as": step.collect_as,
                "spawn_context": step.spawn_context,
            },
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_parallel_step(
        self, graph: ExecutionGraph, step: ParallelStep, parent_id: Optional[str]
    ) -> str:
        """Build a parallel execution node."""
        node_id = self._new_node_id()
        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.PARALLEL,
            config={},
            parent=parent_id,
        )

        # Build child nodes for parallel steps
        child_ids = []
        for child_step in step.parallel:
            child_id = self._build_step(graph, child_step, node_id)
            child_ids.append(child_id)

        node.children = child_ids
        graph.nodes[node_id] = node
        return node_id

    def _build_loop_step(
        self, graph: ExecutionGraph, step: LoopStep, parent_id: Optional[str]
    ) -> str:
        """Build a loop execution node."""
        node_id = self._new_node_id()
        loop_cfg = step.loop

        # Build body nodes from typed LoopConfig
        body_steps = loop_cfg.body or []
        body_entry = self._build_flow_steps(graph, body_steps, node_id)

        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.LOOP,
            config={
                "max_iterations": loop_cfg.max_iterations,
                "until": loop_cfg.until,
            },
            children=[body_entry] if body_entry else [],
            parent=parent_id,
        )

        graph.nodes[node_id] = node
        return node_id

    def _build_route_step(
        self, graph: ExecutionGraph, step: RouteStep, parent_id: Optional[str]
    ) -> str:
        """Build a route execution node."""
        node_id = self._new_node_id()
        route_config = step.route

        # Build case branches
        case_nodes = {}
        for case_value, case_steps in route_config.cases.items():
            case_entry = self._build_flow_steps(graph, case_steps, node_id)
            if case_entry:
                case_nodes[case_value] = case_entry

        # Build default branch if present
        default_entry = None
        if route_config.default:
            default_entry = self._build_flow_steps(graph, route_config.default, node_id)

        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.ROUTE,
            config={
                "condition": route_config.condition,
                "cases": case_nodes,
                "default": default_entry,
            },
            children=list(case_nodes.values()) + ([default_entry] if default_entry else []),
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_supervise_step(
        self, graph: ExecutionGraph, step: SuperviseStep, parent_id: Optional[str]
    ) -> str:
        """Build a supervise execution node."""
        node_id = self._new_node_id()
        sup_cfg = step.supervise

        # Build worker nodes from typed SuperviseConfig
        worker_ids = []
        for worker in sup_cfg.workers:
            # worker.input is the canonical field (normalized from deprecated 'task')
            worker_step = StepInput(
                agent=worker.agent,
                input=worker.input,
                output_as=worker.output_as,
            )
            worker_id = self._build_step(graph, worker_step, node_id)
            worker_ids.append(worker_id)

        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.SUPERVISE,
            config={
                "supervisor": sup_cfg.supervisor,
                "on_reject": sup_cfg.on_reject,
                "max_retries": sup_cfg.max_retries,
                "workers": worker_ids,
            },
            children=worker_ids,
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_interval_step(
        self, graph: ExecutionGraph, step: IntervalStep, parent_id: Optional[str]
    ) -> str:
        """Build an interval execution node."""
        node_id = self._new_node_id()
        int_cfg = step.interval

        # Build the agent step from typed IntervalConfig
        # (agent and output_as are required fields validated by Pydantic)
        agent_step = StepInput(
            agent=int_cfg.agent,
            input=int_cfg.input or "",
            output_as=int_cfg.output_as,
        )
        agent_node_id = self._build_step(graph, agent_step, node_id)

        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.INTERVAL,
            config={
                "every": int_cfg.every,
                "until": int_cfg.until,
            },
            children=[agent_node_id],
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_approve_step(
        self, graph: ExecutionGraph, step: ApproveStep, parent_id: Optional[str]
    ) -> str:
        """Build a human approval execution node."""
        node_id = self._new_node_id()
        app_cfg = step.approve
        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.APPROVE,
            config={
                "content": app_cfg.content,
                "message": app_cfg.message or "Review and approve:",
                "output_as": app_cfg.output_as,
                "on_reject": app_cfg.on_reject,
                "max_retries": app_cfg.max_retries,
            },
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_load_context_step(
        self, graph: ExecutionGraph, step: LoadContextStep, parent_id: Optional[str]
    ) -> str:
        """Build a load_context node (zero-LLM memory retrieval into context)."""
        node_id = self._new_node_id()
        lc_cfg = step.load_context
        # Serialize LoadContextEntry objects to dicts so executor can use .get() without changes
        load_entries = [
            {"key": e.key, "as": e.as_, "read_file": e.read_file}
            for e in lc_cfg.load
        ]
        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.LOAD_CONTEXT,
            config={
                "memory_tool": lc_cfg.memory_tool,
                "load": load_entries,
                "output_as": lc_cfg.output_as,
            },
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_save_context_step(
        self, graph: ExecutionGraph, step: SaveContextStep, parent_id: Optional[str]
    ) -> str:
        """Build a save_context node (zero-LLM context variable persistence to memory)."""
        node_id = self._new_node_id()
        sc_cfg = step.save_context
        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.SAVE_CONTEXT,
            config={
                "memory_tool": sc_cfg.memory_tool,
                "save": sc_cfg.save,
            },
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _build_tool_call_step(
        self, graph: ExecutionGraph, step: ToolCallStep, parent_id: Optional[str]
    ) -> str:
        """Build a tool_call node (zero-LLM direct tool invocation)."""
        node_id = self._new_node_id()
        tc_cfg = step.tool_call

        # Normalize single-tool shorthand into calls list
        if tc_cfg.tool:
            calls = [{"tool": tc_cfg.tool, "parameters": tc_cfg.parameters or {}, "output_as": tc_cfg.output_as}]
        else:
            calls = [
                {"tool": e.tool, "parameters": e.parameters, "output_as": e.output_as}
                for e in (tc_cfg.calls or [])
            ]

        node = GraphNode(
            node_id=node_id,
            node_type=NodeType.TOOL_CALL,
            config={
                "calls": calls,
                "output_as": tc_cfg.output_as,
                "on_error": tc_cfg.on_error,
            },
            parent=parent_id,
        )
        graph.nodes[node_id] = node
        return node_id

    def _get_last_node(self, graph: ExecutionGraph, start_node_id: str) -> str:
        """Get the last node in a chain starting from start_node_id."""
        current = start_node_id
        while current and current in graph.nodes:
            node = graph.nodes[current]
            if node.next is None:
                return current
            current = node.next
        return start_node_id

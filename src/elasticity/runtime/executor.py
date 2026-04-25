"""Async execution engine for orchestrations."""

import asyncio
import time
import uuid
import re
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

import structlog

from ..compiler.graph import ExecutionGraph, GraphNode, NodeType
from ..config.schema import Config, AgentTypeDefinition, ErrorStrategy
from ..runtime.agent import AgentRunner, ApprovalFn
from ..runtime.context import ContextManager
from ..runtime.context_strategy import ContextStrategy
from ..runtime.tools import ToolRegistry
from ..runtime.spawn import SpawnManager
from ..runtime.scheduler import IntervalScheduler
from ..runtime.session import Session
from ..events import (
    EventBus,
    NodeStarted,
    NodeCompleted,
    NodeError,
    NodeRetrying,
    AgentStarted,
    AgentCompleted,
    AgentErrorEvent,
    LoopIteration,
    RouteTaken,
    ParallelStarted,
    ParallelCompleted,
    SpawnStarted,
    SpawnCompleted,
    SpawnParseFailed,
    SpawnWaveStarted,
    SpawnWaveCompleted,
    SupervisorWorkerStarted,
    SupervisorReview,
    SupervisorAccepted,
    SupervisorRejected,
    OrchestrationStarted,
    OrchestrationCompleted,
    InterruptReceived,
    OrchestrationInterruptedEvent,
    ApprovalRequested,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalEdited,
    ToolCallStepStarted,
    ToolCallStepCompleted,
)
from ..errors import ExecutionError, AgentError, OrchestrationInterrupted
from ..runtime.input_handler import InputHandler, UserInput

logger = structlog.get_logger(__name__)


@dataclass
class HumanApprovalResult:
    """Result from a human approval prompt."""

    decision: Literal["approve", "reject", "edit"]
    feedback: Optional[str] = None
    edited_content: Optional[str] = None


# Callable[[message, content], Awaitable[HumanApprovalResult]]
HumanApprovalFn = Callable[[str, str], Awaitable[HumanApprovalResult]]


class Executor:
    """Executes orchestration graphs."""

    def __init__(
        self,
        config: Config,
        tool_registry: ToolRegistry,
        event_bus: Optional[EventBus] = None,
        stream_responses: bool = False,
        approval_fn: Optional[ApprovalFn] = None,
        human_approval_fn: Optional[HumanApprovalFn] = None,
        context_strategy: Optional[ContextStrategy] = None,
    ):
        self.config = config
        self.tool_registry = tool_registry
        self._events = event_bus or EventBus()
        self.agent_runner = AgentRunner(
            tool_registry, self._events, approval_fn=approval_fn,
            context_strategy=context_strategy,
        )
        self.agent_runner.stream_responses = stream_responses
        self.spawn_manager = SpawnManager()
        self.scheduler = IntervalScheduler()
        self._run_id = str(uuid.uuid4())
        self._run_start: float = 0.0
        self._human_approval_fn = human_approval_fn
        self._input_handler: Optional[InputHandler] = None
        self._orchestration_name: str = ""

    async def execute(
        self,
        graph: ExecutionGraph,
        orchestration_name: str,
        context: ContextManager,
        input_data: Optional[Dict[str, Any]] = None,
        default_error_strategy: Optional[ErrorStrategy] = None,
        session: Optional[Session] = None,
        input_handler: Optional[InputHandler] = None,
    ) -> Dict[str, Any]:
        """Execute an orchestration graph."""
        self._run_start = time.monotonic()
        self._input_handler = input_handler
        self._orchestration_name = orchestration_name
        self._events.emit(OrchestrationStarted(
            run_id=self._run_id,
            orchestration_name=orchestration_name,
        ))

        if input_data:
            context.set_initial_input(input_data)
            context.update_shared(input_data)

        if session:
            context.update_shared(session.context)

        if graph.entry_node is None:
            result = context.to_dict()
            if session:
                session.context.update(result)
            duration_ms = (time.monotonic() - self._run_start) * 1000
            self._events.emit(OrchestrationCompleted(
                run_id=self._run_id,
                orchestration_name=orchestration_name,
                duration_ms=duration_ms,
            ))
            return result

        try:
            await self._execute_node(graph, graph.entry_node, context, default_error_strategy, session)
        except OrchestrationInterrupted:
            raise
        finally:
            self._input_handler = None
            await self.scheduler.cancel_all()

        # Only emit OrchestrationCompleted on the success path (after the try/finally
        # exits normally).  Exceptions propagate through the finally block above
        # without reaching this point.
        duration_ms = (time.monotonic() - self._run_start) * 1000
        self._events.emit(OrchestrationCompleted(
            run_id=self._run_id,
            orchestration_name=orchestration_name,
            duration_ms=duration_ms,
        ))

        result = context.to_dict()
        if session:
            session.context.update(result)

        return result

    def _check_interrupt(self, node_id: str, context: ContextManager) -> Optional[UserInput]:
        """Check for interrupt at a checkpoint. Returns UserInput if graceful, raises if cancel."""
        if not self._input_handler or self._input_handler.config.mode != "interrupt":
            return None
        if not self._input_handler.has_interrupt():
            return None

        ui = self._input_handler.get_interrupt()
        if not ui:
            return None

        behavior = self._input_handler.config.interrupt_behavior or "cancel"
        self._events.emit(InterruptReceived(
            message=ui.message,
            orchestration=self._orchestration_name,
            current_node=node_id,
            behavior=behavior,
        ))

        if behavior == "cancel":
            self._events.emit(OrchestrationInterruptedEvent(
                orchestration=self._orchestration_name,
                interrupted_at_node=node_id,
                interrupt_message=ui.message,
            ))
            raise OrchestrationInterrupted(ui.message, node_id)

        # Graceful: deliver via configured mechanisms
        delivery = self._input_handler.config.interrupt_delivery or []
        if "context" in delivery:
            context.set_shared("_interrupt", {
                "message": ui.message,
                "timestamp": ui.timestamp,
                "acknowledged": False,
            })
        # "event" already emitted via InterruptReceived
        # "agent" is handled by passing ui to agent runner
        return ui

    def _check_interrupt_cancel_only(self, node_id: str) -> None:
        """Check for cancel interrupt only (e.g. during streaming). Raises if cancel."""
        if not self._input_handler or self._input_handler.config.mode != "interrupt":
            return
        ui = self._input_handler.peek_interrupt()
        if not ui:
            return
        behavior = self._input_handler.config.interrupt_behavior or "cancel"
        if behavior == "cancel":
            self._input_handler.get_interrupt()  # consume
            self._events.emit(InterruptReceived(
                message=ui.message,
                orchestration=self._orchestration_name,
                current_node=node_id,
                behavior=behavior,
            ))
            self._events.emit(OrchestrationInterruptedEvent(
                orchestration=self._orchestration_name,
                interrupted_at_node=node_id,
                interrupt_message=ui.message,
            ))
            raise OrchestrationInterrupted(ui.message, node_id)
        # Graceful: leave interrupt for next checkpoint, don't consume

    async def _execute_node(
        self,
        graph: ExecutionGraph,
        node_id: str,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute a single graph node, applying retry logic when configured."""
        if node_id not in graph.nodes:
            raise ExecutionError(f"Node '{node_id}' not found in graph")

        # Interrupt checkpoint: between nodes
        self._check_interrupt(node_id, context)

        node = graph.nodes[node_id]
        error_strategy = self._get_error_strategy(node, default_error_strategy)
        self._events.emit(NodeStarted(step_id=node_id, node_type=node.node_type.value))

        try:
            await self._execute_with_retry(graph, node, context, error_strategy, default_error_strategy, session)
            self._events.emit(NodeCompleted(step_id=node_id))

            if node.next:
                await self._execute_node(graph, node.next, context, default_error_strategy, session)

        except Exception as e:
            self._events.emit(NodeError(step_id=node_id, error=str(e)))
            await self._handle_error(e, node, error_strategy, graph, context, default_error_strategy, session)

    async def _execute_with_retry(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        error_strategy: Optional[ErrorStrategy],
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session],
    ) -> None:
        """Execute node dispatch, retrying with backoff when the error strategy is 'retry'."""
        is_retry = error_strategy is not None and error_strategy.strategy == "retry"
        max_attempts = (error_strategy.max_retries + 1) if is_retry else 1

        last_error: Optional[Exception] = None
        for attempt in range(max_attempts):
            if attempt > 0:
                delay = self._compute_backoff(error_strategy.backoff, attempt)  # type: ignore[union-attr]
                self._events.emit(NodeRetrying(
                    step_id=node.node_id,
                    attempt=attempt,
                    delay_seconds=delay,
                ))
                logger.warning(
                    "Retrying node after error",
                    node_id=node.node_id,
                    attempt=attempt,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)

            try:
                await self._dispatch_node(graph, node, context, default_error_strategy, session)
                return  # success
            except Exception as e:
                last_error = e
                if not is_retry or attempt >= max_attempts - 1:
                    raise

        raise last_error  # type: ignore[misc]  # unreachable but satisfies type checker

    def _compute_backoff(self, backoff_mode: str, attempt: int) -> float:
        """Compute delay in seconds for a retry attempt (1-indexed).

        exponential: 2^attempt seconds, capped at 60s
        linear: attempt * 2 seconds
        fixed: 2 seconds
        """
        if backoff_mode == "exponential":
            return min(2.0 ** attempt, 60.0)
        elif backoff_mode == "linear":
            return float(attempt * 2)
        else:  # fixed
            return 2.0

    async def _dispatch_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session],
    ) -> None:
        """Route execution to the appropriate node handler."""
        if node.node_type == NodeType.AGENT:
            await self._execute_agent_node(node, context, default_error_strategy, session)
        elif node.node_type == NodeType.PARALLEL:
            await self._execute_parallel_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.LOOP:
            await self._execute_loop_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.ROUTE:
            await self._execute_route_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.SUPERVISE:
            await self._execute_supervise_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.INTERVAL:
            await self._execute_interval_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.APPROVE:
            await self._execute_approve_node(graph, node, context, default_error_strategy, session)
        elif node.node_type == NodeType.LOAD_CONTEXT:
            await self._execute_load_context_node(node, context)
        elif node.node_type == NodeType.SAVE_CONTEXT:
            await self._execute_save_context_node(node, context)
        elif node.node_type == NodeType.TOOL_CALL:
            await self._execute_tool_call_node(node, context)
        else:
            raise ExecutionError(f"Unknown node type: {node.node_type}")

    async def _execute_agent_node(
        self,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute an agent node, then handle dynamic spawning if configured."""
        agent_name = node.config.get("agent")
        if not agent_name or agent_name not in self.config.agent_types:
            raise ExecutionError(f"Agent type '{agent_name}' not found")

        agent_type = self.config.agent_types[agent_name]
        input_text = node.config.get("input")
        output_as = node.config.get("output_as")
        spawn_strategy = node.config.get("spawn_strategy")
        collect_as = node.config.get("collect_as")
        spawn_context = node.config.get("spawn_context")

        # Interrupt checkpoint: before agent
        interrupt_ui = self._check_interrupt(node.node_id, context)
        formatted_input = context.format_input(input_text)

        # Inject approval feedback from a preceding approve step rejection
        approval_feedback = context.messages.pop("_approval_feedback", None)
        if approval_feedback:
            formatted_input = (formatted_input or "") + f"\n\nUser feedback on previous attempt: {approval_feedback}"

        if interrupt_ui and self._input_handler and "agent" in (self._input_handler.config.interrupt_delivery or []):
            # Inject interrupt as additional user message for agent to see
            formatted_input = (formatted_input or "") + f"\n\n[INTERRUPT] User says: {interrupt_ui.message}"

        t0 = time.monotonic()
        self._events.emit(AgentStarted(
            step_id=node.node_id,
            agent_name=agent_name,
            input_text=formatted_input or "",
        ))

        def _interrupt_check() -> None:
            self._check_interrupt_cancel_only(node.node_id)

        try:
            result = await self.agent_runner.run(
                agent_type, agent_name, formatted_input, context, session,
                step_id=node.node_id,
                interrupt_check=_interrupt_check if self._input_handler else None,
            )

            duration_ms = (time.monotonic() - t0) * 1000
            output = result.get("content", "")

            if output_as:
                context.set_output(output_as, output)

            if agent_type.output_schema:
                self._extract_schema_fields(output, agent_type.output_schema, context)

            self._events.emit(AgentCompleted(
                step_id=node.node_id,
                agent_name=agent_name,
                output=output,
                duration_ms=duration_ms,
                stop_reason=str(result.get("stop_reason") or ""),
                output_chars=len(output),
                truncation_recoveries=int(result.get("truncation_recoveries") or 0),
                input_tokens=int(result.get("input_tokens") or 0),
                output_tokens=int(result.get("output_tokens") or 0),
                cache_read_tokens=int(result.get("cache_read_tokens") or 0),
                cache_creation_tokens=int(result.get("cache_creation_tokens") or 0),
            ))

            # Handle dynamic spawning after the agent completes
            if spawn_strategy == "dynamic" and agent_type.can_spawn:
                await self._execute_spawns(
                    agent_name=agent_name,
                    agent_type=agent_type,
                    content=output,
                    context=context,
                    default_error_strategy=default_error_strategy,
                    session=session,
                    collect_as=collect_as,
                    spawn_context=spawn_context,
                )

        except Exception as e:
            self._events.emit(AgentErrorEvent(
                step_id=node.node_id,
                agent_name=agent_name,
                error=str(e),
            ))
            raise AgentError(f"Agent '{agent_name}' failed: {e}") from e

    async def _execute_spawns(
        self,
        agent_name: str,
        agent_type: AgentTypeDefinition,
        content: str,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session],
        collect_as: Optional[str],
        spawn_context: Optional[str] = None,
    ) -> None:
        """Parse an agent's response for spawn requests and execute them.

        Supports wave-based dependency ordering: tasks in the same wave run
        concurrently, but waves execute sequentially. This ensures dependent
        tasks (e.g., task 2 depends on task 1's output) are not launched until
        their prerequisites complete.
        """
        waves = self._parse_spawn_requests(content)
        if not waves:
            if content.strip():
                logger.warning(
                    "Agent output did not contain parseable spawn requests",
                    agent=agent_name,
                    content_length=len(content),
                )
                self._events.emit(SpawnParseFailed(
                    parent_agent=agent_name,
                    content_preview=content[:200],
                ))
            return

        # Format spawn_context once using the parent context so template variables
        # (e.g. {architecture_plan}) are resolved before injection into each child.
        formatted_spawn_context: Optional[str] = (
            context.format_input(spawn_context) if spawn_context else None
        )

        async def run_spawn(
            spawn_req: Dict[str, Any],
        ) -> Tuple[Optional[str], Optional[ContextManager]]:
            child_type_name = spawn_req.get("agent")
            child_input = spawn_req.get("input", "")

            if formatted_spawn_context:
                child_input = f"{formatted_spawn_context}\n\n---\n\n{child_input}"

            if not child_type_name:
                return None, None

            if not self.spawn_manager.can_spawn(agent_name, agent_type, child_type_name):
                logger.warning(
                    "Spawn not permitted — agent type not in can_spawn or limit reached",
                    parent=agent_name,
                    child_type=child_type_name,
                )
                return None, None

            if child_type_name not in self.config.agent_types:
                logger.warning(
                    "Spawn references unknown agent type",
                    parent=agent_name,
                    child_type=child_type_name,
                )
                return None, None

            child_agent_type = self.config.agent_types[child_type_name]
            child_id = str(uuid.uuid4())

            # Each spawned agent gets an isolated context snapshot so concurrent
            # children don't race on shared state.  Pass session=None for the
            # same reason parallel branches do: concurrent add_turn() calls
            # would interleave conversation history.
            branch_ctx = context.branch()
            self.spawn_manager.register_spawn(agent_name, child_id)
            try:
                self._events.emit(SpawnStarted(
                    parent_agent=agent_name,
                    child_type=child_type_name,
                    child_id=child_id,
                ))
                result = await self.agent_runner.run(
                    child_agent_type, child_type_name, child_input, branch_ctx, None
                )
                self._events.emit(SpawnCompleted(
                    child_id=child_id,
                    child_type=child_type_name,
                ))
                return result.get("content", ""), branch_ctx
            except Exception as e:
                logger.error(
                    "Spawned agent failed",
                    child_type=child_type_name,
                    child_id=child_id,
                    error=str(e),
                )
                return None, None
            finally:
                self.spawn_manager.unregister_spawn(agent_name, child_id)

        max_concurrent = agent_type.max_concurrent_spawns
        all_results: List[Tuple[Optional[str], Optional[ContextManager]]] = []
        wave_count = len(waves)
        # Accumulates text outputs from completed waves so subsequent wave
        # spawns can see what prior waves accomplished (branch names, files
        # changed, blockers, etc.) even though inputs are generated upfront.
        prior_wave_outputs: List[str] = []

        async def run_spawn_with_wave_context(
            spawn_req: Dict[str, Any],
        ) -> Tuple[Optional[str], Optional[ContextManager]]:
            """Wrap run_spawn to prepend prior-wave context to child_input."""
            if prior_wave_outputs:
                original_input = spawn_req.get("input", "")
                combined = "\n\n".join(prior_wave_outputs)
                augmented_req = dict(spawn_req)
                augmented_req["input"] = (
                    f"--- PRIOR WAVE RESULTS ---\n{combined}\n--- END PRIOR WAVE RESULTS ---\n\n"
                    f"{original_input}"
                )
                return await run_spawn(augmented_req)
            return await run_spawn(spawn_req)

        for wave_index, wave in enumerate(waves):
            if not wave:
                continue

            self._events.emit(SpawnWaveStarted(
                parent_agent=agent_name,
                wave_index=wave_index,
                wave_count=wave_count,
                spawn_count=len(wave),
            ))

            # Execute this wave's spawns concurrently, respecting max_concurrent_spawns.
            wave_results: List[Tuple[Optional[str], Optional[ContextManager]]] = []

            if max_concurrent and len(wave) > max_concurrent:
                for i in range(0, len(wave), max_concurrent):
                    batch = wave[i : i + max_concurrent]
                    tasks = [asyncio.ensure_future(run_spawn_with_wave_context(r)) for r in batch]
                    try:
                        batch_results = await asyncio.gather(*tasks)
                    except Exception:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                    wave_results.extend(batch_results)
            else:
                tasks = [asyncio.ensure_future(run_spawn_with_wave_context(r)) for r in wave]
                try:
                    results = await asyncio.gather(*tasks)
                except Exception:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                wave_results.extend(results)

            # Merge writes from this wave back into the parent context BEFORE
            # branching the next wave, so subsequent waves see prior wave outputs.
            for _, branch_ctx in wave_results:
                if branch_ctx is not None:
                    context.merge_from(branch_ctx)

            # Collect this wave's text outputs so the next wave's spawns can
            # see what this wave accomplished in their input.
            wave_texts = [o for o, _ in wave_results if o is not None]
            if wave_texts:
                prior_wave_outputs.append(
                    f"[Wave {wave_index}]\n" + "\n---\n".join(wave_texts)
                )

            all_results.extend(wave_results)

            self._events.emit(SpawnWaveCompleted(
                parent_agent=agent_name,
                wave_index=wave_index,
                wave_count=wave_count,
            ))

        if collect_as:
            context.set_output(collect_as, [o for o, _ in all_results if o is not None])

    def _extract_json_objects(self, text: str) -> List[str]:
        """Extract top-level brace-balanced substrings from text.

        Walks the string tracking brace depth while respecting JSON string
        literals (including escaped quotes), so curly braces inside string
        values don't confuse the depth counter.
        """
        candidates = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "{":
                start = i
                depth = 0
                in_string = False
                while i < n:
                    ch = text[i]
                    if in_string:
                        if ch == "\\":
                            i += 1  # skip the escaped character
                        elif ch == '"':
                            in_string = False
                    else:
                        if ch == '"':
                            in_string = True
                        elif ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                candidates.append(text[start : i + 1])
                                break
                    i += 1
            i += 1
        return candidates

    def _try_raw_decode_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse first top-level JSON object from text, allowing trailing garbage.

        Helps when the model closes the JSON object then adds prose, or when
        markdown fences include extra characters after the object.
        """
        text = text.strip()
        if not text.startswith("{"):
            return None
        try:
            obj, _idx = json.JSONDecoder().raw_decode(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        return None

    def _regex_extract_schema_fields_dict(
        self, content: str, output_schema: Dict[str, str]
    ) -> Optional[Dict[str, Any]]:
        """Extract declared schema keys via regex when JSON is truncated or invalid."""
        extracted: Dict[str, Any] = {}
        for field_name in output_schema:
            pattern = rf'["\']?{re.escape(field_name)}["\']?\s*:\s*(["\']?)(.+?)\1(?:\s*[,\n\r}}]|$)'
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if match:
                value: Any = match.group(2).strip().rstrip('",}')
                if value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False
                elif value.lstrip("-").replace(".", "", 1).isdigit():
                    try:
                        value = int(value) if value.lstrip("-").isdigit() else float(value)
                    except ValueError:
                        pass
                extracted[field_name] = value
        return extracted if extracted else None

    def _parse_spawn_requests(self, content: str) -> List[List[Dict[str, Any]]]:
        """Extract spawn requests from agent response content.

        Returns a list of waves, where each wave is a list of spawn request
        dicts. Supports two JSON output formats:

        - Wave-based (new):  {"waves": [[{...}, {...}], [{...}]]}
          Tasks in the same wave run concurrently; waves execute sequentially.
        - Legacy flat:       {"spawn": [{...}, {...}]}
          Treated as a single wave for full backward compatibility.

        Uses three extraction strategies in order:
        1. Fast path: parse the entire content as JSON directly.
        2. Markdown fence: strip ```json ... ``` code blocks and parse each.
        3. Brace-balanced scan: extract every top-level {...} substring and
           try to parse each — handles LLMs that add preamble/explanation text
           and spawn inputs that contain curly braces (code, templates, etc.).
        """
        if not content:
            return []

        def _try_parse(text: str) -> Optional[List[List[Dict[str, Any]]]]:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    # New wave-based format: {"waves": [[...], [...]]}
                    if isinstance(data.get("waves"), list):
                        waves = data["waves"]
                        if all(isinstance(w, list) for w in waves):
                            return waves
                    # Legacy flat format: {"spawn": [...]}
                    if isinstance(data.get("spawn"), list):
                        return [data["spawn"]]
            except (json.JSONDecodeError, ValueError):
                pass
            return None

        # 1. Fast path: entire content is valid spawn JSON
        result = _try_parse(content)
        if result is not None:
            return result

        # 2. Markdown code fence extraction
        fence_pattern = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
        for fence_match in fence_pattern.finditer(content):
            result = _try_parse(fence_match.group(1).strip())
            if result is not None:
                return result

        # 3. Brace-balanced extraction (handles preamble and nested braces in
        #    spawn input strings that defeat simple regex approaches)
        for candidate in self._extract_json_objects(content):
            result = _try_parse(candidate)
            if result is not None:
                return result

        return []

    async def _execute_parallel_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute all children concurrently using per-branch context snapshots."""
        self._events.emit(ParallelStarted(step_id=node.node_id, branch_count=len(node.children)))
        branches = [(child_id, context.branch()) for child_id in node.children]

        # Parallel branches must not share the Session object — concurrent
        # add_turn() calls would interleave history.  Branches don't need
        # conversation history, so pass session=None for each branch.
        tasks = [
            asyncio.ensure_future(
                self._execute_node(graph, child_id, branch_ctx, default_error_strategy, None)
            )
            for child_id, branch_ctx in branches
        ]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            # Cancel any still-running sibling tasks so they don't continue as
            # orphans after one branch has failed.
            for t in tasks:
                t.cancel()
            # Wait for cancellations to complete before propagating the error.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        for _, branch_ctx in branches:
            context.merge_from(branch_ctx)

        self._events.emit(ParallelCompleted(step_id=node.node_id))

    async def _execute_loop_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute a loop node."""
        max_iterations = node.config.get("max_iterations", 10)
        until_condition = node.config.get("until")
        body_entry = node.children[0] if node.children else None

        if not body_entry:
            return

        for iteration in range(1, max_iterations + 1):
            # Interrupt checkpoint: loop iteration boundary
            self._check_interrupt(node.node_id, context)
            self._events.emit(LoopIteration(step_id=node.node_id, iteration=iteration))
            context.set_shared("_loop_iteration", iteration)
            context.set_shared("_loop_max", max_iterations)
            await self._execute_node(graph, body_entry, context, default_error_strategy, session)

            if until_condition and self._evaluate_condition(until_condition, context):
                break

    def _evaluate_condition(self, condition: str, context: ContextManager) -> bool:
        """Evaluate a condition string against context."""
        if not condition:
            return False

        if condition.startswith("{") and condition.endswith("}"):
            var_name = condition[1:-1]
            value = self._resolve_variable(var_name, context)
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)

        comparison_pattern = r"^(\w+)\s*(>=|<=|==|!=|>|<)\s*(.+)$"
        match = re.match(comparison_pattern, condition.strip())
        if match:
            var_name, operator, right_side = match.groups()
            value = self._resolve_variable(var_name, context)
            if value is None:
                return False

            right_value = self._parse_literal(right_side.strip())
            if right_value is None:
                return False

            try:
                if operator == ">=":
                    return value >= right_value
                elif operator == "<=":
                    return value <= right_value
                elif operator == ">":
                    return value > right_value
                elif operator == "<":
                    return value < right_value
                elif operator == "==":
                    return value == right_value
                elif operator == "!=":
                    return value != right_value
            except TypeError:
                return False

        try:
            value = self._resolve_variable(condition, context)
            return bool(value)
        except Exception as exc:
            logger.warning(
                "condition_eval_failed",
                condition=condition,
                error=str(exc),
            )

        return False

    def _resolve_variable(self, name: str, context: ContextManager) -> Any:
        """Resolve a variable from context, preferring messages over shared context."""
        value = context.get_output(name)
        if value is not None:
            return value
        return context.get_shared(name)

    def _parse_literal(self, value_str: str) -> Any:
        """Parse a literal value from a string (number, boolean, or quoted string)."""
        value_str = value_str.strip()

        if (value_str.startswith('"') and value_str.endswith('"')) or (
            value_str.startswith("'") and value_str.endswith("'")
        ):
            return value_str[1:-1]

        if value_str.lower() == "true":
            return True
        if value_str.lower() == "false":
            return False

        try:
            return int(value_str)
        except ValueError:
            pass

        try:
            return float(value_str)
        except ValueError:
            pass

        return value_str

    def _extract_schema_fields(
        self,
        content: str,
        output_schema: Dict[str, str],
        context: ContextManager,
    ) -> None:
        """Extract output_schema fields from agent response and store as context variables."""
        if not content or not output_schema:
            return

        parsed_data = None

        # 1. Fast path: entire content is valid JSON
        try:
            parsed_data = json.loads(content)
            if not isinstance(parsed_data, dict):
                parsed_data = None
        except json.JSONDecodeError:
            parsed_data = self._try_raw_decode_json_object(content)

        # 2. Markdown fence extraction (```json ... ```)
        if not parsed_data:
            fence_pattern = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
            for fence_match in fence_pattern.finditer(content):
                inner = fence_match.group(1).strip()
                try:
                    obj = json.loads(inner)
                    if isinstance(obj, dict):
                        parsed_data = obj
                        break
                except json.JSONDecodeError:
                    parsed_data = self._try_raw_decode_json_object(inner)
                    if parsed_data:
                        break

        # 3. Brace-balanced extraction (handles nested objects and string escapes)
        if not parsed_data:
            for candidate in self._extract_json_objects(content):
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict) and any(k in obj for k in output_schema):
                        parsed_data = obj
                        break
                except json.JSONDecodeError:
                    obj = self._try_raw_decode_json_object(candidate)
                    if isinstance(obj, dict) and any(k in obj for k in output_schema):
                        parsed_data = obj
                        break

        # 4. Regex fallback (truncated / non-JSON tail; salvage scalar fields)
        if not parsed_data:
            parsed_data = self._regex_extract_schema_fields_dict(content, output_schema)

        if parsed_data and isinstance(parsed_data, dict):
            extracted_any = False
            for field_name, type_hint in output_schema.items():
                if field_name in parsed_data:
                    value = self._coerce_schema_value(parsed_data[field_name], str(type_hint))
                    context.set_output(field_name, value)
                    extracted_any = True
            if not extracted_any:
                logger.warning(
                    "Failed to extract output_schema fields from agent response",
                    content_preview=content[:100] if len(content) > 100 else content,
                )
        else:
            logger.warning(
                "Failed to extract output_schema fields from agent response",
                content_preview=content[:100] if len(content) > 100 else content,
            )

    def _coerce_schema_value(self, value: Any, type_hint: str) -> Any:
        """Coerce a value to the declared output_schema type."""
        type_hint = type_hint.strip().lower()
        try:
            if type_hint == "float":
                return float(value)
            if type_hint in ("int", "integer"):
                return int(value)
            if type_hint in ("bool", "boolean"):
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            if type_hint in ("string", "str"):
                return str(value)
        except (ValueError, TypeError):
            pass
        return value

    async def _execute_route_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute a route node."""
        route_on = node.config.get("condition", "")
        cases = node.config.get("cases", {})
        default = node.config.get("default")

        route_value = self._evaluate_route_condition(route_on, context)
        self._events.emit(RouteTaken(step_id=node.node_id, case=route_value))

        target_entry = cases.get(route_value)
        if target_entry is None and default:
            target_entry = default

        if target_entry:
            await self._execute_node(graph, target_entry, context, default_error_strategy, session)

    def _evaluate_route_condition(self, condition: str, context: ContextManager) -> str:
        """Evaluate a route condition and return the matching case value."""
        try:
            value = context.format_input(condition)
            return str(value) if value else ""
        except Exception:
            return ""

    async def _execute_supervise_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute a supervise node."""
        supervisor_name = node.config.get("supervisor")
        worker_ids: List[str] = node.config.get("workers", [])
        max_retries: int = node.config.get("max_retries", 3)
        on_reject: str = node.config.get("on_reject", "retry")

        if not supervisor_name or supervisor_name not in self.config.agent_types:
            raise ExecutionError(f"Supervisor agent '{supervisor_name}' not found")

        supervisor_type = self.config.agent_types[supervisor_name]

        for worker_id in worker_ids:
            if worker_id not in graph.nodes:
                raise ExecutionError(f"Worker node '{worker_id}' not found in graph")

            worker_node = graph.nodes[worker_id]
            worker_agent_name = worker_node.config.get("agent", "")
            worker_output_as = worker_node.config.get("output_as")
            worker_input_template = worker_node.config.get("input") or ""

            if not worker_agent_name or worker_agent_name not in self.config.agent_types:
                raise ExecutionError(f"Worker agent type '{worker_agent_name}' not found")

            worker_agent_type = self.config.agent_types[worker_agent_name]
            feedback: Optional[str] = None
            accepted = False

            for attempt in range(max_retries + 1):
                effective_input = context.format_input(worker_input_template)
                if feedback and on_reject == "retry_with_feedback":
                    effective_input = f"{effective_input}\n\nSupervisor feedback: {feedback}"

                self._events.emit(SupervisorWorkerStarted(
                    worker_id=worker_id,
                    worker_agent=worker_agent_name,
                    attempt=attempt + 1,
                ))

                worker_result = await self.agent_runner.run(
                    worker_agent_type, worker_agent_name, effective_input, context, session,
                    step_id=worker_id,
                )
                worker_output = worker_result.get("content", "")

                if worker_output_as:
                    context.set_output(worker_output_as, worker_output)

                supervisor_prompt = (
                    f"Worker '{worker_agent_name}' has completed its task.\n\n"
                    f"Task:\n{effective_input}\n\n"
                    f"Worker output:\n{worker_output}\n\n"
                    "Review the output and respond with your verdict."
                )
                if supervisor_type.output_schema and "verdict" in supervisor_type.output_schema:
                    supervisor_prompt += (
                        " Respond with a JSON object containing 'verdict' ('accept' or 'reject')"
                        " and optionally 'feedback' (string with guidance for the worker)."
                    )

                self._events.emit(SupervisorReview(
                    supervisor=supervisor_name,
                    worker_id=worker_id,
                    attempt=attempt + 1,
                ))

                supervisor_result = await self.agent_runner.run(
                    supervisor_type, supervisor_name, supervisor_prompt, context, session,
                    step_id=node.node_id,
                )
                supervisor_content = supervisor_result.get("content", "")

                verdict, feedback = self._parse_supervisor_verdict(supervisor_content, supervisor_type)

                if verdict == "accept":
                    self._events.emit(SupervisorAccepted(
                        supervisor=supervisor_name,
                        worker_id=worker_id,
                        attempt=attempt + 1,
                    ))
                    accepted = True
                    break

                self._events.emit(SupervisorRejected(
                    supervisor=supervisor_name,
                    worker_id=worker_id,
                    attempt=attempt + 1,
                    feedback=feedback,
                ))

                if on_reject == "skip":
                    logger.warning(
                        "Worker rejected by supervisor, skipping",
                        worker_id=worker_id,
                        worker_agent=worker_agent_name,
                        feedback=feedback,
                    )
                    break

                if attempt >= max_retries:
                    logger.warning(
                        "Worker rejected after max retries",
                        worker_id=worker_id,
                        worker_agent=worker_agent_name,
                        max_retries=max_retries,
                    )
                    break

            if not accepted and on_reject not in ("skip", "retry", "retry_with_feedback"):
                raise ExecutionError(
                    f"Worker '{worker_agent_name}' was rejected by supervisor '{supervisor_name}' "
                    f"after {max_retries + 1} attempts"
                )

    def _parse_supervisor_verdict(
        self,
        content: str,
        supervisor_type: AgentTypeDefinition,
    ) -> Tuple[str, Optional[str]]:
        """Parse a supervisor agent's response to extract verdict and feedback."""
        feedback: Optional[str] = None

        if supervisor_type.output_schema and "verdict" in supervisor_type.output_schema:
            parsed = None
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                json_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
                for match in re.findall(json_pattern, content):
                    try:
                        parsed = json.loads(match)
                        break
                    except json.JSONDecodeError:
                        continue

            if isinstance(parsed, dict):
                verdict = str(parsed.get("verdict", "")).lower().strip()
                feedback = parsed.get("feedback")
                if verdict in ("accept", "reject"):
                    return verdict, feedback

        content_lower = content.lower()
        if "reject" in content_lower:
            return "reject", content
        if "accept" in content_lower:
            return "accept", None

        logger.warning(
            "Could not parse supervisor verdict, defaulting to accept",
            content_preview=content[:100] if len(content) > 100 else content,
        )
        return "accept", None

    def _find_previous_node(self, graph: ExecutionGraph, target_node_id: str) -> Optional[str]:
        """Find the node whose .next points to target_node_id."""
        for node_id, node in graph.nodes.items():
            if node.next == target_node_id:
                return node_id
        return None

    async def _execute_approve_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute a human-in-the-loop approval node."""
        content_template = node.config.get("content", "")
        message = node.config.get("message", "Review and approve:")
        output_as = node.config.get("output_as")
        on_reject = node.config.get("on_reject", "retry_previous")
        max_retries = node.config.get("max_retries", 3)

        # Batch mode: no approval fn → auto-approve
        if not self._human_approval_fn:
            content = context.format_input(content_template)
            if output_as:
                context.set_output(output_as, content)
            return

        prev_node_id = self._find_previous_node(graph, node.node_id)

        for attempt in range(max_retries + 1):
            content = context.format_input(content_template)
            self._events.emit(ApprovalRequested(
                step_id=node.node_id,
                content=content,
                message=message,
                attempt=attempt,
            ))

            result = await self._human_approval_fn(message, content or "")

            if result.decision == "approve":
                self._events.emit(ApprovalGranted(step_id=node.node_id, attempt=attempt))
                if output_as:
                    context.set_output(output_as, content)
                return

            if result.decision == "edit":
                self._events.emit(ApprovalEdited(step_id=node.node_id, attempt=attempt))
                edited = result.edited_content or content
                if output_as:
                    context.set_output(output_as, edited)
                return

            # decision == "reject"
            self._events.emit(ApprovalRejected(
                step_id=node.node_id,
                attempt=attempt,
                feedback=result.feedback or "",
            ))

            if on_reject != "retry_previous" or prev_node_id is None:
                raise ExecutionError(
                    f"Approval rejected at step '{node.node_id}' and on_reject={on_reject!r}"
                )

            if attempt >= max_retries:
                raise ExecutionError(
                    f"Approval rejected after {max_retries + 1} attempts at step '{node.node_id}'"
                )

            # Store feedback so _execute_agent_node can inject it
            if result.feedback:
                context.messages["_approval_feedback"] = result.feedback

            # Re-run the predecessor node
            prev_node = graph.nodes[prev_node_id]
            await self._dispatch_node(graph, prev_node, context, default_error_strategy, session)
            # Loop: re-format content and re-prompt

    def _get_memory_db_path(self, tool_name: Optional[str]) -> Optional[Path]:
        """Return the resolved db_path for a named memory tool, or None if unavailable."""
        if not tool_name or tool_name not in self.config.tools:
            return None
        tool_cfg = self.config.tools[tool_name].config or {}
        raw = tool_cfg.get("db_path")
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def _memory_get_exact(self, db_path: Optional[Path], key: str) -> Optional[str]:
        """Retrieve a single memory value by exact key from the SQLite store."""
        if db_path is None or not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("SELECT value FROM memories WHERE key = ?", (key,))
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning("load_context_memory_read_failed", key=key, error=str(e))
            return None

    def _memory_store_value(self, db_path: Optional[Path], key: str, value: str) -> None:
        """Write a key-value pair to the SQLite memory store."""
        if db_path is None:
            return
        from datetime import datetime, UTC
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path))
            try:
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS memories "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO memories (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("save_context_memory_write_failed", key=key, error=str(e))

    async def _execute_load_context_node(
        self,
        node: GraphNode,
        context: ContextManager,
    ) -> None:
        """Execute a load_context node: retrieve memory keys into context variables.

        Zero LLM cost — entirely deterministic reads from the memory store and
        (optionally) the filesystem.
        """
        memory_tool_name = node.config.get("memory_tool")
        load_entries = node.config.get("load", [])
        output_as = node.config.get("output_as")

        db_path = self._get_memory_db_path(memory_tool_name)

        loaded_parts: List[str] = []
        for entry in load_entries:
            key = entry.get("key", "")
            as_name = entry.get("as", entry.get("as_", ""))
            read_file = entry.get("read_file", False)

            value = self._memory_get_exact(db_path, key) or ""

            if read_file and value:
                file_path = Path(value).expanduser()
                if file_path.exists():
                    try:
                        value = file_path.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.warning(
                            "load_context_file_read_failed",
                            path=str(file_path),
                            error=str(e),
                        )

            if as_name:
                context.set_output(as_name, value)

            if value:
                loaded_parts.append(f"[{key}]:\n{value}")

        # Set output_as only when every entry was resolved to a non-empty value
        # so that route-based fallback patterns can detect partial/missing loads.
        if output_as and loaded_parts and len(loaded_parts) == len(load_entries):
            context.set_output(output_as, "\n\n".join(loaded_parts))

    async def _execute_save_context_node(
        self,
        node: GraphNode,
        context: ContextManager,
    ) -> None:
        """Execute a save_context node: write context variables to the memory store.

        Zero LLM cost — entirely deterministic writes. Template placeholders in
        values are resolved against the current context before storing.
        """
        memory_tool_name = node.config.get("memory_tool")
        save_mapping: Dict[str, str] = node.config.get("save", {})

        db_path = self._get_memory_db_path(memory_tool_name)

        for memory_key, value_template in save_mapping.items():
            value = context.format_input(str(value_template)) or ""
            if value:
                self._memory_store_value(db_path, memory_key, value)

    async def _execute_tool_call_node(
        self,
        node: GraphNode,
        context: ContextManager,
    ) -> None:
        """Execute a tool_call node: invoke registered tools directly from the flow.

        Zero LLM cost — deterministic tool invocations with template-interpolated
        parameters.
        """
        calls = node.config.get("calls", [])
        top_output_as = node.config.get("output_as")
        on_error = node.config.get("on_error", "fail")

        last_result = ""
        for call_spec in calls:
            tool_name = call_spec.get("tool", "")
            raw_parameters: Dict[str, Any] = call_spec.get("parameters", {})
            call_output_as = call_spec.get("output_as")

            # Template-interpolate string parameter values
            parameters: Dict[str, Any] = {}
            for k, v in raw_parameters.items():
                if isinstance(v, str):
                    parameters[k] = context.format_input(v) or ""
                else:
                    parameters[k] = v

            self._events.emit(ToolCallStepStarted(
                step_id=node.node_id,
                tool_name=tool_name,
                parameters=parameters,
            ))

            t0 = time.monotonic()
            try:
                result = await self.tool_registry.invoke_async(tool_name, parameters)
                result_str = str(result) if result is not None else ""
            except Exception as e:
                if on_error == "skip":
                    logger.warning(
                        "tool_call_skipped",
                        tool=tool_name,
                        error=str(e),
                        step_id=node.node_id,
                    )
                    result_str = ""
                else:
                    raise ExecutionError(
                        f"tool_call step failed invoking '{tool_name}': {e}"
                    ) from e

            duration_ms = (time.monotonic() - t0) * 1000
            self._events.emit(ToolCallStepCompleted(
                step_id=node.node_id,
                tool_name=tool_name,
                result=result_str[:200],
                duration_ms=duration_ms,
            ))

            if call_output_as:
                context.set_output(call_output_as, result_str)
            last_result = result_str

        # Top-level output_as stores the last call's result
        if top_output_as and calls:
            context.set_output(top_output_as, last_result)

    async def _execute_interval_node(
        self,
        graph: ExecutionGraph,
        node: GraphNode,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Execute an interval node."""
        every = node.config.get("every", "30s")
        until = node.config.get("until")
        agent_node_id = node.children[0] if node.children else None

        if not agent_node_id:
            return

        schedule_id = f"interval_{node.node_id}"

        async def interval_callback() -> None:
            # Give each tick its own context branch so that the interval agent's
            # writes don't race with the main execution path on the shared context.
            tick_ctx = context.branch()
            await self._execute_node(graph, agent_node_id, tick_ctx, default_error_strategy, None)
            context.merge_from(tick_ctx)
            if until and self._evaluate_condition(until, context):
                await self.scheduler.cancel(schedule_id)

        await self.scheduler.schedule(schedule_id, every, interval_callback, until)

    def _get_error_strategy(
        self,
        node: GraphNode,
        default_error_strategy: Optional[ErrorStrategy],
    ) -> Optional[ErrorStrategy]:
        """Get the error strategy for a node, falling back to the orchestration default."""
        if node.config.get("on_error"):
            error_dict = node.config["on_error"]
            if isinstance(error_dict, dict):
                return ErrorStrategy(**error_dict)
        return default_error_strategy

    async def _handle_error(
        self,
        error: Exception,
        node: GraphNode,
        error_strategy: Optional[ErrorStrategy],
        graph: ExecutionGraph,
        context: ContextManager,
        default_error_strategy: Optional[ErrorStrategy],
        session: Optional[Session] = None,
    ) -> None:
        """Handle an execution error."""
        if not error_strategy:
            raise error

        if error_strategy.strategy == "fail":
            raise error
        elif error_strategy.strategy == "retry":
            raise error
        elif error_strategy.strategy == "skip":
            logger.warning("Skipping node due to error", node_id=node.node_id, error=str(error))
            return
        elif error_strategy.strategy == "fallback":
            if error_strategy.fallback_agent:
                fallback_node = GraphNode(
                    node_id=f"{node.node_id}_fallback",
                    node_type=NodeType.AGENT,
                    config={
                        "agent": error_strategy.fallback_agent,
                        "input": node.config.get("input"),
                    },
                )
                await self._execute_agent_node(fallback_node, context, default_error_strategy, session)
                return
            raise error

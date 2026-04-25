"""Batch-run and chat SSE streaming endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ... import Orchestration
from ...conductor import Conductor
from ...events import (
    AgentCompleted,
    AgentErrorEvent,
    AgentStarted,
    AgentToken,
    ApprovalEdited,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    EventBus,
    LoopIteration,
    NodeCompleted,
    NodeError,
    NodeStarted,
    ParallelCompleted,
    ParallelStarted,
    RouteTaken,
    SpawnCompleted,
    SpawnStarted,
    SupervisorAccepted,
    SupervisorRejected,
    SupervisorReview,
    SupervisorWorkerStarted,
    ToolApprovalRequested,
    ToolCalled,
    ToolDenied,
    ToolResult,
)
from ...runtime.executor import HumanApprovalResult
from ...runtime.session import Session
from ...storage import SessionStore
from ..run_manager import RunManager
from . import get_config_dir, resolve_config_path

router = APIRouter(tags=["orchestrations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(type_: str, **kwargs: Any) -> str:
    return f"data: {json.dumps({'type': type_, **kwargs})}\n\n"


def _run_manager(request: Request) -> RunManager:
    return request.app.state.run_manager  # type: ignore[attr-defined]


def _resolve_config(config_dir: Path, config_id: str) -> Path:
    path = resolve_config_path(config_dir, config_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")
    return path


class TurnEventCollector:
    """Collects EventBus events during a turn for later storage."""

    _COLLECTED_TYPES = [
        AgentStarted, AgentCompleted,
        ToolCalled, ToolResult, ToolDenied, ToolApprovalRequested,
        LoopIteration, RouteTaken,
        ParallelStarted, ParallelCompleted,
        SpawnStarted, SpawnCompleted,
        NodeError,
    ]

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._events: List[Dict[str, Any]] = []
        for event_type in self._COLLECTED_TYPES:
            bus.subscribe(event_type, self._collect)

    def _collect(self, event: Any) -> None:
        d: Dict[str, Any] = {"type": type(event).__name__}
        for k, v in vars(event).items():
            if k == "timestamp":
                continue
            d[k] = v
        self._events.append(d)

    def stop(self) -> None:
        for event_type in self._COLLECTED_TYPES:
            self._bus.unsubscribe(event_type, self._collect)

    def get_events(self) -> List[Dict[str, Any]]:
        return list(self._events)


def _start_turn(
    store: SessionStore,
    session: Session,
    user_input: str,
    turn_number: int,
    orchestration: str,
    config_path: str,
    session_persisted: bool,
) -> tuple[int, bool]:
    """Save session if needed and insert a pending turn record.

    Returns ``(turn_id, session_persisted)`` — turn_id is -1 on error.
    """
    try:
        if not session_persisted:
            store.save_session(session, orchestration=orchestration, config_path=config_path)
            session_persisted = True
        turn_id = store.save_pending_turn(
            session_id=session.id,
            turn_number=turn_number,
            user_input=user_input,
            created_at=datetime.now(UTC).isoformat(),
        )
        return turn_id, session_persisted
    except Exception:
        return -1, session_persisted


def _finish_turn(
    store: SessionStore,
    session: Session,
    turn_id: int,
    response: str,
    events: List[Dict[str, Any]],
    duration_ms: float,
    status: str = "complete",
) -> None:
    """Complete a pending turn with response and event data."""
    if turn_id == -1:
        return
    try:
        store.complete_turn(
            turn_id=turn_id,
            response=response,
            agent_outputs={"events": events},
            duration_ms=duration_ms,
            status=status,
        )
        store.save_context(session.id, session.context)
    except Exception:
        pass  # Persistence errors must not break the SSE stream


def _subscribe_events(bus: EventBus, run_emit) -> None:
    """Wire up EventBus → SSE events for a run."""

    def put(type_: str, **kw: Any) -> None:
        run_emit(_make_event(type_, **kw))

    bus.subscribe(AgentStarted, lambda e: put("agent_start", agent=e.agent_name, step_id=e.step_id))
    bus.subscribe(AgentToken, lambda e: put("token", agent=e.agent_name, text=e.token))
    bus.subscribe(
        AgentCompleted,
        lambda e: put(
            "agent_complete", agent=e.agent_name, step_id=e.step_id, duration_ms=e.duration_ms,
            input_tokens=e.input_tokens, output_tokens=e.output_tokens,
            cache_read_tokens=e.cache_read_tokens, cache_creation_tokens=e.cache_creation_tokens,
        ),
    )
    bus.subscribe(AgentErrorEvent, lambda e: put("agent_error", agent=e.agent_name, message=e.error))
    bus.subscribe(
        ToolCalled,
        lambda e: put("tool_call", tool=e.tool_name, agent=e.agent_name, args={k: str(v)[:200] for k, v in list(e.arguments.items())[:5]}),
    )
    bus.subscribe(
        ToolResult,
        lambda e: put("tool_result", tool=e.tool_name, result=e.result[:2000]),
    )
    bus.subscribe(ToolDenied, lambda e: put("tool_denied", tool=e.tool_name, reason=e.reason))
    bus.subscribe(NodeStarted, lambda e: put("node_start", step_id=e.step_id, node_type=e.node_type))
    bus.subscribe(NodeCompleted, lambda e: put("node_complete", step_id=e.step_id))
    bus.subscribe(NodeError, lambda e: put("node_error", step_id=e.step_id, message=e.error))
    bus.subscribe(
        ParallelStarted,
        lambda e: put("parallel_start", step_id=e.step_id, branches=e.branch_count),
    )
    bus.subscribe(ParallelCompleted, lambda e: put("parallel_complete", step_id=e.step_id))
    bus.subscribe(LoopIteration, lambda e: put("loop_iter", step_id=e.step_id, iteration=e.iteration))
    bus.subscribe(RouteTaken, lambda e: put("route_taken", step_id=e.step_id, case=e.case))
    bus.subscribe(
        SupervisorWorkerStarted,
        lambda e: put("supervisor_worker_start", worker_id=e.worker_id, attempt=e.attempt),
    )
    bus.subscribe(
        SupervisorReview,
        lambda e: put("supervisor_review", worker_id=e.worker_id, attempt=e.attempt),
    )
    bus.subscribe(
        SupervisorAccepted,
        lambda e: put("supervisor_accepted", worker_id=e.worker_id, attempt=e.attempt),
    )
    bus.subscribe(
        SupervisorRejected,
        lambda e: put(
            "supervisor_rejected",
            worker_id=e.worker_id,
            attempt=e.attempt,
            feedback=e.feedback or "",
        ),
    )
    bus.subscribe(
        ApprovalRequested,
        lambda e: put("approval_node_requested", step_id=e.step_id, message=e.message, content=e.content),
    )
    bus.subscribe(ApprovalGranted, lambda e: put("approval_node_granted", step_id=e.step_id))
    bus.subscribe(
        ApprovalRejected,
        lambda e: put("approval_node_rejected", step_id=e.step_id, feedback=e.feedback),
    )
    bus.subscribe(ApprovalEdited, lambda e: put("approval_node_edited", step_id=e.step_id))


def _make_approval_fn(run):
    """Build the tool-approval callback that blocks on a web-submitted response."""

    async def approval_fn(agent_name: str, tool_name: str, arguments: Dict[str, Any]) -> bool:
        cached = run.session_policies.get(tool_name)
        if cached == "always_allow":
            return True
        if cached == "always_deny":
            return False

        args_preview = {k: str(v)[:200] for k, v in list(arguments.items())[:5]}
        run.emit(
            _make_event(
                "approval_requested",
                run_id=run.run_id,
                agent=agent_name,
                tool=tool_name,
                args=args_preview,
            )
        )

        loop = asyncio.get_running_loop()
        run.pending_future = loop.create_future()
        run.pending_type = "approval"
        try:
            decision: str = await run.pending_future
        finally:
            run.pending_future = None
            run.pending_type = None

        if decision == "always_allow":
            run.session_policies[tool_name] = "always_allow"
            return True
        if decision == "always_deny":
            run.session_policies[tool_name] = "always_deny"
            return False
        return decision == "allow"

    return approval_fn


def _make_human_approval_fn(run):
    """Build the APPROVE-node callback that blocks on a web-submitted response."""

    async def human_approval_fn(message: str, content: str) -> HumanApprovalResult:
        run.emit(
            _make_event(
                "human_approval_requested",
                run_id=run.run_id,
                message=message,
                content=content,
            )
        )

        loop = asyncio.get_running_loop()
        run.pending_future = loop.create_future()
        run.pending_type = "human_approval"
        try:
            payload: dict = await run.pending_future
        finally:
            run.pending_future = None
            run.pending_type = None

        decision = payload.get("decision", "reject")
        return HumanApprovalResult(
            decision=decision,
            feedback=payload.get("feedback") or None,
            edited_content=payload.get("edited_content") or None,
        )

    return human_approval_fn


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------


class RunBody(BaseModel):
    input: Dict[str, Any] = {}


@router.post("/api/run/{config_id}/{orchestration}")
async def run_orchestration(
    config_id: str,
    orchestration: str,
    body: RunBody,
    request: Request,
) -> StreamingResponse:
    """Start a batch orchestration and stream events back as SSE."""
    config_dir = get_config_dir(request)
    mgr = _run_manager(request)
    config_path = _resolve_config(config_dir, config_id)

    try:
        orch = Orchestration.from_file(str(config_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if orchestration not in orch.config.orchestrations:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orchestration}' not found")

    run = mgr.create()
    bus = EventBus()
    _subscribe_events(bus, run.emit)

    approval_fn = _make_approval_fn(run)
    human_approval_fn = _make_human_approval_fn(run)

    # Emit run_start so the client knows the run_id immediately.
    run.emit(_make_event("run_start", run_id=run.run_id))

    async def _task() -> None:
        task = asyncio.current_task()
        mgr.register_task(task, run.run_id)
        try:
            result = await orch.run(
                orchestration,
                input_data=body.input or None,
                event_bus=bus,
                stream_responses=True,
                approval_fn=approval_fn,
                human_approval_fn=human_approval_fn,
            )
            run.emit(_make_event("done", result=_serializable(result)))
        except Exception as exc:
            run.emit(_make_event("error", message=str(exc)))
        finally:
            run.finish()

    asyncio.create_task(_task())

    return StreamingResponse(
        mgr.sse_generator(run.run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatBody(BaseModel):
    session_id: Optional[str] = None
    message: str


@router.post("/api/chat/{config_id}/{orchestration}")
async def chat(
    config_id: str,
    orchestration: str,
    body: ChatBody,
    request: Request,
) -> StreamingResponse:
    """Send a chat message and stream the response events as SSE."""
    config_dir = get_config_dir(request)
    mgr = _run_manager(request)
    config_path = _resolve_config(config_dir, config_id)

    try:
        orch = Orchestration.from_file(str(config_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if orchestration not in orch.config.orchestrations:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orchestration}' not found")

    # Resolve or create session.
    store = SessionStore()
    abs_config_path = str(config_path.resolve())
    session_persisted = False

    if body.session_id:
        session = store.load_session(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session '{body.session_id}' not found")
        session_persisted = True
    else:
        session = Session()

    run = mgr.create()
    bus = EventBus()
    _subscribe_events(bus, run.emit)

    approval_fn = _make_approval_fn(run)
    human_approval_fn = _make_human_approval_fn(run)

    # Emit run_start so the client knows both run_id and session_id immediately.
    run.emit(_make_event("run_start", run_id=run.run_id, session_id=session.id))

    # Capture turn_number before orch.chat() adds to message_history
    turn_number = len(session.message_history) // 2 + 1

    async def _task() -> None:
        task = asyncio.current_task()
        mgr.register_task(task, run.run_id)

        # Pre-save pending turn for crash-safe resume
        turn_id, _persisted = _start_turn(
            store, session, body.message, turn_number,
            orchestration, abs_config_path, session_persisted,
        )
        collector = TurnEventCollector(bus)
        t0 = asyncio.get_event_loop().time()

        try:
            response = await orch.chat(
                orchestration,
                body.message,
                session=session,
                event_bus=bus,
                stream_responses=True,
                approval_fn=approval_fn,
                human_approval_fn=human_approval_fn,
            )
            collector.stop()
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            _finish_turn(store, session, turn_id, response, collector.get_events(), elapsed_ms)
            run.emit(_make_event("done", response=response, session_id=session.id))
        except Exception as exc:
            collector.stop()
            _finish_turn(store, session, turn_id, "", collector.get_events(), 0.0, status="error")
            run.emit(_make_event("error", message=str(exc)))
        finally:
            run.finish()

    asyncio.create_task(_task())

    return StreamingResponse(
        mgr.sse_generator(run.run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Conductor chat
# ---------------------------------------------------------------------------


class ConductorChatBody(BaseModel):
    session_id: Optional[str] = None
    message: str


@router.post("/api/conductor-chat/{config_id}")
async def conductor_chat(
    config_id: str,
    body: ConductorChatBody,
    request: Request,
) -> StreamingResponse:
    """Send a chat message to a conductor and stream the response events as SSE."""
    config_dir = get_config_dir(request)
    mgr = _run_manager(request)
    config_path = _resolve_config(config_dir, config_id)

    try:
        conductor = Conductor(str(config_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    store = SessionStore()
    abs_config_path = str(config_path.resolve())
    session_persisted = False

    if body.session_id:
        session = store.load_session(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session '{body.session_id}' not found")
        session_persisted = True
    else:
        session = Session()

    run = mgr.create()
    bus = EventBus()
    _subscribe_events(bus, run.emit)

    approval_fn = _make_approval_fn(run)

    run.emit(_make_event("run_start", run_id=run.run_id, session_id=session.id))

    conductor_turn_number = len(session.message_history) // 2 + 1

    async def _task() -> None:
        task = asyncio.current_task()
        mgr.register_task(task, run.run_id)
        conductor._events = bus
        conductor.agent_runner._events = bus
        conductor.agent_runner.stream_responses = True
        conductor.agent_runner._approval_fn = approval_fn
        # Emit per-round AgentStarted/AgentCompleted so each director message
        # gets its own sequential chat bubble.
        conductor.agent_runner.emit_agent_events = True

        turn_id, _persisted = _start_turn(
            store, session, body.message, conductor_turn_number,
            "conductor", abs_config_path, session_persisted,
        )
        collector = TurnEventCollector(bus)
        t0 = asyncio.get_event_loop().time()

        try:
            response = await conductor.chat(
                body.message,
                session=session,
                event_bus=bus,
                stream_responses=True,
                approval_fn=approval_fn,
            )
            collector.stop()
            elapsed_ms = (asyncio.get_event_loop().time() - t0) * 1000
            _finish_turn(store, session, turn_id, response, collector.get_events(), elapsed_ms)
            run.emit(_make_event("done", response=response, session_id=session.id))
        except Exception as exc:
            collector.stop()
            _finish_turn(store, session, turn_id, "", collector.get_events(), 0.0, status="error")
            run.emit(_make_event("error", message=str(exc)))
        finally:
            run.finish()

    asyncio.create_task(_task())

    return StreamingResponse(
        mgr.sse_generator(run.run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _serializable(obj: Any) -> Any:
    """Convert an object to a JSON-safe representation."""
    if isinstance(obj, dict):
        return {k: _serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serializable(v) for v in obj]
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)

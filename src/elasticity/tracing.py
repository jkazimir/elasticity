"""Structured logging and run traces."""

import json
import os
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, Optional, Union

import structlog

from .config.global_loader import get_data_dir, load_global_config
from .events import (
    Event,
    EventBus,
    NodeStarted,
    NodeCompleted,
    NodeError,
    NodeRetrying,
    AgentStarted,
    AgentCompleted,
    ToolCalled,
    ToolResult,
    LoopIteration,
    SpawnStarted,
    SpawnCompleted,
    SupervisorWorkerStarted,
    SupervisorReview,
    SupervisorAccepted,
    SupervisorRejected,
    OrchestrationStarted,
    OrchestrationCompleted,
)

logger = structlog.get_logger(__name__)


def _get_chat_log_path() -> Optional[Path]:
    """Resolve chat log file path.

    Resolution order (highest priority first):
    1. ``ELASTICITY_CHAT_LOG_FILE`` environment variable (falsy value disables)
    2. ``logging.chat_log`` in the global config file (``false`` disables)
    3. XDG data directory: ``~/.local/share/elasticity/chat.log``
    """
    env_val = os.environ.get("ELASTICITY_CHAT_LOG_FILE")
    if env_val is not None:
        env_val = env_val.strip()
        if env_val.lower() in ("", "0", "false", "no", "off"):
            return None
        return Path(env_val).expanduser()

    global_cfg = load_global_config()
    chat_log: Union[str, bool, None] = global_cfg.logging.chat_log
    if chat_log is False:
        return None
    if isinstance(chat_log, str):
        return Path(chat_log).expanduser()

    return get_data_dir() / "chat.log"


def write_chat_turn_log(
    *,
    session_id: str,
    conversation_turns: int,
    orchestration: str,
    message: str,
    response_length: int,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one JSON line to the chat log file. Skips silently if path disabled or unwritable."""
    path = _get_chat_log_path()
    if path is None:
        return
    try:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "conversation_turns": conversation_turns,
            "orchestration": orchestration,
            "message": message[:500] + "..." if len(message) > 500 else message,
            "response_length": response_length,
        }
        if result is not None:
            payload["result"] = {k: v for k, v in result.items() if k != "initial_input"}
        line = json.dumps(payload, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


class RunTrace:
    """Tracks execution trace for an orchestration run.

    Can be used standalone (for backward compat) or as an event subscriber
    by calling ``subscribe_to(event_bus)``.
    """

    def __init__(self, run_id: str, orchestration_name: str, log_to_console: bool = True):
        self.run_id = run_id
        self.orchestration_name = orchestration_name
        self.log_to_console = log_to_console
        self.started_at = datetime.now(UTC)
        self.events: list[Dict[str, Any]] = []
        self.completed_at: Optional[datetime] = None

    def subscribe_to(self, bus: EventBus) -> None:
        """Subscribe to an EventBus and record all events as trace entries."""
        bus.subscribe(Event, self._on_event)

    def _on_event(self, event: Event) -> None:
        """Convert a typed event into the legacy trace event dict format."""
        event_type = type(event).__name__
        # Build a flat dict from the event fields (excluding timestamp)
        fields = {k: v for k, v in event.__dict__.items() if k != "timestamp"}
        self._record(event_type, **fields)

    def _record(self, event_type: str, step_id: Optional[str] = None, **kwargs: Any) -> None:
        """Internal: append an event dict and optionally log it."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            "step_id": step_id,
            **kwargs,
        }
        self.events.append(event)
        if self.log_to_console:
            logger.info(event_type, **{k: v for k, v in event.items() if k != "type"})

    def add_event(
        self,
        event_type: str,
        step_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Add an event to the trace directly (legacy interface, still supported)."""
        self._record(event_type, step_id=step_id, agent_name=agent_name, **kwargs)

    def complete(self) -> None:
        """Mark the trace as complete."""
        self.completed_at = datetime.now(UTC)

    def to_dict(self) -> Dict[str, Any]:
        """Export trace as dictionary."""
        return {
            "run_id": self.run_id,
            "orchestration_name": self.orchestration_name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "events": self.events,
        }


def format_run_log(trace: "RunTrace", team_name: str, input_args: Dict[str, Any]) -> str:
    """Convert a RunTrace into human-readable Markdown for conductor inspection."""
    lines = [
        f"# Team Run Log: {team_name}",
        "",
        f"**Orchestration:** {trace.orchestration_name}",
        f"**Run ID:** {trace.run_id}",
        f"**Started:** {trace.started_at.isoformat()}",
    ]
    if trace.completed_at:
        duration = (trace.completed_at - trace.started_at).total_seconds()
        lines.append(f"**Duration:** {duration:.1f}s")

    if input_args:
        lines += ["", "## Input", ""]
        for k, v in input_args.items():
            v_str = str(v)
            if len(v_str) > 300:
                v_str = v_str[:300] + "...[truncated]"
            lines.append(f"- **{k}:** {v_str}")

    lines += ["", "## Execution Trace", ""]

    start_ts: Optional[datetime] = None
    if trace.events:
        try:
            start_ts = datetime.fromisoformat(trace.events[0]["timestamp"])
        except (KeyError, ValueError):
            pass

    def _rel(ts_str: str) -> str:
        if start_ts is None:
            return ts_str
        try:
            t = datetime.fromisoformat(ts_str)
            delta = (t - start_ts).total_seconds()
            return f"+{delta:.1f}s"
        except (ValueError, TypeError):
            return ts_str

    for ev in trace.events:
        etype = ev.get("type", "")
        ts = _rel(ev.get("timestamp", ""))

        if etype == "AgentToken":
            continue
        elif etype == "AgentStarted":
            agent = ev.get("agent_name", "")
            inp = ev.get("input_text", "")
            if len(inp) > 500:
                inp = inp[:500] + "...[truncated]"
            lines.append(f"### [{ts}] Agent: `{agent}`")
            if inp:
                lines.append(f"> {inp}")
            lines.append("")
        elif etype == "AgentCompleted":
            agent = ev.get("agent_name", "")
            out = ev.get("output", "")
            if len(out) > 1000:
                out = out[:1000] + "...[truncated]"
            dur = ev.get("duration_ms", 0)
            sr = ev.get("stop_reason", "")
            och = ev.get("output_chars", len(out) if out else 0)
            tr = ev.get("truncation_recoveries", 0)
            lines.append(
                f"**[{ts}] Completed:** `{agent}` ({dur:.0f}ms) "
                f"stop_reason={sr!r} chars={och} truncation_recoveries={tr}"
            )
            if out:
                lines.append(f"Output: {out}")
            lines.append("")
        elif etype == "AgentErrorEvent":
            agent = ev.get("agent_name", "")
            error = ev.get("error", "")
            lines.append(f"**[{ts}] Agent Error:** `{agent}` — {error}")
            lines.append("")
        elif etype == "ToolCalled":
            tool = ev.get("tool_name", "")
            args = ev.get("arguments", {})
            args_str = json.dumps(args, default=str)
            if len(args_str) > 300:
                args_str = args_str[:300] + "...[truncated]"
            lines.append(f"  - [{ts}] **Tool:** `{tool}` — {args_str}")
        elif etype == "ToolResult":
            tool = ev.get("tool_name", "")
            result = ev.get("result", "")
            if len(result) > 500:
                result = result[:500] + "...[truncated]"
            dur = ev.get("duration_ms", 0)
            lines.append(f"  - [{ts}] **Result:** `{tool}` ({dur:.0f}ms) — {result}")
        elif etype == "ToolDenied":
            tool = ev.get("tool_name", "")
            reason = ev.get("reason", "")
            lines.append(f"  - [{ts}] **Tool Denied:** `{tool}` — {reason}")
        elif etype == "NodeStarted":
            step_id = ev.get("step_id", "")
            node_type = ev.get("node_type", "")
            lines.append("---")
            lines.append(f"**[{ts}] Step:** `{step_id}` ({node_type})")
        elif etype == "NodeCompleted":
            step_id = ev.get("step_id", "")
            lines.append(f"**[{ts}] Step Done:** `{step_id}`")
            lines.append("")
        elif etype == "NodeError":
            step_id = ev.get("step_id", "")
            error = ev.get("error", "")
            lines.append(f"**[{ts}] Step Error:** `{step_id}` — {error}")
            lines.append("")
        elif etype == "LoopIteration":
            step_id = ev.get("step_id", "")
            iteration = ev.get("iteration", 0)
            lines.append(f"  **[{ts}] Loop Iteration {iteration}** (`{step_id}`)")
        elif etype == "RouteTaken":
            step_id = ev.get("step_id", "")
            case = ev.get("case", "")
            lines.append(f"  **[{ts}] Route:** `{step_id}` → `{case}`")
        elif etype == "ParallelStarted":
            step_id = ev.get("step_id", "")
            count = ev.get("branch_count", 0)
            lines.append(f"**[{ts}] Parallel:** `{step_id}` — {count} branches")
        elif etype == "ParallelCompleted":
            step_id = ev.get("step_id", "")
            lines.append(f"**[{ts}] Parallel Done:** `{step_id}`")
        elif etype == "SupervisorWorkerStarted":
            worker_agent = ev.get("worker_agent", "")
            worker_id = ev.get("worker_id", "")
            attempt = ev.get("attempt", 0)
            lines.append(
                f"  **[{ts}] Supervisor Worker:** `{worker_agent}` "
                f"(id={worker_id}, attempt={attempt})"
            )
        elif etype == "SupervisorReview":
            supervisor = ev.get("supervisor", "")
            worker_id = ev.get("worker_id", "")
            attempt = ev.get("attempt", 0)
            lines.append(
                f"  **[{ts}] Supervisor Review:** `{supervisor}` reviewing "
                f"`{worker_id}` (attempt {attempt})"
            )
        elif etype == "SupervisorAccepted":
            supervisor = ev.get("supervisor", "")
            worker_id = ev.get("worker_id", "")
            attempt = ev.get("attempt", 0)
            lines.append(
                f"  **[{ts}] Supervisor Accepted:** `{worker_id}` by "
                f"`{supervisor}` (attempt {attempt})"
            )
        elif etype == "SupervisorRejected":
            supervisor = ev.get("supervisor", "")
            worker_id = ev.get("worker_id", "")
            attempt = ev.get("attempt", 0)
            feedback = ev.get("feedback") or ""
            lines.append(
                f"  **[{ts}] Supervisor Rejected:** `{worker_id}` by "
                f"`{supervisor}` (attempt {attempt}) — {feedback}"
            )

    return "\n".join(lines)


def write_team_run_log(
    trace: "RunTrace",
    team_name: str,
    input_args: Dict[str, Any],
    conductor_name: str,
) -> str:
    """Write a formatted run log to <data_dir>/run_logs/ and return the absolute path.

    Returns an empty string if writing fails.
    """
    log_dir = Path(get_data_dir()) / "run_logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    uid = uuid.uuid4().hex[:8]
    safe_conductor = conductor_name.replace("/", "_").replace(" ", "_")
    safe_team = team_name.replace("/", "_").replace(" ", "_")
    filename = f"{safe_conductor}_{safe_team}_{timestamp}_{uid}.md"
    path = log_dir / filename

    content = format_run_log(trace, team_name, input_args)
    try:
        path.write_text(content, encoding="utf-8")
        return str(path.resolve())
    except OSError:
        return ""

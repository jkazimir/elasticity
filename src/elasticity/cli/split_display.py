"""Split-screen CLI display for concurrent input handling.

Provides an output panel (streaming), status bar (queue depth, status),
and integrates with prompt_toolkit for always-available input.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from rich.layout import Layout
from rich.live import Live
from rich.text import Text

from ..events import (
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
    NodeError,
    OrchestrationCompleted,
    ParallelCompleted,
    ParallelStarted,
    RouteTaken,
    SpawnCompleted,
    SpawnStarted,
    SupervisorAccepted,
    SupervisorRejected,
    SupervisorReview,
    SupervisorWorkerStarted,
    ToolCalled,
    ToolDenied,
)

from ..runtime.input_handler import InputHandler
from ._console import console


class OutputRenderer:
    """Renders orchestration output from EventBus events. Used by SplitDisplay."""

    def __init__(self, bus: EventBus, on_refresh: Optional[Callable[[], None]] = None):
        self._bus = bus
        self._on_refresh = on_refresh
        self._agent_states: Dict[str, str] = {}
        self._current_text: str = ""
        self._turn_start: float = time.monotonic()
        self._agent_call_count: int = 0
        self._flow_log: List[str] = []
        self._lock = threading.Lock()
        self._done = False

        bus.subscribe(AgentStarted, self._on_agent_started)
        bus.subscribe(AgentCompleted, self._on_agent_completed)
        bus.subscribe(AgentErrorEvent, self._on_agent_error)
        bus.subscribe(AgentToken, self._on_agent_token)
        bus.subscribe(ToolCalled, self._on_tool_called)
        bus.subscribe(ToolDenied, self._on_tool_denied)
        bus.subscribe(LoopIteration, self._on_loop_iteration)
        bus.subscribe(RouteTaken, self._on_route_taken)
        bus.subscribe(ParallelStarted, self._on_parallel_started)
        bus.subscribe(ParallelCompleted, self._on_parallel_completed)
        bus.subscribe(SpawnStarted, self._on_spawn_started)
        bus.subscribe(SpawnCompleted, self._on_spawn_completed)
        bus.subscribe(SupervisorWorkerStarted, self._on_supervisor_worker_started)
        bus.subscribe(SupervisorReview, self._on_supervisor_review)
        bus.subscribe(SupervisorAccepted, self._on_supervisor_accepted)
        bus.subscribe(SupervisorRejected, self._on_supervisor_rejected)
        bus.subscribe(NodeError, self._on_node_error)
        bus.subscribe(OrchestrationCompleted, self._on_orchestration_completed)
        bus.subscribe(ApprovalRequested, self._on_approval_requested)
        bus.subscribe(ApprovalGranted, self._on_approval_granted)
        bus.subscribe(ApprovalRejected, self._on_approval_rejected)
        bus.subscribe(ApprovalEdited, self._on_approval_edited)

    def _render(self) -> Text:
        lines = Text()
        for note in self._flow_log[-3:]:
            lines.append(f"  {note}\n", style="dim italic")
        for agent_name, status in self._agent_states.items():
            lines.append(f"  {agent_name}", style="cyan")
            lines.append(f"  {status}\n", style="dim")
        if self._current_text:
            lines.append("\n")
            lines.append(self._current_text, style="default")
            lines.append(" ▌", style="dim")
        return lines

    def _trigger_refresh(self) -> None:
        if self._on_refresh:
            self._on_refresh()

    def reset_turn(self) -> None:
        """Reset state for a new turn."""
        with self._lock:
            self._current_text = ""
            self._turn_start = time.monotonic()
            self._agent_call_count = 0
            self._done = False
            # Bound the flow log to prevent unbounded memory growth in long sessions.
            if len(self._flow_log) > 100:
                self._flow_log = self._flow_log[-50:]

    def _on_agent_started(self, event: AgentStarted) -> None:
        with self._lock:
            self._agent_states[event.agent_name] = "⠋ running..."
            self._agent_call_count += 1
        self._trigger_refresh()

    def _on_agent_completed(self, event: AgentCompleted) -> None:
        with self._lock:
            summary = event.output[:60].replace("\n", " ") if event.output else ""
            if summary and len(event.output) > 60:
                summary += "…"
            status = f"✓ {event.duration_ms:.0f}ms"
            if summary:
                status += f"  {summary}"
            self._agent_states[event.agent_name] = status
        self._trigger_refresh()

    def _on_agent_error(self, event: AgentErrorEvent) -> None:
        with self._lock:
            self._agent_states[event.agent_name] = f"✗ {event.error[:60]}"
        self._trigger_refresh()

    def _on_agent_token(self, event: AgentToken) -> None:
        with self._lock:
            self._current_text += event.token
        self._trigger_refresh()

    def _on_tool_called(self, event: ToolCalled) -> None:
        with self._lock:
            args_preview = ", ".join(
                f"{k}={repr(v)[:20]}" for k, v in list(event.arguments.items())[:2]
            )
            self._agent_states[event.agent_name] = f"⚙ {event.tool_name}({args_preview})"
        self._trigger_refresh()

    def _on_tool_denied(self, event: ToolDenied) -> None:
        with self._lock:
            self._agent_states[event.agent_name] = f"✗ {event.tool_name} denied"
        self._trigger_refresh()

    def _on_loop_iteration(self, event: LoopIteration) -> None:
        with self._lock:
            self._flow_log.append(f"↺ loop iteration {event.iteration}")
        self._trigger_refresh()

    def _on_route_taken(self, event: RouteTaken) -> None:
        with self._lock:
            if event.case:
                self._flow_log.append(f"⇒ route → {event.case}")
        self._trigger_refresh()

    def _on_parallel_started(self, event: ParallelStarted) -> None:
        with self._lock:
            self._flow_log.append(f"⇉ parallel ({event.branch_count} branches)")
        self._trigger_refresh()

    def _on_parallel_completed(self, event: ParallelCompleted) -> None:
        self._trigger_refresh()

    def _on_spawn_started(self, event: SpawnStarted) -> None:
        with self._lock:
            self._flow_log.append(f"↳ spawn {event.child_type} [{event.child_id[:6]}]")
        self._trigger_refresh()

    def _on_spawn_completed(self, event: SpawnCompleted) -> None:
        self._trigger_refresh()

    def _on_supervisor_worker_started(self, event: SupervisorWorkerStarted) -> None:
        with self._lock:
            self._agent_states[event.worker_agent] = f"⠋ supervised attempt {event.attempt}…"
        self._trigger_refresh()

    def _on_supervisor_review(self, event: SupervisorReview) -> None:
        with self._lock:
            self._agent_states[event.supervisor] = f"⠋ reviewing {event.worker_id[:8]}…"
        self._trigger_refresh()

    def _on_supervisor_accepted(self, event: SupervisorAccepted) -> None:
        with self._lock:
            self._agent_states[event.supervisor] = f"✓ accepted (attempt {event.attempt})"
        self._trigger_refresh()

    def _on_supervisor_rejected(self, event: SupervisorRejected) -> None:
        with self._lock:
            fb = event.feedback[:40] if event.feedback else ""
            self._agent_states[event.supervisor] = f"✗ rejected  {fb}"
        self._trigger_refresh()

    def _on_node_error(self, event: NodeError) -> None:
        with self._lock:
            self._flow_log.append(f"✗ error [{event.step_id[:8]}]: {event.error[:50]}")
        self._trigger_refresh()

    def _on_approval_requested(self, event: ApprovalRequested) -> None:
        with self._lock:
            self._flow_log.append(f"⏸ waiting for approval (attempt {event.attempt + 1})")
        self._trigger_refresh()

    def _on_approval_granted(self, event: ApprovalGranted) -> None:
        with self._lock:
            self._flow_log.append(f"✓ approved")
        self._trigger_refresh()

    def _on_approval_rejected(self, event: ApprovalRejected) -> None:
        with self._lock:
            fb = f"  {event.feedback[:40]}" if event.feedback else ""
            self._flow_log.append(f"✗ rejected — retrying{fb}")
        self._trigger_refresh()

    def _on_approval_edited(self, event: ApprovalEdited) -> None:
        with self._lock:
            self._flow_log.append(f"✏ content edited by user")
        self._trigger_refresh()

    def _on_orchestration_completed(self, event: OrchestrationCompleted) -> None:
        with self._lock:
            self._done = True

    @property
    def current_text(self) -> str:
        return self._current_text

    @property
    def agent_call_count(self) -> int:
        return self._agent_call_count

    @property
    def turn_start(self) -> float:
        return self._turn_start


class SplitDisplay:
    """Split-screen display with output panel, status bar, and input area.

    The output panel shows streaming orchestration output. The status bar shows
    queue depth and execution status. Input is collected via prompt_async().
    """

    def __init__(
        self,
        event_bus: EventBus,
        input_handler: Optional[InputHandler] = None,
    ):
        self._bus = event_bus
        self._input_handler = input_handler
        self._output = OutputRenderer(event_bus, on_refresh=self.refresh)
        self._live: Optional[Live] = None
        self._running = False
        self._status = "idle"

    def _make_layout(self) -> Layout:
        """Build the Rich layout: output + status bar."""
        output_text = self._output._render()
        queue_depth = self._input_handler.queue_depth() if self._input_handler else 0
        status_line = Text()
        status_line.append(f"[queue: {queue_depth}] ", style="dim")
        status_line.append(f"[status: {self._status}]", style="dim")
        status_line.append("  /i <msg> to interrupt", style="dim italic")
        
        # Calculate available height: leave 2 lines at bottom for input prompt
        # Layout total = output_height + 2 (status) = term_height - 2
        term_height = console.size.height
        output_height = max(5, term_height - 4)
        
        layout = Layout()
        layout.split(
            Layout(name="output", size=output_height),  # Fixed size instead of ratio=1
            Layout(status_line, size=2, name="status"),
        )
        layout["output"].update(output_text)
        return layout

    def _render(self) -> Layout:
        return self._make_layout()

    def start(self) -> None:
        """Start the Live display."""
        self._running = True
        live = Live(
            self._render(),
            console=console,
            refresh_per_second=10,
            transient=False,
            vertical_overflow="crop",
            redirect_stdout=False,
            redirect_stderr=False,
        )
        live.__enter__()
        self._live = live  # Only assign after successful __enter__

    def stop(self) -> None:
        """Stop the Live display."""
        self._running = False
        if self._live:
            self._live.__exit__(None, None, None)
            self._live = None

    def pause(self) -> None:
        """Temporarily stop the Live display (e.g. for tool approval prompt)."""
        if self._live:
            self._live.__exit__(None, None, None)
            self._live = None

    def resume(self) -> None:
        """Restart the Live display after a pause."""
        if self._running and not self._live:
            live = Live(
                self._render(),
                console=console,
                refresh_per_second=10,
                transient=False,
                vertical_overflow="crop",
                redirect_stdout=False,
                redirect_stderr=False,
            )
            live.__enter__()
            self._live = live  # Only assign after successful __enter__

    def set_status(self, status: str) -> None:
        """Update the status bar text."""
        self._status = status
        if self._live:
            self._live.update(self._render())

    def refresh(self) -> None:
        """Refresh the display."""
        if self._live and self._running:
            self._live.update(self._render())

    def reset_turn(self) -> None:
        """Reset for a new turn."""
        self._output.reset_turn()
        self.refresh()

    @property
    def output(self) -> OutputRenderer:
        return self._output


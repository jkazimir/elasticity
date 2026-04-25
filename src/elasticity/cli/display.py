"""Rich rendering components for the Elasticity CLI.

This module contains the display components that subscribe to EventBus events
and render them to the terminal using Rich. All rendering is synchronous and
driven by event callbacks.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from rich.columns import Columns
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
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
    ToolResult,
)

from ._console import console, error_console  # noqa: F401 – re-exported for consumers
from .split_display import OutputRenderer


# ---------------------------------------------------------------------------
# Session header
# ---------------------------------------------------------------------------


def print_session_header(
    orchestration: str,
    config_path: str,
    agent_count: int,
    tool_names: List[str],
    mcp_servers: List[str],
    session_id: str,
) -> None:
    """Print a one-time header panel when a chat session starts."""
    parts = []
    parts.append(f"[bold]{orchestration}[/bold]")
    parts.append(f"[dim]{config_path}[/dim]")

    meta = []
    if agent_count:
        meta.append(f"[cyan]{agent_count} agent{'s' if agent_count != 1 else ''}[/cyan]")
    if tool_names:
        meta.append(f"[green]{len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}[/green]")
    if mcp_servers:
        meta.append(f"[yellow]MCP: {', '.join(mcp_servers)}[/yellow]")
    meta.append(f"[dim]session {session_id[:8]}[/dim]")

    if meta:
        parts.append(" · ".join(meta))

    console.print(Panel("\n".join(parts), border_style="dim"))


# ---------------------------------------------------------------------------
# Turn display
# ---------------------------------------------------------------------------


class TurnDisplay:
    """Manages the Rich Live display for a single conversation turn.

    Subscribes to EventBus events to show agent activity, stream tokens,
    and emit tool call notifications. Uses OutputRenderer internally.
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._output = OutputRenderer(bus, on_refresh=self._refresh)
        self._live: Optional[Live] = None
        self._done = False

    def _render(self) -> Text:
        return self._output._render()

    def _refresh(self) -> None:
        self._done = self._output._done
        if self._live and not self._done:
            self._live.update(self._render())

    def start(self) -> None:
        """Start the Live display context."""
        live = Live(
            self._render(),
            console=console,
            refresh_per_second=10,
            transient=True,
        )
        live.__enter__()
        self._live = live  # Only assign after successful __enter__

    def pause(self) -> None:
        """Temporarily stop the Live display (e.g. to show an interactive prompt)."""
        if self._live:
            self._live.__exit__(None, None, None)
            self._live = None

    def resume(self) -> None:
        """Restart the Live display after a pause."""
        if not self._done:
            live = Live(
                self._render(),
                console=console,
                refresh_per_second=10,
                transient=True,
            )
            live.__enter__()
            self._live = live  # Only assign after successful __enter__

    def stop(self) -> None:
        """Stop the Live display context and print final response as Markdown."""
        self._done = True
        if self._live:
            self._live.__exit__(None, None, None)
            self._live = None

    def print_response(self, response: str) -> None:
        """Print the final response as formatted Markdown after Live stops."""
        if response:
            console.print()
            console.print(Markdown(response))

    def print_stats(self, response: str) -> None:
        """Print a brief summary line after the response."""
        elapsed = time.monotonic() - self._output.turn_start
        stats = Text()
        stats.append(f"  {elapsed:.1f}s", style="dim")
        if self._output.agent_call_count:
            stats.append("  ·  ", style="dim")
            stats.append(
                f"{self._output.agent_call_count} agent invocation{'s' if self._output.agent_call_count != 1 else ''}",
                style="dim",
            )
        console.print(stats)
        console.print()


# ---------------------------------------------------------------------------
# Batch run observability
# ---------------------------------------------------------------------------


class BatchObserver:
    """Subscribes to events during a batch run and prints activity to console."""

    def __init__(self, bus: EventBus, verbose: bool = True):
        self._verbose = verbose
        if verbose:
            bus.subscribe(AgentStarted, self._on_agent_started)
            bus.subscribe(AgentCompleted, self._on_agent_completed)
            bus.subscribe(AgentErrorEvent, self._on_agent_error)
            bus.subscribe(ToolCalled, self._on_tool_called)
            bus.subscribe(ToolResult, self._on_tool_result)
            bus.subscribe(ToolDenied, self._on_tool_denied)
            bus.subscribe(LoopIteration, self._on_loop_iteration)
            bus.subscribe(RouteTaken, self._on_route_taken)
            bus.subscribe(ParallelStarted, self._on_parallel_started)
            bus.subscribe(SpawnStarted, self._on_spawn_started)
            bus.subscribe(SupervisorWorkerStarted, self._on_supervisor_worker_started)
            bus.subscribe(SupervisorAccepted, self._on_supervisor_accepted)
            bus.subscribe(SupervisorRejected, self._on_supervisor_rejected)
            bus.subscribe(ApprovalRequested, self._on_approval_requested)
            bus.subscribe(ApprovalGranted, self._on_approval_granted)
            bus.subscribe(ApprovalRejected, self._on_approval_rejected)
            bus.subscribe(ApprovalEdited, self._on_approval_edited)

    def _on_agent_started(self, event: AgentStarted) -> None:
        console.print(f"  [cyan]→[/cyan] [bold]{event.agent_name}[/bold]", highlight=False)

    def _on_agent_completed(self, event: AgentCompleted) -> None:
        console.print(
            f"  [green]✓[/green] [bold]{event.agent_name}[/bold] "
            f"[dim]({event.duration_ms:.0f}ms)[/dim]",
            highlight=False,
        )

    def _on_agent_error(self, event: AgentErrorEvent) -> None:
        console.print(
            f"  [red]✗[/red] [bold]{event.agent_name}[/bold] [red]{event.error[:80]}[/red]",
            highlight=False,
        )

    def _on_tool_called(self, event: ToolCalled) -> None:
        args_preview = ", ".join(
            f"{k}={repr(v)[:20]}" for k, v in list(event.arguments.items())[:2]
        )
        console.print(
            f"    [dim]⚙ {event.tool_name}({args_preview})[/dim]",
            highlight=False,
        )

    def _on_tool_result(self, event: ToolResult) -> None:
        console.print(
            f"    [dim]  ↩ {event.result[:60].replace(chr(10), ' ')} ({event.duration_ms:.0f}ms)[/dim]",
            highlight=False,
        )

    def _on_tool_denied(self, event: ToolDenied) -> None:
        console.print(
            f"    [yellow]✗ {event.tool_name} denied ({event.reason})[/yellow]",
            highlight=False,
        )

    def _on_loop_iteration(self, event: LoopIteration) -> None:
        console.print(
            f"  [dim]↺ loop iteration {event.iteration}[/dim]",
            highlight=False,
        )

    def _on_route_taken(self, event: RouteTaken) -> None:
        if event.case:
            console.print(f"  [dim]⇒ route → {event.case}[/dim]", highlight=False)

    def _on_parallel_started(self, event: ParallelStarted) -> None:
        console.print(
            f"  [dim]⇉ parallel ({event.branch_count} branches)[/dim]",
            highlight=False,
        )

    def _on_spawn_started(self, event: SpawnStarted) -> None:
        console.print(
            f"  [dim]↳ spawn {event.child_type}[/dim]",
            highlight=False,
        )

    def _on_supervisor_worker_started(self, event: SupervisorWorkerStarted) -> None:
        console.print(
            f"  [cyan]→[/cyan] [bold]{event.worker_agent}[/bold] [dim](supervised, attempt {event.attempt})[/dim]",
            highlight=False,
        )

    def _on_supervisor_accepted(self, event: SupervisorAccepted) -> None:
        console.print(
            f"  [green]✓[/green] [dim]supervisor accepted {event.worker_id[:8]} (attempt {event.attempt})[/dim]",
            highlight=False,
        )

    def _on_supervisor_rejected(self, event: SupervisorRejected) -> None:
        fb = f"  {event.feedback[:40]}" if event.feedback else ""
        console.print(
            f"  [yellow]⚠[/yellow] [dim]supervisor rejected (attempt {event.attempt}){fb}[/dim]",
            highlight=False,
        )

    def _on_approval_requested(self, event: ApprovalRequested) -> None:
        console.print(
            f"  [yellow]⏸[/yellow] [dim]waiting for approval (attempt {event.attempt + 1})[/dim]",
            highlight=False,
        )

    def _on_approval_granted(self, event: ApprovalGranted) -> None:
        console.print(
            f"  [green]✓[/green] [dim]approved[/dim]",
            highlight=False,
        )

    def _on_approval_rejected(self, event: ApprovalRejected) -> None:
        fb = f"  {event.feedback[:40]}" if event.feedback else ""
        console.print(
            f"  [yellow]✗[/yellow] [dim]rejected — retrying{fb}[/dim]",
            highlight=False,
        )

    def _on_approval_edited(self, event: ApprovalEdited) -> None:
        console.print(
            f"  [cyan]✏[/cyan] [dim]content edited by user[/dim]",
            highlight=False,
        )


# ---------------------------------------------------------------------------
# Concurrent chat display (no Rich Live — avoids conflict with prompt_toolkit)
# ---------------------------------------------------------------------------


class ConcurrentDisplay:
    """Line-by-line event display for concurrent chat mode.

    Uses ``BatchObserver`` (plain ``console.print`` per event) rather than
    Rich ``Live`` to avoid cursor-management conflicts between Rich's
    background refresh thread and prompt_toolkit's ``prompt_async``.
    Provides the same ``start / stop / pause / resume / output`` interface
    as ``SplitDisplay`` so the orchestration loop doesn't need special-casing.
    """

    def __init__(self, bus: EventBus) -> None:
        self._observer = BatchObserver(bus)
        self._turn_start: float = time.monotonic()
        self._agent_call_count: int = 0
        bus.subscribe(AgentStarted, self._on_agent_started)

    def _on_agent_started(self, event: AgentStarted) -> None:
        self._agent_call_count += 1

    def start(self) -> None:
        self._turn_start = time.monotonic()
        self._agent_call_count = 0

    def stop(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def set_status(self, status: str) -> None:
        pass

    def reset_turn(self) -> None:
        self._turn_start = time.monotonic()
        self._agent_call_count = 0

    @property
    def output(self) -> "ConcurrentDisplay":
        return self

    @property
    def turn_start(self) -> float:
        return self._turn_start

    @property
    def agent_call_count(self) -> int:
        return self._agent_call_count


# ---------------------------------------------------------------------------
# Session history replay
# ---------------------------------------------------------------------------


def replay_stored_turn(
    user_input: str,
    events: List[Dict[str, Any]],
    response: str,
) -> None:
    """Render a completed turn from stored event data.

    Mirrors the BatchObserver output style so replayed history looks the same
    as what was shown during the original run.
    """
    console.print(f"[bold green]You:[/bold green] {user_input}", highlight=False)
    console.print()

    for ev in events:
        t = ev.get("type", "")
        if t == "AgentStarted":
            name = ev.get("agent_name", "")
            console.print(f"  [cyan]→[/cyan] [bold]{name}[/bold]", highlight=False)
        elif t == "AgentCompleted":
            name = ev.get("agent_name", "")
            duration = ev.get("duration_ms", 0)
            console.print(
                f"  [green]✓[/green] [bold]{name}[/bold] [dim]({duration:.0f}ms)[/dim]",
                highlight=False,
            )
        elif t == "ToolCalled":
            tool = ev.get("tool_name", "")
            args = ev.get("arguments", {})
            if isinstance(args, dict):
                args_preview = ", ".join(
                    f"{k}={repr(v)[:20]}" for k, v in list(args.items())[:2]
                )
            else:
                args_preview = str(args)[:40]
            console.print(f"    [dim]⚙ {tool}({args_preview})[/dim]", highlight=False)
        elif t == "ToolResult":
            tool = ev.get("tool_name", "")
            result = str(ev.get("result", "")).replace("\n", " ")[:60]
            duration = ev.get("duration_ms", 0)
            console.print(
                f"    [dim]  ↩ {result} ({duration:.0f}ms)[/dim]",
                highlight=False,
            )
        elif t == "ToolDenied":
            tool = ev.get("tool_name", "")
            reason = ev.get("reason", "")
            console.print(f"    [yellow]✗ {tool} denied ({reason})[/yellow]", highlight=False)
        elif t == "LoopIteration":
            console.print(
                f"  [dim]↺ loop iteration {ev.get('iteration', '')}[/dim]",
                highlight=False,
            )
        elif t == "RouteTaken":
            case = ev.get("case", "")
            if case:
                console.print(f"  [dim]⇒ route → {case}[/dim]", highlight=False)
        elif t == "ParallelStarted":
            console.print(
                f"  [dim]⇉ parallel ({ev.get('branch_count', '')} branches)[/dim]",
                highlight=False,
            )
        elif t == "SpawnStarted":
            console.print(
                f"  [dim]↳ spawn {ev.get('child_type', '')}[/dim]",
                highlight=False,
            )
        elif t == "NodeError":
            err = ev.get("error", "")[:80]
            console.print(f"  [red]✗ error: {err}[/red]", highlight=False)

    if response:
        console.print()
        console.print(Markdown(response))
    console.print()

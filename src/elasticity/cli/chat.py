"""Rich interactive chat session for Elasticity."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal as _pt_run_in_terminal
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .. import Orchestration
from ..config.schema import InputHandlingConfig
from ..errors import OrchestrationInterrupted
from ..events import (
    AgentCompleted,
    AgentStarted,
    EventBus,
    LoopIteration,
    NodeError,
    ParallelCompleted,
    ParallelStarted,
    RouteTaken,
    SpawnCompleted,
    SpawnStarted,
    ToolApprovalRequested,
    ToolCalled,
    ToolDenied,
    ToolResult,
)
from ..runtime.executor import HumanApprovalFn, HumanApprovalResult
from ..runtime.input_handler import InputHandler
from ..runtime.session import Session
from ..storage import SessionStore, TurnRecord
from ..tools.ask_user import set_ask_user_fn
from .display import ConcurrentDisplay, TurnDisplay, console, error_console, print_session_header, replay_stored_turn

class TurnEventCollector:
    """Subscribes to a turn's EventBus and collects events for later storage.

    Skips AgentToken events (too many; the full response text is stored separately).
    Call ``stop()`` when the turn ends to unsubscribe all callbacks.
    """

    _COLLECTED_TYPES = [
        AgentStarted,
        AgentCompleted,
        ToolCalled,
        ToolResult,
        ToolDenied,
        ToolApprovalRequested,
        LoopIteration,
        RouteTaken,
        ParallelStarted,
        ParallelCompleted,
        SpawnStarted,
        SpawnCompleted,
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


_SLASH_COMMANDS = {
    "/quit": "Exit the session",
    "/exit": "Exit the session",
    "/clear": "Clear session history (start fresh)",
    "/history": "Show conversation history",
    "/sessions": "List saved sessions",
    "/help": "Show this help",
}

# Return values from _dispatch_slash
_SLASH_QUIT = "quit"
_SLASH_HANDLED = "handled"
_SLASH_UNKNOWN = "unknown"


def _print_help() -> None:
    for cmd, desc in _SLASH_COMMANDS.items():
        console.print(f"  [bold cyan]{cmd}[/bold cyan]  [dim]{desc}[/dim]")
    console.print()


def _print_history(session: Session) -> None:
    history = session.get_history()
    if not history:
        console.print("[dim](no conversation history)[/dim]")
        return
    for msg in history:
        role = "You" if msg["role"] == "user" else "Assistant"
        style = "bold green" if msg["role"] == "user" else "bold blue"
        content = msg["content"]
        truncated = content[:200] + ("…" if len(content) > 200 else "")
        console.print(f"[{style}]{role}:[/{style}] {truncated}")
    console.print()


# ---------------------------------------------------------------------------
# Shared session state
# ---------------------------------------------------------------------------


@dataclass
class ChatSessionState:
    """Mutable state shared across both tasks in a concurrent chat session,
    and across turns in a turn-based session."""

    session: Session
    session_persisted: bool
    turn_number: int
    agent_running: bool = False
    current_display: Optional[Any] = None  # TurnDisplay | SplitDisplay
    pending_future: Optional[asyncio.Future] = None
    session_policies: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_session(
    store: SessionStore,
    config_path: str,
    orchestration: str,
    resume: bool,
    session_id: Optional[str],
) -> Optional[tuple[Session, bool]]:
    """Locate or create a session.

    Returns ``(session, was_loaded)`` or ``None`` if a fatal error occurred
    (the error has already been printed to *error_console*).
    """
    if session_id:
        session = store.load_session(session_id)
        if session is None:
            error_console.print(f"[red]Session '{session_id}' not found.[/red]")
            return None
        console.print(f"[dim]Resumed session {session_id[:8]}[/dim]")
        return session, True
    if resume:
        session = store.get_latest_session(config_path, orchestration)
        if session is None:
            console.print("[dim]No previous session found. Starting new session.[/dim]")
        return session or Session(), session is not None
    return Session(), False


def _dispatch_slash(
    cmd: str,
    full_input: str,
    state: ChatSessionState,
    store: SessionStore,
    config_path: str,
) -> str:
    """Handle a slash command.

    Returns one of *_SLASH_QUIT*, *_SLASH_HANDLED*, or *_SLASH_UNKNOWN*.
    """
    if cmd in ("/quit", "/exit"):
        console.print("[dim]Goodbye![/dim]")
        return _SLASH_QUIT
    if cmd == "/clear":
        state.session.clear()
        store.save_context(state.session.id, {})
        console.print("[dim]Session cleared.[/dim]")
        state.turn_number = 1
        return _SLASH_HANDLED
    if cmd == "/history":
        _print_history(state.session)
        return _SLASH_HANDLED
    if cmd == "/sessions":
        _list_sessions(store, config_path)
        return _SLASH_HANDLED
    if cmd == "/help":
        _print_help()
        return _SLASH_HANDLED
    console.print(f"[dim]Unknown command: {full_input}. Type /help for help.[/dim]")
    return _SLASH_UNKNOWN


def _start_turn(
    store: SessionStore,
    state: ChatSessionState,
    user_input: str,
    orchestration: str,
    config_path: str,
) -> int:
    """Save the session (on first turn) and insert a pending turn record.

    Returns the DB row id so the turn can be completed via ``_finish_turn()``.
    Returns -1 on error (caller should still run the agent).
    """
    try:
        if not state.session_persisted:
            store.save_session(
                state.session, orchestration=orchestration, config_path=config_path
            )
            state.session_persisted = True
        return store.save_pending_turn(
            session_id=state.session.id,
            turn_number=state.turn_number,
            user_input=user_input,
            created_at=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        error_console.print(f"[red]Save error:[/red] {exc}")
        return -1


def _finish_turn(
    store: SessionStore,
    turn_id: int,
    state: ChatSessionState,
    response: str,
    events: List[Dict[str, Any]],
    duration_ms: float,
    status: str = "complete",
) -> None:
    """Update the pending turn with the completed response and event data."""
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
        store.save_context(state.session.id, state.session.context)
    except Exception as exc:
        error_console.print(f"[red]Save error:[/red] {exc}")


# ---------------------------------------------------------------------------
# Session history display
# ---------------------------------------------------------------------------


def _display_loaded_history(store: SessionStore, session_id: str) -> None:
    """Replay stored turn events for a resumed session."""
    try:
        turns = store.get_turns(session_id)
    except Exception:
        logger.warning("Failed to load session history for %s", session_id, exc_info=True)
        return
    completed = [t for t in turns if t["status"] == "complete"]
    if not completed:
        return
    console.print("[dim]── session history ──────────────────────────────[/dim]")
    for turn in completed:
        events = turn["agent_outputs"].get("events", [])
        replay_stored_turn(
            user_input=turn["user_input"],
            events=events,
            response=turn["response"],
        )
    console.print("[dim]── end of history ───────────────────────────────[/dim]")
    console.print()


def _get_pending_input(store: SessionStore, session_id: str) -> Optional[str]:
    """Return the user_input of the last pending turn, or None if no pending turn."""
    try:
        turns = store.get_turns(session_id)
        if turns and turns[-1]["status"] == "pending":
            return turns[-1]["user_input"]
    except Exception:
        logger.warning("Failed to load pending input for session %s", session_id, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Approval / ask_user callbacks – turn-based path
# (use run_in_executor so blocking input() doesn't stall the event loop)
# ---------------------------------------------------------------------------


def _make_approval_fn(state: ChatSessionState) -> Any:
    """Return an async approval callback for the turn-based path."""

    async def approval_fn(agent_name: str, tool_name: str, arguments: Dict[str, Any]) -> bool:
        cached = state.session_policies.get(tool_name)
        if cached == "always_allow":
            return True
        if cached == "always_deny":
            return False

        display = state.current_display
        loop = asyncio.get_running_loop()

        def _prompt_user() -> str:
            if display is not None:
                display.pause()
            try:
                args_text = ", ".join(
                    f"{k}={repr(v)[:40]}" for k, v in list(arguments.items())[:4]
                )
                console.print()
                console.print(
                    f"[bold yellow]Tool approval required[/bold yellow]  "
                    f"[cyan]{agent_name}[/cyan] wants to call "
                    f"[bold]{tool_name}[/bold]({args_text})"
                )
                console.print(
                    "  [dim][y] allow  [n] deny  [a] always allow this session  "
                    "[d] always deny this session[/dim]"
                )
                while True:
                    raw = input("  > ").strip().lower()
                    if raw in ("y", "yes", "n", "no", "a", "always", "d"):
                        return raw
                    console.print("  [dim]Please enter y, n, a, or d[/dim]")
            finally:
                if display is not None:
                    display.resume()

        answer = await loop.run_in_executor(None, _prompt_user)

        if answer in ("a", "always"):
            state.session_policies[tool_name] = "always_allow"
            return True
        if answer == "d":
            state.session_policies[tool_name] = "always_deny"
            return False
        return answer in ("y", "yes")

    return approval_fn


def _make_human_approval_fn(state: ChatSessionState) -> HumanApprovalFn:
    """Return an async human-approval callback for the turn-based path."""

    async def human_approval_fn(message: str, content: str) -> HumanApprovalResult:
        display = state.current_display
        loop = asyncio.get_running_loop()

        def _prompt_user() -> HumanApprovalResult:
            if display is not None:
                display.pause()
            try:
                console.print()
                console.print(Panel(content, title=f"[bold yellow]{message}[/bold yellow]", border_style="yellow"))
                console.print(
                    "  [dim][a] approve  [r] reject with feedback  [e] edit[/dim]"
                )
                while True:
                    raw = input("  > ").strip().lower()
                    if raw in ("a", "approve"):
                        return HumanApprovalResult(decision="approve")
                    elif raw in ("r", "reject"):
                        feedback = input("  Feedback: ").strip()
                        return HumanApprovalResult(decision="reject", feedback=feedback or None)
                    elif raw in ("e", "edit"):
                        console.print("  [dim]Enter edited content (empty line to finish):[/dim]")
                        lines = []
                        while True:
                            line = input()
                            if line == "":
                                break
                            lines.append(line)
                        return HumanApprovalResult(decision="edit", edited_content="\n".join(lines))
                    console.print("  [dim]Please enter a, r, or e[/dim]")
            finally:
                if display is not None:
                    display.resume()

        return await loop.run_in_executor(None, _prompt_user)

    return human_approval_fn


def _make_ask_user_fn(state: ChatSessionState) -> Any:
    """Return an async ask_user callback for the turn-based path."""

    async def ask_user_fn(question: str) -> str:
        display = state.current_display
        loop = asyncio.get_running_loop()

        def _prompt_user() -> str:
            if display is not None:
                display.pause()
            try:
                console.print()
                console.print(f"[bold cyan]Agent question:[/bold cyan] {question}")
                answer = input("  > ").strip()
                return answer
            finally:
                if display is not None:
                    display.resume()

        return await loop.run_in_executor(None, _prompt_user)

    return ask_user_fn


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_chat(
    config_path: str,
    orchestration: Optional[str],
    resume: bool,
    session_id: Optional[str],
) -> None:
    """Entry point for the chat command."""
    try:
        orch = Orchestration.from_file(config_path)
    except Exception as e:
        error_console.print(f"[red]Error loading config:[/red] {e}")
        sys.exit(1)

    # Resolve orchestration name
    orch_names = orch.get_orchestration_names()
    if not orchestration:
        if not orch_names:
            error_console.print("[red]No orchestrations found in config.[/red]")
            sys.exit(1)
        if len(orch_names) == 1:
            orchestration = orch_names[0]
        else:
            error_console.print(
                f"Multiple orchestrations: {', '.join(orch_names)}. Use --orchestration."
            )
            sys.exit(1)

    orch_def = orch.config.orchestrations[orchestration]
    if orch_def.mode != "conversational":
        error_console.print(
            f"[red]Error:[/red] Orchestration '{orchestration}' is not in conversational mode.\n"
            "Set [bold]mode: conversational[/bold] in the config."
        )
        sys.exit(1)

    input_handling = orch_def.input_handling
    use_concurrent = input_handling and input_handling.mode in ("queue", "interrupt")

    if use_concurrent:
        _run_chat_concurrent(
            orch=orch,
            orchestration=orchestration,
            config_path=config_path,
            resume=resume,
            session_id=session_id,
            input_handling=input_handling,
        )
    else:
        _run_chat_turn_based(
            orch=orch,
            orchestration=orchestration,
            config_path=config_path,
            resume=resume,
            session_id=session_id,
        )


_CONCURRENT_SENTINEL = object()  # Shutdown token for the orchestration queue


def _run_chat_concurrent(
    orch: Orchestration,
    orchestration: str,
    config_path: str,
    resume: bool,
    session_id: Optional[str],
    input_handling: InputHandlingConfig,
) -> None:
    """Concurrent chat loop supporting queue/interrupt input handling."""
    asyncio.run(
        _chat_session_async(
            orch=orch,
            orchestration=orchestration,
            config_path=config_path,
            resume=resume,
            session_id=session_id,
            input_handling=input_handling,
        )
    )


async def _chat_session_async(
    orch: Orchestration,
    orchestration: str,
    config_path: str,
    resume: bool,
    session_id: Optional[str],
    input_handling: InputHandlingConfig,
) -> None:
    """Async session: one input-loop task + one orchestration-loop task."""
    store = SessionStore()
    abs_config_path = str(Path(config_path).resolve())

    # ── Session setup ──────────────────────────────────────────────────────
    result = _resolve_session(store, abs_config_path, orchestration, resume, session_id)
    if result is None:
        return
    session, session_was_loaded = result

    # Load queued messages saved from a previous interrupted session
    _SHUTDOWN_MARKER = "__SHUTDOWN__"
    pending_msgs = [
        m for m in store.load_pending_queue(session.id) if m != _SHUTDOWN_MARKER
    ]
    store.clear_pending_queue(session.id)

    # ── Infrastructure ─────────────────────────────────────────────────────
    bus = EventBus()
    input_handler = InputHandler(input_handling, event_bus=bus)
    chat_queue: asyncio.Queue = asyncio.Queue()
    for msg in pending_msgs:
        chat_queue.put_nowait(msg)

    state = ChatSessionState(
        session=session,
        session_persisted=session_was_loaded,
        turn_number=len(session.message_history) // 2 + 1,
    )

    # ── Print header ───────────────────────────────────────────────────────
    agent_count = len(orch.config.agent_types)
    tool_names = list(orch.config.tools.keys())
    _mcp = getattr(orch.config, "mcp_servers", None) or {}
    mcp_servers = list(_mcp.keys())
    print_session_header(
        orchestration=orchestration,
        config_path=abs_config_path,
        agent_count=agent_count,
        tool_names=tool_names,
        mcp_servers=mcp_servers,
        session_id=session.id,
    )
    console.print("[dim]Type /help for commands. /i <msg> to interrupt.[/dim]")
    if pending_msgs:
        console.print(f"[dim]Replaying {len(pending_msgs)} queued message(s).[/dim]")
    console.print()

    # ── Display history and detect incomplete turns ─────────────────────────
    if session_was_loaded:
        _display_loaded_history(store, session.id)
        pending_input = _get_pending_input(store, session.id)
        if pending_input:
            console.print(
                f"[dim]Your last message didn't complete. Re-running: "
                f'"{pending_input[:80]}{"…" if len(pending_input) > 80 else ""}"[/dim]'
            )
            console.print()
            chat_queue.put_nowait(pending_input)

    # ── Approval / ask_user callbacks (concurrent path) ────────────────────
    # These route input from _input_loop via pending_future instead of
    # blocking the event loop with run_in_executor.

    async def _approval_fn(agent_name: str, tool_name: str, arguments: Dict[str, Any]) -> bool:
        cached = state.session_policies.get(tool_name)
        if cached == "always_allow":
            return True
        if cached == "always_deny":
            return False

        if state.current_display:
            state.current_display.pause()

        args_text = ", ".join(f"{k}={repr(v)[:40]}" for k, v in list(arguments.items())[:4])
        console.print()
        console.print(
            f"[bold yellow]Tool approval required[/bold yellow]  "
            f"[cyan]{agent_name}[/cyan] wants to call [bold]{tool_name}[/bold]({args_text})"
        )
        console.print(
            "  [dim][y] allow  [n] deny  [a] always allow this session  "
            "[d] always deny this session[/dim]"
        )

        loop = asyncio.get_running_loop()
        state.pending_future = loop.create_future()
        try:
            answer = (await state.pending_future).strip().lower()
        finally:
            state.pending_future = None
            if state.current_display:
                state.current_display.resume()

        if answer in ("a", "always"):
            state.session_policies[tool_name] = "always_allow"
            return True
        if answer == "d":
            state.session_policies[tool_name] = "always_deny"
            return False
        return answer in ("y", "yes")

    async def _human_approval_fn(message: str, content: str) -> HumanApprovalResult:
        if state.current_display:
            state.current_display.pause()

        console.print()
        console.print(Panel(content, title=f"[bold yellow]{message}[/bold yellow]", border_style="yellow"))
        console.print("  [dim][a] approve  [r] reject with feedback  [e] edit[/dim]")

        loop = asyncio.get_running_loop()
        state.pending_future = loop.create_future()
        try:
            raw = (await state.pending_future).strip().lower()
        finally:
            state.pending_future = None

        if raw in ("a", "approve"):
            if state.current_display:
                state.current_display.resume()
            return HumanApprovalResult(decision="approve")

        if raw in ("r", "reject"):
            console.print("  Feedback (Enter to skip): ", end="")
            state.pending_future = loop.create_future()
            try:
                feedback = (await state.pending_future).strip()
            finally:
                state.pending_future = None
            if state.current_display:
                state.current_display.resume()
            return HumanApprovalResult(decision="reject", feedback=feedback or None)

        if raw in ("e", "edit"):
            console.print("  [dim]Enter edited content (empty line to finish):[/dim]")
            lines = []
            while True:
                state.pending_future = loop.create_future()
                try:
                    line = await state.pending_future
                finally:
                    state.pending_future = None
                if line == "":
                    break
                lines.append(line)
            if state.current_display:
                state.current_display.resume()
            return HumanApprovalResult(decision="edit", edited_content="\n".join(lines))

        # Unrecognised → reject
        if state.current_display:
            state.current_display.resume()
        return HumanApprovalResult(decision="reject")

    async def _ask_user_fn(question: str) -> str:
        if state.current_display:
            state.current_display.pause()

        console.print()
        console.print(f"[bold cyan]Agent question:[/bold cyan] {question}")

        loop = asyncio.get_running_loop()
        state.pending_future = loop.create_future()
        try:
            answer = (await state.pending_future).strip()
        finally:
            state.pending_future = None
            if state.current_display:
                state.current_display.resume()

        return answer

    set_ask_user_fn(_ask_user_fn)

    # ── Input loop ─────────────────────────────────────────────────────────

    async def _input_loop() -> None:
        prompt_session: PromptSession = PromptSession(history=InMemoryHistory())

        while True:
            try:
                user_input = await prompt_session.prompt_async(
                    FormattedText([("bold", "> ")])
                )
            except KeyboardInterrupt:
                # Ctrl+C: interrupt agent if running, else exit
                if state.pending_future is not None and not state.pending_future.done():
                    state.pending_future.set_result("n")
                if state.agent_running:
                    input_handler.request_interrupt("")
                    continue
                console.print("\n[dim]Goodbye![/dim]")
                await chat_queue.put(_CONCURRENT_SENTINEL)
                break
            except EOFError:
                console.print("\n[dim]Goodbye![/dim]")
                await chat_queue.put(_CONCURRENT_SENTINEL)
                break

            user_input = (user_input or "").strip()
            if not user_input:
                continue

            # Route to pending approval / ask_user future
            if state.pending_future is not None and not state.pending_future.done():
                state.pending_future.set_result(user_input)
                continue

            # Slash commands
            if user_input.startswith("/"):
                cmd = user_input.lower().split()[0]
                # /i is exclusive to concurrent mode — check before dispatch
                if user_input.startswith("/i ") or user_input == "/i":
                    msg = user_input[3:].strip() if len(user_input) > 2 else ""
                    if state.agent_running:
                        input_handler.request_interrupt(msg)
                    else:
                        console.print("[dim](No agent turn is running)[/dim]")
                    continue
                signal = _dispatch_slash(cmd, user_input, state, store, abs_config_path)
                if signal == _SLASH_QUIT:
                    await chat_queue.put(_CONCURRENT_SENTINEL)
                    break
                if signal == _SLASH_HANDLED and cmd == "/help":
                    # Concurrent mode extends /help with the /i command
                    console.print(
                        "  [bold cyan]/i <msg>[/bold cyan]  "
                        "[dim]Interrupt the current agent turn[/dim]"
                    )
                continue

            # Route message: queue to input_handler while agent runs, else to chat_queue
            if state.agent_running:
                accepted = input_handler.submit(user_input)
                if not accepted:
                    console.print("[dim]Input queue full — message dropped.[/dim]")
            else:
                await chat_queue.put(user_input)

    # ── Orchestration loop ─────────────────────────────────────────────────

    async def _orchestration_loop() -> None:
        while True:
            msg = await chat_queue.get()
            if msg is _CONCURRENT_SENTINEL:
                break

            state.agent_running = True
            turn_bus = EventBus()
            display = ConcurrentDisplay(turn_bus)
            state.current_display = display
            display.start()
            display.set_status("running")

            # Pre-save user input before agent runs (crash-safe resume)
            turn_id = _start_turn(store, state, msg, orchestration, abs_config_path)
            collector = TurnEventCollector(turn_bus)

            try:
                response = await orch.chat(
                    orchestration,
                    msg,
                    session=state.session,
                    event_bus=turn_bus,
                    stream_responses=True,
                    approval_fn=_approval_fn,
                    human_approval_fn=_human_approval_fn,
                    input_handler=input_handler,
                )
            except OrchestrationInterrupted as exc:
                collector.stop()
                _finish_turn(store, turn_id, state, "", collector.get_events(), 0.0, status="error")
                display.stop()
                state.current_display = None
                state.agent_running = False
                _interrupt_msg = exc.interrupt_message
                def _print_interrupt() -> None:
                    console.print(f"\n[dim]Interrupted: {_interrupt_msg}[/dim]")
                try:
                    await _pt_run_in_terminal(_print_interrupt)
                except Exception:
                    _print_interrupt()
                # Only re-enqueue if the message looks like a new instruction,
                # not a bare cancel signal.
                _cancel_words = {"", "stop", "cancel", "abort", "quit", "exit"}
                if exc.interrupt_message.strip().lower() not in _cancel_words:
                    await chat_queue.put(exc.interrupt_message)
                continue
            except Exception as exc:
                collector.stop()
                _finish_turn(store, turn_id, state, "", collector.get_events(), 0.0, status="error")
                display.stop()
                state.current_display = None
                state.agent_running = False
                _err = exc
                def _print_error() -> None:
                    error_console.print(f"[red]Error:[/red] {_err}")
                try:
                    await _pt_run_in_terminal(_print_error)
                except Exception:
                    _print_error()
                continue

            collector.stop()
            elapsed = time.monotonic() - display.output.turn_start
            display.stop()
            state.current_display = None

            _response_md = Markdown(response)
            _stats = Text()
            _stats.append(f"  {elapsed:.1f}s", style="dim")
            if display.output.agent_call_count:
                _stats.append("  ·  ", style="dim")
                n = display.output.agent_call_count
                _stats.append(
                    f"{n} agent invocation{'s' if n != 1 else ''}",
                    style="dim",
                )

            def _print_response() -> None:
                console.print()
                console.print(_response_md)
                console.print(_stats)
                console.print()

            try:
                await _pt_run_in_terminal(_print_response)
            except Exception:
                _print_response()

            _finish_turn(store, turn_id, state, response, collector.get_events(), elapsed * 1000)
            state.turn_number += 1
            state.agent_running = False

            # Drain queued messages into chat_queue for the next turn
            for queued in input_handler.drain_queue():
                await chat_queue.put(queued.message)

    # ── Run both tasks ─────────────────────────────────────────────────────

    try:
        input_task = asyncio.create_task(_input_loop())
        orch_task = asyncio.create_task(_orchestration_loop())
        await asyncio.gather(input_task, orch_task)
    except Exception as exc:
        error_console.print(f"[red]Session error:[/red] {exc}")
    finally:
        # Notify context strategy of session end (best-effort)
        if state.session is not None:
            try:
                await orch.end_session(orchestration, state.session)
            except Exception:
                logger.warning("Failed to run orch.end_session", exc_info=True)

        set_ask_user_fn(None)
        # Persist any unprocessed queued messages so they survive a restart
        queued_msgs: list = []
        for item in input_handler.drain_queue():
            queued_msgs.append(item.message)
        while not chat_queue.empty():
            try:
                item = chat_queue.get_nowait()
                if item is not _CONCURRENT_SENTINEL:
                    queued_msgs.append(item)
            except asyncio.QueueEmpty:
                break
        if queued_msgs and state.session:
            try:
                if not state.session_persisted:
                    store.save_session(
                        state.session, orchestration=orchestration, config_path=abs_config_path
                    )
                store.save_pending_queue(state.session.id, queued_msgs)
            except Exception:
                logger.warning("Failed to persist session/pending queue", exc_info=True)


def _run_chat_turn_based(
    orch: Orchestration,
    orchestration: str,
    config_path: str,
    resume: bool,
    session_id: Optional[str],
) -> None:
    """Turn-based chat loop (original behavior)."""
    store = SessionStore()
    abs_config_path = str(Path(config_path).resolve())

    result = _resolve_session(store, abs_config_path, orchestration, resume, session_id)
    if result is None:
        return
    session, session_was_loaded = result

    agent_count = len(orch.config.agent_types)
    tool_names = list(orch.config.tools.keys())
    mcp_servers = list(orch.config.mcp_servers.keys()) if hasattr(orch.config, "mcp_servers") else []

    print_session_header(
        orchestration=orchestration,
        config_path=config_path,
        agent_count=agent_count,
        tool_names=tool_names,
        mcp_servers=mcp_servers,
        session_id=session.id,
    )
    console.print("[dim]Type /help for commands[/dim]")
    console.print()

    state = ChatSessionState(
        session=session,
        session_persisted=session_was_loaded,
        turn_number=len(session.message_history) // 2 + 1,
    )

    # Display history and detect incomplete turns
    pending_input: Optional[str] = None
    if session_was_loaded:
        _display_loaded_history(store, session.id)
        pending_input = _get_pending_input(store, session.id)
        if pending_input:
            console.print(
                f"[dim]Your last message didn't complete. Re-running: "
                f'"{pending_input[:80]}{"…" if len(pending_input) > 80 else ""}"[/dim]'
            )
            console.print()

    approval_fn = _make_approval_fn(state)
    human_approval_fn = _make_human_approval_fn(state)
    ask_user_fn = _make_ask_user_fn(state)
    set_ask_user_fn(ask_user_fn)

    prompt_session: PromptSession = PromptSession(history=InMemoryHistory())

    while True:
        if pending_input is not None:
            user_input = pending_input
            pending_input = None
        else:
            try:
                user_input = prompt_session.prompt(FormattedText([("bold", "> ")]))
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye![/dim]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                cmd = user_input.lower().split()[0]
                signal = _dispatch_slash(cmd, user_input, state, store, abs_config_path)
                if signal == _SLASH_QUIT:
                    break
                continue

        bus = EventBus()
        display = TurnDisplay(bus)
        state.current_display = display
        display.start()

        # Pre-save user input before agent runs (crash-safe resume)
        turn_id = _start_turn(store, state, user_input, orchestration, abs_config_path)
        collector = TurnEventCollector(bus)

        try:
            t0 = time.monotonic()
            response = asyncio.run(
                orch.chat(
                    orchestration,
                    user_input,
                    session=state.session,
                    event_bus=bus,
                    stream_responses=True,
                    approval_fn=approval_fn,
                    human_approval_fn=human_approval_fn,
                )
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
        except Exception as e:
            collector.stop()
            _finish_turn(store, turn_id, state, "", collector.get_events(), 0.0, status="error")
            display.stop()
            error_console.print(f"[red]Error:[/red] {e}")
            continue

        collector.stop()
        display.stop()
        display.print_response(response)
        display.print_stats(response)

        _finish_turn(store, turn_id, state, response, collector.get_events(), elapsed_ms)
        state.turn_number += 1

    # Notify context strategy of session end (best-effort)
    if state.session is not None:
        try:
            asyncio.run(orch.end_session(orchestration, state.session))
        except Exception:
            logger.warning("Failed to run orch.end_session", exc_info=True)

    set_ask_user_fn(None)


def _list_sessions(store: SessionStore, config_path: str) -> None:
    summaries = store.list_sessions(config_path=config_path)
    if not summaries:
        console.print("[dim]No saved sessions.[/dim]")
        return
    console.print(f"\n[bold]Saved sessions for {config_path}:[/bold]")
    for s in summaries[:10]:
        console.print(
            f"  [cyan]{s.id[:8]}[/cyan]  "
            f"[dim]{s.updated_at[:19]}[/dim]  "
            f"{s.turn_count} turn{'s' if s.turn_count != 1 else ''}"
        )
    console.print()

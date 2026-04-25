"""Batch run command for Elasticity."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .. import Orchestration
from ..events import EventBus
from ..tracing import RunTrace
from .display import BatchObserver, console, error_console


def run_orchestration(
    config_path: str,
    orchestration: Optional[str],
    input_data: Optional[dict],
    show_trace: bool,
    verbose: bool,
) -> None:
    """Execute a batch orchestration run."""
    import asyncio

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

    # Set up event bus and observability
    bus = EventBus()
    run_trace = RunTrace("cli_run", orchestration, log_to_console=False) if show_trace else None
    if run_trace:
        run_trace.subscribe_to(bus)

    if verbose:
        console.print(f"[bold]Running orchestration:[/bold] {orchestration}")
        BatchObserver(bus, verbose=True)

    try:
        result = asyncio.run(
            orch.run(orchestration, input_data=input_data, event_bus=bus)
        )
    except Exception as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    # Output result
    orch_def = orch.config.orchestrations[orchestration]
    if orch_def.mode == "conversational":
        response_key = orch_def.response_key or "response"
        messages = result.get("messages") if isinstance(result.get("messages"), dict) else None
        if messages and response_key in messages:
            console.print(f"\n{messages[response_key]}\n")
        else:
            console.print_json(json.dumps(result, default=str))
    else:
        console.print_json(json.dumps(result, default=str))

    if show_trace and run_trace:
        console.print("\n[bold]Execution Trace:[/bold]")
        console.print_json(json.dumps(run_trace.to_dict(), default=str))

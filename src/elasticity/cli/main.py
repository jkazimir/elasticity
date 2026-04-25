"""Main CLI entry point for Elasticity."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

from ..config.loader import load_config
from ..config.validator import validate_references
from ..config.global_loader import (
    get_global_config_path,
    get_data_dir,
    load_global_config,
    get_default_global_config_yaml,
)
from ..diagram import generate_sequence_diagram
from ..errors import ConfigError, ConfigReferenceError, ValidationError
from ..storage import SessionStore
from .display import console, error_console
from .chat import run_chat
from .run import run_orchestration
from ..web.server import web_command


@click.group()
@click.version_option()
def main() -> None:
    """Elasticity: Configuration-driven agent orchestration framework."""
    pass


main.add_command(web_command)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--orchestration", "-o", help="Orchestration name to run")
@click.option("--input", "-i", "input_str", help="Input JSON string or @file.json")
@click.option("--trace", "show_trace", is_flag=True, help="Show execution trace after run")
@click.option("--verbose", "-v", is_flag=True, help="Show agent activity during execution")
def run(
    config_path: str,
    orchestration: Optional[str],
    input_str: Optional[str],
    show_trace: bool,
    verbose: bool,
) -> None:
    """Run an orchestration from a config file."""
    input_data = _parse_input(input_str)
    run_orchestration(config_path, orchestration, input_data, show_trace, verbose)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--orchestration", "-o", help="Orchestration name to chat with")
@click.option("--resume", is_flag=True, help="Resume the most recent session for this config")
@click.option("--session", "session_id", default=None, help="Resume a specific session by ID")
def chat(
    config_path: str,
    orchestration: Optional[str],
    resume: bool,
    session_id: Optional[str],
) -> None:
    """Start an interactive chat session with a conversational orchestration."""
    run_chat(config_path, orchestration, resume, session_id)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@main.command()
@click.option("--config", "config_path", default=None, help="Filter by config file path")
@click.option("--delete", "delete_id", default=None, help="Delete a session by ID")
def sessions(config_path: Optional[str], delete_id: Optional[str]) -> None:
    """List or manage saved chat sessions."""
    store = SessionStore()

    if delete_id:
        if store.delete_session(delete_id):
            console.print(f"[green]Deleted session {delete_id[:8]}[/green]")
        else:
            error_console.print(f"[red]Session '{delete_id}' not found.[/red]")
            sys.exit(1)
        return

    abs_config = str(Path(config_path).resolve()) if config_path else None
    summaries = store.list_sessions(config_path=abs_config)

    if not summaries:
        console.print("[dim]No saved sessions.[/dim]")
        return

    console.print(f"\n[bold]Saved sessions ({len(summaries)}):[/bold]")
    for s in summaries:
        console.print(
            f"  [cyan]{s.id[:8]}[/cyan]  "
            f"[dim]{s.updated_at[:19]}[/dim]  "
            f"[bold]{s.orchestration}[/bold]  "
            f"{s.turn_count} turn{'s' if s.turn_count != 1 else ''}  "
            f"[dim]{s.config_path}[/dim]"
        )
    console.print()


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
def validate(config_path: str) -> None:
    """Validate a configuration file."""
    try:
        config = load_config(config_path)
        validate_references(config)
        console.print(f"[green]✓[/green] Configuration file '{config_path}' is valid.")
        console.print(f"  [dim]{len(config.agent_types)} agent type(s)[/dim]")
        console.print(f"  [dim]{len(config.tools)} tool(s)[/dim]")
        if config.mcp_servers:
            console.print(f"  [dim]{len(config.mcp_servers)} MCP server(s)[/dim]")
        console.print(f"  [dim]{len(config.orchestrations)} orchestration(s)[/dim]")
    except (ConfigError, ValidationError, ConfigReferenceError) as e:
        error_console.print(f"[red]✗ Validation failed:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[red]✗ Error:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command(name="list")
@click.argument("config_path", type=click.Path(exists=True))
def list_cmd(config_path: str) -> None:
    """List orchestrations in a configuration file."""
    try:
        from .. import Orchestration
        orch = Orchestration.from_file(config_path)
        orch_names = orch.get_orchestration_names()

        if not orch_names:
            console.print("[dim]No orchestrations found.[/dim]")
            return

        console.print(f"[bold]Orchestrations in '{config_path}':[/bold]")
        for name in orch_names:
            orch_def = orch.config.orchestrations[name]
            desc = orch_def.description or "[dim](no description)[/dim]"
            mode = orch_def.mode
            console.print(f"  [cyan]{name}[/cyan] [dim]({mode})[/dim]  {desc}")
    except Exception as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


@main.command()
@click.argument("trace_file", type=click.Path(exists=True))
def trace(trace_file: str) -> None:
    """View an execution trace from a JSON file."""
    try:
        with open(trace_file) as f:
            trace_data = json.load(f)
        console.print("[bold]Execution Trace:[/bold]")
        console.print_json(json.dumps(trace_data, default=str))
    except Exception as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# diagram
# ---------------------------------------------------------------------------


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--orchestration", "-o", help="Orchestration name")
def diagram(config_path: str, orchestration: Optional[str]) -> None:
    """Generate a Mermaid sequence diagram for an orchestration."""
    try:
        config = load_config(config_path)
        orch_names = list(config.orchestrations.keys())

        if not orchestration:
            if not orch_names:
                error_console.print("[red]No orchestrations found.[/red]")
                sys.exit(1)
            if len(orch_names) == 1:
                orchestration = orch_names[0]
            else:
                error_console.print(
                    f"Multiple orchestrations: {', '.join(orch_names)}. Use --orchestration."
                )
                sys.exit(1)

        diagram_text = generate_sequence_diagram(config, orchestration)
        console.print(f"# Sequence Diagram — {orchestration}")
        console.print("```mermaid")
        console.print(diagram_text)
        console.print("```")

    except KeyError as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@main.group()
def config() -> None:
    """Manage the global Elasticity application configuration."""
    pass


@config.command(name="path")
def config_path_cmd() -> None:
    """Print the resolved path to the global config file."""
    path = get_global_config_path()
    exists_marker = "" if path.exists() else " [dim](not yet created)[/dim]"
    console.print(str(path) + exists_marker)


@config.command(name="show")
def config_show() -> None:
    """Display the current effective global configuration."""
    path = get_global_config_path()
    try:
        cfg = load_global_config()
    except ConfigError as e:
        error_console.print(f"[red]Error loading global config:[/red] {e}")
        sys.exit(1)

    if not path.exists():
        console.print(f"[dim]No global config file found at {path}. Showing defaults.[/dim]\n")

    data = cfg.model_dump()
    console.print(yaml.dump(data, default_flow_style=False, sort_keys=False), end="")
    console.print(f"\n[dim]Data directory: {get_data_dir()}[/dim]")


@config.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
def config_init(force: bool) -> None:
    """Generate a default global config file."""
    path = get_global_config_path()
    if path.exists() and not force:
        console.print(
            f"[yellow]Global config already exists at {path}.[/yellow]\n"
            "Use --force to overwrite."
        )
        sys.exit(1)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(get_default_global_config_yaml(), encoding="utf-8")
        console.print(f"[green]✓[/green] Created global config at [bold]{path}[/bold]")
    except OSError as e:
        error_console.print(f"[red]Error creating config file:[/red] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# conduct
# ---------------------------------------------------------------------------


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--input", "-i", "goal", default=None, help="Goal for the conductor (plain string)")
@click.option("--stream", is_flag=True, default=False, help="Stream tokens as they arrive")
def conduct(config_path: str, goal: Optional[str], stream: bool) -> None:
    """Run a conductor with a single goal."""
    import asyncio
    from ..conductor import Conductor
    from .display import OutputRenderer

    if not goal:
        try:
            goal = click.prompt("Goal")
        except click.Abort:
            return

    try:
        conductor = Conductor(config_path)
    except Exception as e:
        error_console.print(f"[red]Error loading conductor:[/red] {e}")
        sys.exit(1)

    from ..events import EventBus
    bus = EventBus()
    renderer = OutputRenderer(bus, stream_mode=stream)

    async def _run() -> str:
        return await conductor.run(goal, event_bus=bus, stream_responses=stream)

    try:
        result = asyncio.run(_run())
        if result and not stream:
            console.print(result)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
    except Exception as e:
        error_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command("conduct-chat")
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--stream", is_flag=True, default=False, help="Stream tokens as they arrive")
def conduct_chat(config_path: str, stream: bool) -> None:
    """Start an interactive chat session with a conductor."""
    import asyncio
    from ..conductor import Conductor
    from ..runtime.session import Session

    try:
        conductor = Conductor(config_path)
    except Exception as e:
        error_console.print(f"[red]Error loading conductor:[/red] {e}")
        sys.exit(1)

    session = Session()
    console.print("[bold]Conductor chat[/bold]  [dim](Ctrl+C or 'exit' to quit)[/dim]\n")

    async def _turn(message: str) -> str:
        return await conductor.chat(message, session=session, stream_responses=stream)

    while True:
        try:
            message = click.prompt("You", prompt_suffix=" > ")
        except (click.Abort, EOFError):
            break

        if message.strip().lower() in ("exit", "quit", "bye"):
            break

        if not message.strip():
            continue

        try:
            response = asyncio.run(_turn(message))
            console.print(f"\n[bold cyan]Conductor:[/bold cyan] {response}\n")
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted.[/dim]")
            break
        except Exception as e:
            error_console.print(f"[red]Error:[/red] {e}")
            break

    try:
        asyncio.run(conductor.end_session(session))
    except Exception:
        pass  # Best-effort; don't crash on cleanup failure

    console.print("[dim]Session ended.[/dim]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_input(input_str: Optional[str]) -> Optional[dict]:
    """Parse --input option: JSON string or @file.json."""
    if input_str is None:
        return None
    if input_str.startswith("@"):
        input_file = Path(input_str[1:])
        if not input_file.exists():
            error_console.print(f"[red]Input file not found:[/red] {input_file}")
            sys.exit(1)
        try:
            with open(input_file) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            error_console.print(f"[red]Invalid JSON in {input_file}:[/red] {e}")
            sys.exit(1)
        if not isinstance(data, dict):
            error_console.print(
                f"[red]Input file must contain a JSON object, got {type(data).__name__}[/red]"
            )
            sys.exit(1)
        return data
    try:
        data = json.loads(input_str)
    except json.JSONDecodeError as e:
        error_console.print(f"[red]Invalid JSON input:[/red] {e}")
        sys.exit(1)
    if not isinstance(data, dict):
        error_console.print(
            f"[red]--input must be a JSON object ({{...}}), got {type(data).__name__}[/red]"
        )
        sys.exit(1)
    return data

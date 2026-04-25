"""FastAPI application factory and ``elasticity web`` CLI command."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import click

from ..tools.ask_user import set_ask_user_fn
from .run_manager import RunManager

try:
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from .api.configs import router as configs_router
    from .api.orchestrations import router as orchestrations_router
    from .api.sessions import router as sessions_router
    from .api.approvals import router as approvals_router
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

_STATIC_DIR = Path(__file__).parent / "static"


def _make_global_ask_user(mgr: RunManager):
    """Return a global ask_user function that dispatches to the current task's run."""

    async def _ask_user(question: str) -> str:
        run = mgr.get_run_for_current_task()
        if run is None:
            return "[ask_user not available in this context]"
        run.emit(
            f'data: {{"type": "ask_user", "run_id": "{run.run_id}", "question": {_json_str(question)}}}\n\n'
        )
        return await run.ask_user(question)

    return _ask_user


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


def create_app(config_dir: Path) -> "FastAPI":
    """Create and configure the FastAPI application."""
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError(
            "FastAPI is required for the web server.\n"
            "Install it with: pip install 'elasticity[web]'"
        )

    run_manager = RunManager()

    @asynccontextmanager
    async def lifespan(app: "FastAPI"):
        # Register global ask_user dispatcher.
        set_ask_user_fn(_make_global_ask_user(run_manager))
        yield
        # Clean up.
        set_ask_user_fn(None)

    app = FastAPI(title="Elasticity Web UI", lifespan=lifespan)

    # Attach shared state.
    app.state.config_dir = config_dir.resolve()
    app.state.run_manager = run_manager

    # API routes.
    app.include_router(configs_router)
    app.include_router(orchestrations_router)
    app.include_router(sessions_router)
    app.include_router(approvals_router)

    # Static files.
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # SPA root.
    @app.get("/")
    async def index():
        return FileResponse(_STATIC_DIR / "index.html")

    return app


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(name="web")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to")
@click.option("--port", default=8080, show_default=True, type=int, help="Port to listen on")
@click.option(
    "--config-dir",
    default=".",
    show_default=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Directory containing YAML orchestration config files",
)
@click.option("--reload", is_flag=True, help="Enable auto-reload on code changes (dev mode)")
def web_command(host: str, port: int, config_dir: str, reload: bool) -> None:
    """Start the Elasticity web UI server."""
    if not _FASTAPI_AVAILABLE:
        click.echo(
            "Error: FastAPI is not installed. Run: pip install 'elasticity[web]'",
            err=True,
        )
        raise SystemExit(1)

    try:
        import uvicorn
    except ImportError:
        click.echo(
            "Error: uvicorn is not installed. Run: pip install 'elasticity[web]'",
            err=True,
        )
        raise SystemExit(1)

    config_path = Path(config_dir).resolve()
    click.echo(f"Starting Elasticity web UI at http://{host}:{port}")
    click.echo(f"Serving configs from: {config_path}")

    if reload:
        # In reload mode, uvicorn manages the app import itself.
        import sys
        import os

        # Write a temp app factory shim that uvicorn can reload.
        # Simpler: just use the app factory string via uvicorn.run factory mode.
        # We'll store config_dir in an env var for the reloader.
        os.environ["ELASTICITY_WEB_CONFIG_DIR"] = str(config_path)

        uvicorn.run(
            "elasticity.web.server:_create_app_from_env",
            host=host,
            port=port,
            reload=True,
            factory=True,
        )
    else:
        app = create_app(config_path)
        uvicorn.run(app, host=host, port=port)


def _create_app_from_env() -> "FastAPI":
    """Factory used by uvicorn --reload (reads config dir from env)."""
    import os

    config_dir = Path(os.environ.get("ELASTICITY_WEB_CONFIG_DIR", "."))
    return create_app(config_dir)

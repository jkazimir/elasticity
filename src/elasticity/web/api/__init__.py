"""Shared utilities for the web API routers."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request


def get_config_dir(request: Request) -> Path:
    """Extract the config directory from the application state."""
    return request.app.state.config_dir  # type: ignore[attr-defined]


def resolve_config_path(config_dir: Path, config_id: str) -> Optional[Path]:
    """Resolve a ``~``-separated *config_id* to a filesystem path.

    ``~`` is used as the URL-safe directory separator so that subdirectory
    paths can be represented without conflicting with URL path segments.
    For example ``teams~planning`` resolves to ``<config_dir>/teams/planning.yaml``.

    Returns the resolved :class:`Path` if found, or ``None`` if the file does
    not exist.  Raises :class:`HTTPException` (400) on path-traversal attempts.
    """
    segments = config_id.split("~")
    if any(s.startswith(".") or s == "" for s in segments):
        raise HTTPException(status_code=400, detail="Invalid config id")
    rel_path = "/".join(segments)
    for ext in (".yaml", ".yml"):
        path = config_dir / (rel_path + ext)
        try:
            path.resolve().relative_to(config_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid config id")
        if path.exists() and path.is_file():
            return path
    return None

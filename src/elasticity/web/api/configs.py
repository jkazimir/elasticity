"""Config file browsing and inspection endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml
from fastapi import APIRouter, HTTPException, Request

from . import get_config_dir, resolve_config_path

router = APIRouter(tags=["configs"])


def _resolve_config_path(config_dir: Path, config_id: str) -> Path:
    """Find the YAML file corresponding to *config_id*. Raises 404 if not found."""
    path = resolve_config_path(config_dir, config_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")
    return path


def _is_conductor_config(config_path: Path) -> bool:
    """Return True if the YAML file has a top-level 'conductor' key.

    Peeks at the raw YAML without fully loading the schema, so it is safe to
    call on any .yaml/.yml file in the config directory.  Returns False on any
    parse or I/O error so that malformed files are silently skipped.
    """
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        return isinstance(raw, dict) and "conductor" in raw
    except Exception:
        return False


def _build_orchestration_list(
    config_dir: Path,
    config_id: str,
    config_path: Path,
) -> List[Dict[str, Any]]:
    """Load a config file and return a list of orchestration summary dicts.

    Files that are conductor configs (containing a top-level 'conductor' key)
    are intentionally skipped so that they do not appear in the orchestrations
    listing — preventing duplication across both sections (AC-4 guard).
    """
    from ... import Orchestration  # avoid circular at module import time

    # Guard: conductor configs must not appear in the orchestrations list.
    if _is_conductor_config(config_path):
        return []

    try:
        orch = Orchestration.from_file(str(config_path))
    except Exception:
        return []

    result: List[Dict[str, Any]] = []
    for name, orch_def in orch.config.orchestrations.items():
        result.append(
            {
                "config_id": config_id,
                "config_filename": config_path.name,
                "name": name,
                "mode": orch_def.mode,
                "description": getattr(orch_def, "description", None) or "",
                "input": dict(orch_def.input) if orch_def.input else {},
                "tool_count": len(orch.config.tools),
                "agent_count": len(orch.config.agent_types),
            }
        )
    return result


# ---------------------------------------------------------------------------
# /api/orchestrations — flat index across all configs (US-001)
# ---------------------------------------------------------------------------


@router.get("/api/orchestrations")
def list_all_orchestrations(request: Request) -> List[Dict[str, Any]]:
    """Return a flat list of every orchestration across all config files.

    Each entry includes the parent config context so the client can render
    a single consolidated index without a two-step drill-down.
    """
    config_dir = get_config_dir(request)
    seen_configs: set[str] = set()
    orchestrations: List[Dict[str, Any]] = []

    for path in sorted(config_dir.rglob("*.yaml")) + sorted(config_dir.rglob("*.yml")):
        config_id = str(path.relative_to(config_dir).with_suffix("")).replace("/", "~")
        if config_id in seen_configs:
            continue
        seen_configs.add(config_id)
        orchestrations.extend(
            _build_orchestration_list(config_dir, config_id, path)
        )

    return orchestrations


# ---------------------------------------------------------------------------
# /api/configs — config-level listing and detail
# ---------------------------------------------------------------------------


@router.get("/api/configs")
def list_configs(request: Request) -> List[Dict[str, Any]]:
    """Return all YAML config files available in the config directory."""
    config_dir = get_config_dir(request)
    seen: set[str] = set()
    configs: List[Dict[str, Any]] = []
    for path in sorted(config_dir.rglob("*.yaml")) + sorted(config_dir.rglob("*.yml")):
        config_id = str(path.relative_to(config_dir).with_suffix("")).replace("/", "~")
        if config_id in seen:
            continue
        seen.add(config_id)
        configs.append(
            {
                "id": config_id,
                "filename": path.name,
            }
        )
    return configs


@router.get("/api/configs/{config_id}/diagram/{orch_name}")
def get_config_diagram(config_id: str, orch_name: str, request: Request) -> Dict[str, Any]:
    """Generate a Mermaid sequence diagram for an orchestration."""
    from ... import Orchestration
    from ...diagram import generate_sequence_diagram

    config_dir = get_config_dir(request)
    config_path = _resolve_config_path(config_dir, config_id)

    try:
        orch = Orchestration.from_file(str(config_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if orch_name not in orch.config.orchestrations:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orch_name}' not found")

    try:
        mermaid = generate_sequence_diagram(orch.config, orch_name)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"orchestration": orch_name, "mermaid": mermaid}


@router.get("/api/configs/{config_id}")
def get_config(config_id: str, request: Request) -> Dict[str, Any]:
    """Load a config and return its orchestrations with metadata."""
    from ... import Orchestration  # avoid circular at import time

    config_dir = get_config_dir(request)
    config_path = _resolve_config_path(config_dir, config_id)

    try:
        orch = Orchestration.from_file(str(config_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    orchestrations: List[Dict[str, Any]] = []
    for name, orch_def in orch.config.orchestrations.items():
        orchestrations.append(
            {
                "name": name,
                "mode": orch_def.mode,
                "description": getattr(orch_def, "description", None) or "",
                "input": dict(orch_def.input) if orch_def.input else {},
                "tool_count": len(orch.config.tools),
                "agent_count": len(orch.config.agent_types),
            }
        )

    agents = [
        {
            "name": name,
            "model": agent_def.model,
            "system_prompt": agent_def.system_prompt,
            "tools": list(agent_def.tools),
            "can_spawn": list(agent_def.can_spawn),
        }
        for name, agent_def in orch.config.agent_types.items()
    ]

    tools = [
        {
            "name": name,
            "builtin": tool_def.builtin,
            "description": tool_def.description,
        }
        for name, tool_def in orch.config.tools.items()
    ]

    return {
        "id": config_id,
        "filename": config_path.name,
        "orchestrations": orchestrations,
        "agents": agents,
        "tools": tools,
    }


# ---------------------------------------------------------------------------
# /api/conductors — conductor config listing and detail
# ---------------------------------------------------------------------------


def _build_conductor_list(
    config_dir: Path,
    config_id: str,
    config_path: Path,
) -> List[Dict[str, Any]]:
    """Load a conductor config file and return a one-element summary list.

    Each conductor YAML is a 1-to-1 mapping (one file → one conductor), unlike
    orchestration configs which may define multiple orchestrations.  Returns an
    empty list when the file fails to load so that malformed configs are silently
    skipped, matching the pattern used by _build_orchestration_list.
    """
    from ...config.conductor_loader import load_conductor_config

    try:
        config = load_conductor_config(config_path.resolve())
    except Exception:
        return []

    return [
        {
            "config_id": config_id,
            "config_filename": config_path.name,
            "name": config_id,
            "agent": config.conductor.agent,
            "team_count": len(config.teams),
        }
    ]


@router.get("/api/conductors")
def list_all_conductors(request: Request) -> List[Dict[str, Any]]:
    """Return a flat list of all conductor configurations in the config directory.

    Scans for *.yaml and *.yml files (same glob pattern used by
    list_all_orchestrations) and filters to those that have a top-level
    'conductor' key.  Each entry includes enough metadata for the index page
    to render the conductors section without a further round-trip.

    Returns an empty list when zero conductor configs exist; the frontend uses
    this to decide whether to render the conductors section at all (AC-5: hide
    section entirely when no conductors are present).
    """
    config_dir = get_config_dir(request)
    seen_configs: set[str] = set()
    conductors: List[Dict[str, Any]] = []

    for path in sorted(config_dir.rglob("*.yaml")) + sorted(config_dir.rglob("*.yml")):
        config_id = str(path.relative_to(config_dir).with_suffix("")).replace("/", "~")
        if config_id in seen_configs:
            continue
        seen_configs.add(config_id)
        if _is_conductor_config(path):
            conductors.extend(
                _build_conductor_list(config_dir, config_id, path)
            )

    return conductors


@router.get("/api/conductors/{config_id}")
def get_conductor(config_id: str, request: Request) -> Dict[str, Any]:
    """Load a conductor config and return its full detail including teams.

    Returns:
        A dict with keys: id, filename, agent, teams (list).

    Raises:
        400: If config_id contains path-traversal characters.
        404: If the file does not exist or is not a conductor config.
        422: If the file exists but fails schema validation.
    """
    from ...config.conductor_loader import load_conductor_config

    config_dir = get_config_dir(request)
    config_path = _resolve_config_path(config_dir, config_id)

    if not _is_conductor_config(config_path):
        raise HTTPException(
            status_code=404,
            detail=f"'{config_id}' is not a conductor config",
        )

    try:
        config = load_conductor_config(config_path.resolve())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    teams: List[Dict[str, Any]] = []
    for name, team_def in config.teams.items():
        teams.append(
            {
                "name": name,
                "description": team_def.description,
                "orchestration": team_def.orchestration,
                "config": team_def.config,
                "input": dict(team_def.input) if team_def.input else {},
                "output": team_def.output,
            }
        )

    agents = [
        {
            "name": name,
            "model": agent_def.model,
            "system_prompt": agent_def.system_prompt,
            "tools": list(agent_def.tools),
            "can_spawn": list(agent_def.can_spawn),
        }
        for name, agent_def in config.agent_types.items()
    ]

    tools = [
        {
            "name": name,
            "builtin": tool_def.builtin,
            "description": tool_def.description,
        }
        for name, tool_def in config.tools.items()
    ]

    return {
        "id": config_id,
        "filename": config_path.name,
        "agent": config.conductor.agent,
        "teams": teams,
        "agents": agents,
        "tools": tools,
    }

"""Session listing and management endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from ...storage import SessionStore
from . import get_config_dir, resolve_config_path

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
def list_sessions(
    request: Request,
    config_id: Optional[str] = Query(None, description="Filter by config id (stem)"),
) -> List[Dict[str, Any]]:
    """List saved sessions, optionally filtered to a specific config file."""
    store = SessionStore()
    config_dir = get_config_dir(request)

    # Resolve config_path filter if a config_id was provided.
    config_path: Optional[str] = None
    if config_id:
        resolved = resolve_config_path(config_dir, config_id)
        if resolved is None:
            return []  # Config file doesn't exist — no sessions possible.
        config_path = str(resolved.resolve())

    summaries = store.list_sessions(config_path=config_path)
    return [
        {
            "id": s.id,
            "orchestration": s.orchestration,
            "config_path": s.config_path,
            "title": s.title,
            "turn_count": s.turn_count,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
        for s in summaries
    ]


@router.get("/{session_id}/history")
def get_session_history(session_id: str) -> Dict[str, Any]:
    """Return the full message history for a session, including stored events per turn."""
    store = SessionStore()
    if not store.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    raw_turns = store.get_turns(session_id)

    # Surface any incomplete (pending) turn so the frontend can offer to re-run.
    pending_input: Optional[str] = None
    if raw_turns and raw_turns[-1]["status"] == "pending":
        pending_input = raw_turns[-1]["user_input"]

    turns = []
    for t in raw_turns:
        if t["status"] != "complete":
            continue
        turns.append(
            {
                "turn": t["turn_number"],
                "user": t["user_input"],
                "assistant": t["response"],
                "events": t["agent_outputs"].get("events", []),
                "duration_ms": t["duration_ms"],
            }
        )

    return {"session_id": session_id, "pending_input": pending_input, "turns": turns}


@router.delete("/{session_id}")
def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete a session by ID (or unique prefix)."""
    store = SessionStore()
    if not store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"ok": True, "deleted": session_id}

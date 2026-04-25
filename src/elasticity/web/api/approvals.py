"""Approval / ask_user response endpoints.

When an orchestration task is blocked waiting for human input (tool approval,
ask_user, or an APPROVE node), the client POSTs to one of these endpoints.
The handler resolves the pending ``asyncio.Future`` held by the :class:`ActiveRun`
so the orchestration coroutine unblocks and continues.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..run_manager import RunManager

router = APIRouter(prefix="/api/runs", tags=["approvals"])


def _run_manager(request: Request) -> RunManager:
    return request.app.state.run_manager  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ApprovalResponse(BaseModel):
    """Response to a tool-policy approval prompt.

    ``decision`` must be one of: ``"allow"`` | ``"deny"`` | ``"always_allow"`` | ``"always_deny"``
    """

    decision: str


class AskUserResponse(BaseModel):
    answer: str


class HumanApprovalResponse(BaseModel):
    """Response to an APPROVE-node human-review prompt.

    ``decision`` must be one of: ``"approve"`` | ``"reject"`` | ``"edit"``
    """

    decision: str
    feedback: Optional[str] = None
    edited_content: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{run_id}/approval")
async def submit_approval(
    run_id: str,
    body: ApprovalResponse,
    request: Request,
) -> Dict[str, Any]:
    """Resolve a pending tool-approval future."""
    mgr = _run_manager(request)
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found or already finished")

    valid = {"allow", "deny", "always_allow", "always_deny"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision must be one of {sorted(valid)}")

    if run.pending_future is None or run.pending_future.done():
        raise HTTPException(status_code=409, detail="No pending approval for this run")
    if run.pending_type != "approval":
        raise HTTPException(status_code=409, detail=f"Pending input is '{run.pending_type}', not 'approval'")

    run.pending_future.set_result(body.decision)
    return {"ok": True}


@router.post("/{run_id}/ask_user")
async def submit_ask_user(
    run_id: str,
    body: AskUserResponse,
    request: Request,
) -> Dict[str, Any]:
    """Resolve a pending ask_user future."""
    mgr = _run_manager(request)
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found or already finished")

    if run.pending_future is None or run.pending_future.done():
        raise HTTPException(status_code=409, detail="No pending ask_user for this run")
    if run.pending_type != "ask_user":
        raise HTTPException(status_code=409, detail=f"Pending input is '{run.pending_type}', not 'ask_user'")

    run.pending_future.set_result(body.answer)
    return {"ok": True}


@router.post("/{run_id}/human_approval")
async def submit_human_approval(
    run_id: str,
    body: HumanApprovalResponse,
    request: Request,
) -> Dict[str, Any]:
    """Resolve a pending APPROVE-node human-review future."""
    mgr = _run_manager(request)
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found or already finished")

    valid = {"approve", "reject", "edit"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision must be one of {sorted(valid)}")

    if run.pending_future is None or run.pending_future.done():
        raise HTTPException(status_code=409, detail="No pending human approval for this run")
    if run.pending_type != "human_approval":
        raise HTTPException(status_code=409, detail=f"Pending input is '{run.pending_type}', not 'human_approval'")

    run.pending_future.set_result(
        {
            "decision": body.decision,
            "feedback": body.feedback,
            "edited_content": body.edited_content,
        }
    )
    return {"ok": True}

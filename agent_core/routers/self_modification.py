"""
Self-modification: /self-modification/*.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from agent_core.orchestrator import orchestrator, require_admin_token

router = APIRouter(prefix="/self-modification", tags=["Auto-modificación"])


class SelfModProposeRequest(BaseModel):
    target_path: str
    proposed_source: str
    justification: str


class SelfModApplyRequest(BaseModel):
    proposal_id: str
    approved_by: str


class SelfModRejectRequest(BaseModel):
    proposal_id: str
    rejected_by: str = ""
    reason: str = ""


@router.post("/propose", dependencies=[Depends(require_admin_token)])
def propose_self_modification(req: SelfModProposeRequest):
    proposal = orchestrator.self_modification.propose(req.target_path, req.proposed_source, req.justification)
    return {"proposal_id": proposal.id, "status": proposal.status, "detail": proposal.detail}


@router.post("/apply", dependencies=[Depends(require_admin_token)])
def apply_self_modification(req: SelfModApplyRequest):
    try:
        proposal = orchestrator.self_modification.apply(req.proposal_id, req.approved_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"proposal_id": proposal.id, "status": proposal.status}


@router.post("/reject", dependencies=[Depends(require_admin_token)])
def reject_self_modification(req: SelfModRejectRequest):
    """
    El espejo de `/apply`: registrar que un humano revisó y dijo NO. Sin esto, la
    única salida de `pending_human_approval` era aplicar el cambio.
    """
    try:
        proposal = orchestrator.self_modification.reject(
            req.proposal_id, rejected_by=req.rejected_by, reason=req.reason
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"proposal_id": proposal.id, "status": proposal.status, "detail": proposal.detail}


@router.get("")
def list_self_modifications():
    return [
        {"id": p.id, "target_path": p.target_path, "justification": p.justification, "status": p.status, "detail": p.detail}
        for p in orchestrator.self_modification.list_proposals()
    ]


@router.get("/{proposal_id}")
def get_self_modification(proposal_id: str):
    proposal = orchestrator.self_modification.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal

"""
Herramientas y skills: /tools, /tools/{name}/*, /skills.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_core.orchestrator import orchestrator, require_admin_token

router = APIRouter(tags=["Herramientas"])


class ToolApproveRequest(BaseModel):
    approved_by: str = Field(description="Identidad de quien aprueba (auditado, no verificado por este endpoint).")


class ToolRollbackRequest(BaseModel):
    to_version: int = Field(description="Versión firmada a la que revertir.")
    approved_by: str = Field(description="Identidad de quien aprueba (auditado, no verificado por este endpoint).")


@router.get("/tools", summary="Listar herramientas activas y pendientes de aprobación")
def list_tools():
    return {"active": orchestrator.tools.list_active(), "pending": orchestrator.tools.list_pending()}


@router.post(
    "/tools/{name}/approve",
    dependencies=[Depends(require_admin_token)],
    summary="Aprobar una herramienta pendiente",
)
def approve_tool(name: str, req: ToolApproveRequest):
    try:
        orchestrator.tools.approve_pending_tool(name, approved_by=req.approved_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"name": name, "status": "active"}


@router.get("/skills", summary="Listar Skills instaladas")
def list_skills():
    return {"skills": orchestrator.tools.list_skills()}


@router.get("/tools/{name}/versions", summary="Historial de versiones de una herramienta")
def list_tool_versions(name: str):
    """
    VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    2026-09-26), B-5: list_versions() rutea por VersionStore._tool_dir(),
    que rechaza con ValueError cualquier nombre que no matchee el
    charset válido (ver kernel/registry/versioning.py) — sin este
    try/except, ese ValueError llegaba sin capturar hasta FastAPI, que
    lo convierte en 500 crudo. Un nombre inválido es un error del
    CLIENTE (400), nunca una falla real del servidor.
    """
    try:
        return {"name": name, "versions": orchestrator.tools.list_versions(name)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tools/{name}/verify", summary="Verificar la firma de una herramienta")
def verify_tool(name: str):
    return {"name": name, "signature_valid": orchestrator.tools.verify_tool_integrity(name)}


@router.post(
    "/tools/{name}/rollback",
    dependencies=[Depends(require_admin_token)],
    summary="Revertir una herramienta a una versión firmada anterior",
)
def rollback_tool(name: str, req: ToolRollbackRequest):
    try:
        orchestrator.tools.rollback_tool(name, req.to_version, approved_by=req.approved_by)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"name": name, "version": req.to_version, "status": "active"}

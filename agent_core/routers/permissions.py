"""
Permission Manager de filesystem y de red: /filesystem-access/*,
/network-access/*. Comparten el motor genérico
kernel/permissions/access_manager.py — el filesystem tiene además
report-outcome (una escritura ocurre del lado de la extensión de VS
Code, fuera del conocimiento del backend); la red no lo necesita
(una descarga sucede enteramente DENTRO del backend, que ya sabe el
resultado real sin que nadie se lo reporte).
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_core.orchestrator import require_admin_token
from audit.audit_log import AuditEvent, audit_log
from kernel.permissions.filesystem_access_manager import (
    FilesystemAccessError,
    filesystem_access_manager,
)
from kernel.permissions.network_access_manager import (
    NetworkAccessError,
    network_access_manager,
)

router = APIRouter(tags=["Permisos"])


def _validate_request_id(request_id: str) -> str:
    # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    # 2026-09-26), parte de C-4: report-outcome queda sin token admin a
    # propósito (ver docstring más abajo — la extensión de VS Code no
    # tiene forma de obtenerlo), pero sin ESTA validación cualquiera
    # podía inyectar entradas arbitrarias en el log de auditoría con un
    # request_id inventado. Los IDs reales siempre son uuid4 (ver
    # tool_integration/adapters/vscode_android.py, kernel/permissions/
    # filesystem_access_manager.py) — esto no autentica al llamador,
    # pero cierra la inyección trivial de basura no estructurada.
    try:
        UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="request_id inválido.")
    return request_id


class FilesystemAccessApproveRequest(BaseModel):
    # "once" | "session" | "project" | "skill" — ver
    # kernel/permissions/filesystem_access_manager.py::GrantLevel.
    level: str = Field(default="once", description="Alcance de la concesión: once | session | project | skill.")


class FilesystemAccessOutcomeRequest(BaseModel):
    # Reportado por la extensión de VS Code después de que el usuario
    # decide en la vista previa — el Kernel ya auto-permitió la acción
    # por política, esto deja constancia de qué pasó DE VERDAD (auditoría
    # con datos reales, no solo "se permitió").
    outcome: str = Field(description="'written' | 'discarded' — qué pasó realmente del lado del cliente.")
    files_written: list[str] = Field(default_factory=list, max_length=200)


class NetworkAccessApproveRequest(BaseModel):
    # "once" | "session" | "project" | "skill" — ver
    # kernel/permissions/network_access_manager.py::GrantLevel.
    level: str = Field(default="once", description="Alcance de la concesión: once | session | project | skill.")


# --- Permission Manager de filesystem ---
#
# La política default (config.yaml: filesystem_access.auto_allow) ya
# auto-permite crear/modificar dentro del workspace de VS Code — hoy
# nada llega acá pidiendo aprobación en la práctica. Estos endpoints
# quedan listos para cuando una Skill futura (o una acción
# delete/rename de VS Code) sí lo necesite.

@router.get("/filesystem-access", summary="Listar accesos a filesystem pendientes de aprobación")
def list_pending_filesystem_access():
    return [
        {
            "id": p.id, "skill_name": p.skill_name, "scope": p.scope.value,
            "action": p.action.value, "resource_key": p.resource_key,
        }
        for p in filesystem_access_manager.list_pending()
    ]


@router.post(
    "/filesystem-access/{request_id}/approve",
    dependencies=[Depends(require_admin_token)],
    summary="Aprobar un acceso a filesystem pendiente",
)
def approve_filesystem_access(request_id: str, req: FilesystemAccessApproveRequest):
    try:
        filesystem_access_manager.approve(request_id, level=req.level)
    except (FilesystemAccessError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": request_id, "status": "approved"}


@router.post(
    "/filesystem-access/{request_id}/deny",
    dependencies=[Depends(require_admin_token)],
    summary="Denegar un acceso a filesystem pendiente",
)
def deny_filesystem_access(request_id: str):
    try:
        filesystem_access_manager.deny(request_id)
    except FilesystemAccessError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": request_id, "status": "denied"}


@router.post("/filesystem-access/{request_id}/report-outcome", summary="Reportar qué pasó realmente con un acceso auto-permitido")
def report_filesystem_access_outcome(request_id: str, req: FilesystemAccessOutcomeRequest):
    """
    Sin token admin a propósito: el Kernel ya auto-permitió esta acción
    por política (auto_allow), esto solo deja constancia auditada de
    qué pasó DE VERDAD del lado de la extensión (¿el usuario aplicó la
    propuesta o la descartó?) — nunca decide nada, solo audita.
    """
    request_id = _validate_request_id(request_id)
    audit_log.record(
        AuditEvent(
            event_type="filesystem_access_granted" if req.outcome == "written" else "filesystem_access_denied",
            summary=f"Extensión de VS Code reportó '{req.outcome}' para la solicitud {request_id}",
            context={"request_id": request_id, "outcome": req.outcome, "files_written": req.files_written},
            outcome="success" if req.outcome == "written" else "failure",
        )
    )
    return {"id": request_id, "outcome": req.outcome}


# --- Permission Manager de red ---

@router.get("/network-access", summary="Listar accesos a red pendientes de aprobación")
def list_pending_network_access():
    return [
        {
            "id": p.id, "skill_name": p.skill_name, "scope": p.scope.value,
            "action": p.action.value, "resource_key": p.resource_key,
        }
        for p in network_access_manager.list_pending()
    ]


@router.post(
    "/network-access/{request_id}/approve",
    dependencies=[Depends(require_admin_token)],
    summary="Aprobar un acceso a red pendiente",
)
def approve_network_access(request_id: str, req: NetworkAccessApproveRequest):
    try:
        network_access_manager.approve(request_id, level=req.level)
    except (NetworkAccessError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": request_id, "status": "approved"}


@router.post(
    "/network-access/{request_id}/deny",
    dependencies=[Depends(require_admin_token)],
    summary="Denegar un acceso a red pendiente",
)
def deny_network_access(request_id: str):
    try:
        network_access_manager.deny(request_id)
    except NetworkAccessError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": request_id, "status": "denied"}

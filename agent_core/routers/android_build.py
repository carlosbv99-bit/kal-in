"""
/android-build/*: auditoría de qué pasó DE VERDAD con un pedido de
android_build_and_screenshot (ver tool_integration/adapters/
vscode_android.py) después de que la extensión de VS Code compila,
instala y captura del lado real del dispositivo — el backend nunca ve
ese trabajo, solo deja constancia. Mismo espíritu que
agent_core/routers/permissions.py::report_filesystem_access_outcome
(sin token admin, best-effort, nunca decide nada) pero en un router
propio: a diferencia de la escritura de archivos, esto no pasa por
ningún Access Manager (filesystem/network) — no hay ninguna decisión
de permiso que auditar, solo el resultado real de compilar/instalar.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from audit.audit_log import AuditEvent, audit_log

router = APIRouter(tags=["Integración VS Code"])


class AndroidBuildOutcomeRequest(BaseModel):
    # "installed" | "build_failed" | "no_device" | "discarded" — ver
    # vscode-extension/src/androidBuild.ts para cada caso real.
    outcome: str
    detail: str = Field(default="", max_length=2000)


@router.post("/android-build/{request_id}/report-outcome")
def report_android_build_outcome(request_id: str, req: AndroidBuildOutcomeRequest):
    # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    # 2026-09-26), parte de C-4: sin token admin a propósito (mismo
    # motivo que report_filesystem_access_outcome en
    # agent_core/routers/permissions.py — la extensión de VS Code no
    # tiene forma de obtener el token), pero sin esta validación
    # cualquiera podía inyectar entradas arbitrarias en el log de
    # auditoría. Los IDs reales siempre son uuid4 (ver
    # tool_integration/adapters/vscode_android.py).
    try:
        UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="request_id inválido.")
    audit_log.record(
        AuditEvent(
            event_type="android_build_completed" if req.outcome == "installed" else "android_build_failed",
            summary=f"Extensión de VS Code reportó '{req.outcome}' para el build {request_id}",
            context={"request_id": request_id, "outcome": req.outcome, "detail": req.detail[:2000]},
            outcome="success" if req.outcome == "installed" else "failure",
        )
    )
    return {"id": request_id, "outcome": req.outcome}

"""
Auditoría: /audit/tail.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from agent_core.orchestrator import require_admin_token
from audit.audit_log import audit_log

router = APIRouter(prefix="/audit", tags=["Auditoría"])


# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), C-4: el log de auditoría completo (acciones
# administrativas, decisiones de permisos, metadatos de conversación)
# se servía sin ningún control de acceso — es exactamente el tipo de
# endpoint que un atacante usaría primero para reconocimiento.
@router.get(
    "/tail",
    dependencies=[Depends(require_admin_token)],
    summary="Últimas N entradas del log de auditoría, con verificación de cadena",
)
def audit_tail(n: int = Query(default=50, ge=1, le=1000)):
    """
    M-7 (auditoría externa Likay-OS, 2026-09-26): AuditLog.tail() trata
    n<=0 como "sin límite" (devuelve el archivo COMPLETO, sin importar
    su tamaño — ver audit/audit_log.py) — ge=1/le=1000 lo rechaza (422)
    antes de que llegue tan lejos.
    """
    return {"verified": audit_log.verify_chain(), "entries": audit_log.tail(n)}

"""
Estado general del agente: /health, /status, /models — sin
dependencias de ningún dominio específico (memoria, self-mod,
permisos...), por eso viven separados del resto.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from agent_core.llm.provider import ProviderError
from agent_core.orchestrator import orchestrator
from audit.audit_log import audit_log
from error_handling.circuit_breaker import circuit_breaker
from utils.config import settings

router = APIRouter(tags=["Sistema"])


@router.get("/health", summary="Liveness check")
def health():
    return {"status": "ok"}


# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), M-7: audit.log es append-only A PROPÓSITO (nunca rota,
# ver audit/audit_log.py) — verify_chain() relee y recalcula el hash de
# CADA entrada desde el disco, un costo que crece sin límite con la
# vida entera del proceso. /status no pide token admin (es un
# "franja de estado" de uso normal) y el frontend lo consulta
# periódicamente — sin caché, cada poll paga ese costo completo de
# nuevo, y cualquiera en la red (autenticado o no) podía forzarlo a
# voluntad. Cachear el resultado un puñado de segundos preserva la
# semántica real (sigue verificando la cadena ENTERA, no una ventana
# recortada) acotando cuántas veces por minuto se paga ese costo.
_AUDIT_VERIFY_CACHE_SECONDS = 5.0
_audit_verify_cache: dict[str, float | bool] = {"result": True, "checked_at": 0.0}


def _cached_audit_chain_verified() -> bool:
    now = time.monotonic()
    if now - _audit_verify_cache["checked_at"] >= _AUDIT_VERIFY_CACHE_SECONDS:
        _audit_verify_cache["result"] = audit_log.verify_chain()
        _audit_verify_cache["checked_at"] = now
    return _audit_verify_cache["result"]


@router.get("/status", summary="Estado de las garantías de seguridad del sistema")
def status():
    """
    Estado de las garantías de seguridad del sistema, usado por la
    franja de estado del frontend — no decoración, son las propiedades
    reales que hacen que kal sea seguro de usar.
    """
    pending_tools = len(orchestrator.tools.list_pending())
    pending_selfmod = sum(1 for p in orchestrator.self_modification.list_proposals() if p.status == "pending_human_approval")
    return {
        "audit_chain_verified": _cached_audit_chain_verified(),
        "sandbox_network_mode": settings.sandbox.network_mode,
        "pending_tool_approvals": pending_tools,
        "pending_self_modification_approvals": pending_selfmod,
        "open_circuit_breakers": circuit_breaker.open_circuit_count(),
        "llm_available": orchestrator.llm.is_available(),
    }


@router.get("/models", summary="Modelos LLM disponibles en el proveedor activo")
def list_models():
    try:
        return {"models": orchestrator.llm.list_models(), "default": settings.llm.default_model}
    except ProviderError as e:
        raise HTTPException(status_code=503, detail=str(e))

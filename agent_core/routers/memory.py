"""
Memoria en tres niveles: /memory/*.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_core.orchestrator import orchestrator, require_admin_token

router = APIRouter(prefix="/memory", tags=["Memoria"])

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), C-4: estos cinco endpoints mutan memoria persistente
# (mediano/largo plazo) sin ninguna autenticación — cualquiera con
# acceso de red podía forzar una consolidación, marcar cualquier ítem
# como verificado con un `verified_by` inventado (ver también M-3),
# fijarlo como no-olvidable, o borrar memoria completa con filtros
# amplios. /search queda sin token: es de solo lectura y tiene el
# mismo nivel de confianza que /chat (que tampoco pide token — ver
# require_admin_token en agent_core/orchestrator.py, reservado para
# acciones administrativas/mutantes, no para el uso normal del agente).


@router.get("/search")
def search_memory(q: str, top_k: int = Query(default=5, ge=1, le=100)):
    """
    M-7 (auditoría externa Likay-OS, 2026-09-26): top_k llegaba crudo
    hasta un `LIMIT ?` de SQLite (mid_term.py) — un valor negativo
    (p.ej. top_k=-1) hace que SQLite interprete "sin límite" y devuelva
    la tabla ENTERA, sin importar qué tan grande sea. ge=1/le=100 lo
    rechaza (422) antes de que llegue tan lejos.
    """
    results = orchestrator.memory.recall(q, top_k=top_k)
    return {
        tier: [
            {"id": i.id, "content": i.content, "metadata": i.metadata, "confidence": i.confidence.value}
            for i in items
        ]
        for tier, items in results.items()
    }


@router.post("/consolidate", dependencies=[Depends(require_admin_token)])
def consolidate():
    return orchestrator.run_consolidation_cycle()


@router.post("/{tier}/{item_id}/verify", dependencies=[Depends(require_admin_token)])
def verify_memory(tier: str, item_id: str):
    """
    M-3 (auditoría externa Likay-OS, 2026-09-26): `verified_by` era un
    string libre que el propio cliente elegía — sin ninguna identidad
    real detrás, cualquiera con el token admin podía firmar como
    "system" o cualquier otro nombre inventado. Este proyecto no tiene
    (todavía) principals por usuario, solo UN token admin compartido
    (ver require_admin_token) — "admin" es la única identidad honesta
    que se puede afirmar hoy: quien llega hasta acá YA demostró tener
    ese token, no hace falta que además lo declare él mismo.
    """
    try:
        item = orchestrator.memory.verify(item_id, tier, verified_by="admin")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": item.id, "confidence": item.confidence.value}


@router.post("/{tier}/{item_id}/pin", dependencies=[Depends(require_admin_token)])
def pin_memory(tier: str, item_id: str):
    try:
        item = orchestrator.memory.pin(item_id, tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": item.id, "confidence": item.confidence.value}


@router.delete("/{tier}/{item_id}", dependencies=[Depends(require_admin_token)])
def forget_memory(tier: str, item_id: str):
    """Derecho al olvido, un item puntual (mid_term/long_term, mismo
    alcance que verify()/pin() — corto plazo no tiene identidad estable
    fuera de la tarea activa). Idempotente: borrar un id que ya no
    existe no es un error."""
    try:
        orchestrator.memory.forget(item_id, tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted": item_id, "tier": tier}


@router.delete("", dependencies=[Depends(require_admin_token)])
def forget_matching_memory(
    keyword: str | None = None,
    tier: str | None = None,
    classification: str | None = None,
    before: float | None = None,
    after: float | None = None,
):
    """Derecho al olvido, masivo con filtros — exige al menos uno
    (ver MemoryManager.forget_matching()), nunca borra todo sin
    condición. Sin `tier`, aplica a los tres niveles."""
    try:
        deleted = orchestrator.memory.forget_matching(
            keyword=keyword, tier=tier, classification=classification, before=before, after=after,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"deleted_count": deleted}

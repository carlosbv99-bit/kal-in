"""
Intervención humana sobre el circuit breaker.

**Por qué existe.** Cuando un circuito se abre, el sistema ya AUDITA
`human_escalation` ("Error requiere intervención humana") y publica
`open_circuit_breakers` en `/status` para el frontend — pero hasta ahora no había
ningún mecanismo para intervenir: `reset()` no tenía un solo llamador. La única
forma de reabrir una firma era reiniciar el proceso, y eso **no es un costo
chico**: `SessionManager` es puramente en memoria (`agent_core/sessions.py`), así
que reiniciar para destrabar UNA firma de error tira todas las sesiones activas.
O sea que la opción "dejar el reinicio como intervención" no era "dejar el
reinicio", era convertir cualquier circuito atascado en un incidente para todas
las conversaciones en curso.

Los dos endpoints usan `require_admin_token`, el mismo gate que ya protege
`/memory/consolidate`, `/audit` y las aprobaciones de self-modification: no es una
decisión de auth nueva, es la que ya existe para "un humano decide intervenir
sobre estado sensible".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_core.orchestrator import require_admin_token
from error_handling.circuit_breaker import circuit_breaker

router = APIRouter(prefix="/error-handling", tags=["error-handling"])


@router.get(
    "/circuit-breakers",
    dependencies=[Depends(require_admin_token)],
    summary="Firmas de error con el circuito abierto (escaladas a humano)",
)
def list_circuit_breakers() -> dict:
    """
    Lo que un humano necesita para decidir: la firma, cuántos intentos fallidos
    acumuló y **desde cuándo** (`first_seen` de la racha actual, más su edad en
    segundos). Sin esto, quien mira la escalación no sabe si el circuito se abrió
    hace un minuto o hace tres días.
    """
    abiertos = circuit_breaker.open_circuits()
    return {"count": len(abiertos), "open": list(abiertos)}


@router.post(
    "/circuit-breakers/{signature}/reset",
    dependencies=[Depends(require_admin_token)],
    summary="Reabrir a mano el circuito de una firma de error tras revisarla",
)
def reset_circuit_breaker(
    signature: str, approved_by: str = Query(default="", max_length=200)
) -> dict:
    """
    Reabre la firma para que el auto-repair vuelva a intentarla.

    404 si esa firma no tenía el circuito abierto: entre que un humano mira el
    listado y hace clic, el circuito puede haberse cerrado solo (un intento tuvo
    éxito), y decir "reabierto" ahí sería una confirmación falsa. La reapertura
    queda auditada (`circuit_breaker_reset`, con firma, intentos y quién).
    """
    intentos = circuit_breaker.reset(signature, approved_by=approved_by)
    if intentos is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No hay circuito abierto para la firma '{signature}'. "
                "Puede haberse cerrado solo (un intento posterior tuvo éxito) o no existir."
            ),
        )
    return {"reset": True, "signature": signature, "attempts": intentos}

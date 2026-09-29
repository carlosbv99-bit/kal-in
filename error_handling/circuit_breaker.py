"""
Circuit breaker: evita que un bucle de auto-reparación fallido consuma
recursos indefinidamente. Tras N intentos fallidos sobre el MISMO error
(identificado por firma, no por instancia), se abre el circuito y se
escala a un humano en vez de seguir reintentando.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from audit.audit_log import AuditEvent, audit_log
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


def error_signature(error_type: str, error_message: str, location: str) -> str:
    """Identifica un error de forma estable para no confundir reintentos
    de errores distintos con reintentos del mismo error."""
    raw = f"{error_type}:{location}:{error_message[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class _CircuitState:
    """
    Estado de una firma de error.

    `first_seen` es **desde cuándo viene fallando la racha actual**: un éxito
    reemplaza el estado entero, así que NO significa "la primera vez en la vida
    del proceso". Su consumidor es el listado de circuitos abiertos que expone el
    endpoint de intervención humana (`open_circuits()`): quien va a reabrir un
    circuito necesita saber desde cuándo está trabado y cuántos intentos lleva.

    Historia de este campo, porque explica la regla: se había sacado por no tener
    ningún lector (revisión, 2026-09-29), y volvió **cuando tuvo uno** — el
    listado. No es un campo "para después": si algún día se quiere reapertura
    automática por tiempo, no alcanza con leerlo, hace falta semántica *half-open*
    (dejar pasar UN intento de prueba tras el enfriamiento y cerrar solo si sale
    bien), que es una decisión de diseño.
    """

    attempts: int = 0
    first_seen: float = field(default_factory=time.time)
    open: bool = False


class CircuitBreaker:
    def __init__(self, max_attempts: int | None = None):
        self.max_attempts = max_attempts or settings.error_handling.max_repair_attempts
        self._states: dict[str, _CircuitState] = {}

    def allow_attempt(self, signature: str) -> bool:
        state = self._states.setdefault(signature, _CircuitState())
        return not state.open

    def open_circuit_count(self) -> int:
        """Para la franja de estado del frontend — cuántas firmas de error están escaladas ahora mismo."""
        return sum(1 for state in self._states.values() if state.open)

    def record_attempt(self, signature: str, success: bool, context: dict) -> None:
        state = self._states.setdefault(signature, _CircuitState())

        if success:
            # éxito resetea el contador para esa firma de error
            self._states[signature] = _CircuitState()
            return

        state.attempts += 1
        if state.attempts >= self.max_attempts:
            state.open = True
            logger.error(
                f"Circuit breaker ABIERTO para error {signature} "
                f"tras {state.attempts} intentos fallidos. Escalando a humano."
            )
            audit_log.record(
                AuditEvent(
                    event_type="circuit_breaker_triggered",
                    summary=f"Circuito abierto tras {state.attempts} intentos fallidos",
                    context={**context, "error_signature": signature},
                    outcome="escalated",
                )
            )
            audit_log.record(
                AuditEvent(
                    event_type="human_escalation",
                    summary="Error requiere intervención humana",
                    context={**context, "error_signature": signature},
                    outcome="pending",
                )
            )

    def open_circuits(self) -> tuple[dict[str, Any], ...]:
        """
        Las firmas con el circuito abierto, de la más vieja a la más nueva, con lo
        que hace falta para decidir una intervención: cuántos intentos fallidos y
        desde cuándo. Es el consumidor de `first_seen`.
        """
        abiertos = [(firma, st) for firma, st in self._states.items() if st.open]
        abiertos.sort(key=lambda par: (par[1].first_seen, par[0]))
        ahora = time.time()
        return tuple(
            {
                "signature": firma,
                "attempts": st.attempts,
                "first_seen": st.first_seen,
                "first_seen_ago_seconds": max(0.0, ahora - st.first_seen),
            }
            for firma, st in abiertos
        )

    def reset(self, signature: str, *, approved_by: str = "") -> int | None:
        """
        Reabre el circuito de una firma tras una revisión humana. Devuelve cuántos
        intentos fallidos había acumulado, o `None` si esa firma no estaba abierta
        (para que el llamador pueda distinguir "listo" de "no había nada que
        reabrir" en vez de mentir con un éxito).

        La reapertura se AUDITA: el sistema registra la apertura como algo que
        "requiere intervención humana", así que la intervención también tiene que
        quedar registrada — quién y sobre qué firma.
        """
        estado = self._states.get(signature)
        if estado is None or not estado.open:
            return None
        del self._states[signature]
        logger.warning(
            f"Circuito REABIERTO a mano para {signature} tras {estado.attempts} intentos"
            + (f" (por {approved_by})" if approved_by else "")
        )
        audit_log.record(
            AuditEvent(
                event_type="circuit_breaker_reset",
                summary=f"Circuito reabierto manualmente tras {estado.attempts} intentos fallidos",
                context={
                    "error_signature": signature,
                    "attempts": estado.attempts,
                    "approved_by": approved_by,
                },
                outcome="success",
            )
        )
        return estado.attempts


circuit_breaker = CircuitBreaker()

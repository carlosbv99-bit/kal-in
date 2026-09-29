"""
Endpoints de intervención humana sobre el circuit breaker.

Antes de esto, la escalación del breaker era **visible y no accionable**: se auditaba
`human_escalation` y el contador salía en `/status`, pero `reset()` no tenía un solo
llamador, así que la única forma de reabrir una firma era reiniciar el proceso — y eso
tira todas las sesiones activas (`SessionManager` es en memoria).

Se prueba el contrato completo: el gate administrativo (sin token no se ve ni se toca),
el listado con lo que hace falta para decidir, y que el reset cierre de verdad y deje
rastro. El gate en sí ya está cubierto de forma transversal en
`test_orchestrator_admin_auth.py`; acá se prueba la lógica de estos dos endpoints.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.orchestrator import _ADMIN_TOKEN, app
from error_handling import circuit_breaker as modulo
from error_handling.circuit_breaker import error_signature

client = TestClient(app, base_url="http://localhost")
_TOKEN = {"X-Kal-Admin-Token": _ADMIN_TOKEN}


@pytest.fixture
def circuito_abierto(monkeypatch):
    """
    Abre un circuito REAL sobre el singleton que usan los endpoints (no un doble:
    el objetivo es probar el camino completo) y lo limpia al terminar para no
    dejar estado global a otros tests.
    """
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: None)
    monkeypatch.setattr(modulo.circuit_breaker, "max_attempts", 1)
    firma = error_signature("RuntimeError", "circuito de prueba de la API", "task:api-test")
    modulo.circuit_breaker.record_attempt(firma, success=False, context={})
    assert modulo.circuit_breaker.allow_attempt(firma) is False
    try:
        yield firma
    finally:
        modulo.circuit_breaker.reset(firma)


def test_el_listado_exige_token():
    assert client.get("/error-handling/circuit-breakers").status_code == 401
    assert (
        client.get("/error-handling/circuit-breakers", headers={"X-Kal-Admin-Token": "malo"}).status_code
        == 401
    )


def test_el_listado_muestra_la_firma_los_intentos_y_desde_cuando(circuito_abierto):
    respuesta = client.get("/error-handling/circuit-breakers", headers=_TOKEN)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["count"] == 1
    entrada = next(c for c in cuerpo["open"] if c["signature"] == circuito_abierto)
    assert entrada["attempts"] == 1
    assert entrada["first_seen"] > 0
    assert entrada["first_seen_ago_seconds"] >= 0


def test_el_reset_exige_token(circuito_abierto):
    assert client.post(f"/error-handling/circuit-breakers/{circuito_abierto}/reset").status_code == 401
    # Y sin token el circuito sigue abierto: el gate no es decorativo.
    assert modulo.circuit_breaker.allow_attempt(circuito_abierto) is False


def test_el_reset_reabre_de_verdad_y_lo_deja_auditado(circuito_abierto, monkeypatch):
    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))

    respuesta = client.post(
        f"/error-handling/circuit-breakers/{circuito_abierto}/reset",
        headers=_TOKEN,
        params={"approved_by": "kalin"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"reset": True, "signature": circuito_abierto, "attempts": 1}
    assert modulo.circuit_breaker.allow_attempt(circuito_abierto) is True
    assert modulo.circuit_breaker.open_circuits() == ()
    evento = next(e for e in eventos if e.event_type == "circuit_breaker_reset")
    assert evento.context["approved_by"] == "kalin"


def test_reabrir_una_firma_que_no_estaba_abierta_da_404():
    """
    Entre que un humano mira el listado y hace clic, el circuito puede haberse
    cerrado solo (un intento posterior tuvo éxito). Confirmar "reabierto" ahí sería
    una confirmación falsa.
    """
    respuesta = client.post(
        "/error-handling/circuit-breakers/firma-que-no-existe/reset", headers=_TOKEN
    )

    assert respuesta.status_code == 404
    assert "No hay circuito abierto" in respuesta.json()["detail"]

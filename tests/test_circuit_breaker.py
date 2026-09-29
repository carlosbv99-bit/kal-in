"""
Tests del circuit breaker (`error_handling/circuit_breaker.py`).

No había NINGUNO (revisión, 2026-09-29), y es un componente que **bloquea un camino
de recuperación**: cuando el circuito de una firma de error se abre, `handle()` deja
de intentar repararla y la tarea se marca ESCALATED. Un contrato así no puede quedar
sin test.

Lo que se fija acá:

1. N fallos de la MISMA firma abren el circuito, y abierto significa `allow_attempt`
   False (o sea: el auto-repair de esa firma se detiene).
2. El circuito es por FIRMA: otro error sigue intentándose.
3. Un éxito resetea el contador de esa firma.
4. La apertura se escala: se auditan `circuit_breaker_triggered` y `human_escalation`.
5. `reset()` —el camino humano— vuelve a habilitar la firma.

El punto 5 es el que documenta una limitación real: `reset()` **no lo llama nadie**
en producción (ni endpoint, ni UI, ni CLI), así que hoy la única intervención humana
posible es reiniciar el proceso. El test lo fija como contrato disponible, no como
camino cableado.
"""
from __future__ import annotations

import pytest

from error_handling.circuit_breaker import CircuitBreaker, error_signature


@pytest.fixture
def breaker():
    return CircuitBreaker(max_attempts=2)


def _firma(mensaje: str = "boom") -> str:
    return error_signature("RuntimeError", mensaje, "task:1")


def test_dos_fallos_de_la_misma_firma_abren_el_circuito(breaker):
    firma = _firma()

    assert breaker.allow_attempt(firma) is True
    breaker.record_attempt(firma, success=False, context={})
    assert breaker.allow_attempt(firma) is True, "con un fallo todavía no se abre"

    breaker.record_attempt(firma, success=False, context={})

    assert breaker.allow_attempt(firma) is False
    assert breaker.open_circuit_count() == 1


def test_el_circuito_es_por_firma(breaker):
    una, otra = _firma("boom"), _firma("otro error")
    breaker.record_attempt(una, success=False, context={})
    breaker.record_attempt(una, success=False, context={})

    assert breaker.allow_attempt(una) is False
    assert breaker.allow_attempt(otra) is True, "un error distinto no paga el de otro"


def test_un_exito_resetea_el_contador_de_esa_firma(breaker):
    firma = _firma()
    breaker.record_attempt(firma, success=False, context={})

    breaker.record_attempt(firma, success=True, context={})

    assert breaker.allow_attempt(firma) is True
    assert breaker.open_circuit_count() == 0
    # Y el contador arranca de cero: un fallo posterior no abre de inmediato.
    breaker.record_attempt(firma, success=False, context={})
    assert breaker.allow_attempt(firma) is True


def test_la_apertura_se_escala_y_queda_auditada(breaker, monkeypatch):
    """La promesa del módulo es "se escala a un humano": eso tiene que dejar rastro."""
    from error_handling import circuit_breaker as modulo

    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))
    firma = _firma()

    breaker.record_attempt(firma, success=False, context={"tool": "run_code"})
    breaker.record_attempt(firma, success=False, context={"tool": "run_code"})

    tipos = [e.event_type for e in eventos]
    assert "circuit_breaker_triggered" in tipos
    assert "human_escalation" in tipos
    escalado = next(e for e in eventos if e.event_type == "human_escalation")
    assert escalado.context["error_signature"] == firma


def test_reset_vuelve_a_habilitar_la_firma_y_es_el_unico_camino(breaker):
    """
    `reset()` es el camino humano documentado. Se fija su efecto —y con él la
    limitación: al no tener llamadores en producción, la única forma real de
    reabrir hoy es reiniciar el proceso.
    """
    firma = _firma()
    breaker.record_attempt(firma, success=False, context={})
    breaker.record_attempt(firma, success=False, context={})
    assert breaker.allow_attempt(firma) is False

    breaker.reset(firma)

    assert breaker.allow_attempt(firma) is True
    assert breaker.open_circuit_count() == 0

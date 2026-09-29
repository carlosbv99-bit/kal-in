"""
Tests del gate (`agent_core/epistemics/gate.py`).

Lo que se prueba, en orden de importancia:

1. **Falla cerrado**: si el gate no puede evaluar, BLOQUEA. Un gate que ante un
   bug se abre es peor que no tener gate, porque se confía en él.
2. **R1**: el agente declaró que le faltaba información y respondió igual sin
   marcar incertidumbre → no sale.
3. **R2**: turno sin ninguna observación ingerida y respuesta larga → sale
   marcada como no verificada, no con la misma apariencia que una verificada.
4. Una abstención legítima y un hedge legítimo NO se bloquean: la abstención
   excesiva también es un fallo, y bloquear al agente por admitir que no sabe
   sería el peor incentivo posible.
"""
from __future__ import annotations

import pytest

from agent_core.epistemics import EpistemicStore
from agent_core.epistemics.gate import GateOutcome, evaluate_answer
from kal_epistemics import AbstentionKind, render_abstention

_AFIRMATIVA_LARGA = (
    "El servicio se cayó porque el proceso de Python consumió toda la memoria "
    "disponible durante el pico de tráfico de las 3 de la mañana, y el sistema "
    "operativo lo terminó forzosamente. Es un problema de configuración del límite "
    "de memoria del contenedor, que estaba puesto demasiado alto para la máquina."
)


def _store() -> EpistemicStore:
    return EpistemicStore(clock=lambda: 1000.0)


def _gate(store, answer, *, session_id="s", evidence_before=0, trivial=False):
    return evaluate_answer(
        answer,
        store=store,
        session_id=session_id,
        evidence_before=evidence_before,
        trivial=trivial,
    )


# --- 1. fail-closed -------------------------------------------------------


def test_si_el_gate_falla_bloquea(monkeypatch):
    store = _store()

    def explota(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "unsatisfied_deficits", explota)

    decision = _gate(store, "cualquier respuesta")

    assert decision.outcome is GateOutcome.BLOQUEAR
    assert decision.reason == "gate_error"
    assert decision.blocked is True


# --- 2. R1: déficit declarado y respondido igual --------------------------


def test_deficit_declarado_y_respuesta_afirmativa_bloquea():
    store = _store()
    store.ledger("s").open_question("¿Por qué se cayó a las 3:14?", missing=["el log del kernel"])

    decision = _gate(store, _AFIRMATIVA_LARGA)

    assert decision.outcome is GateOutcome.BLOQUEAR
    assert decision.reason == "deficit_declarado_y_respondido_igual"
    assert "el log del kernel" in decision.answer
    assert decision.deficits == ("el log del kernel",)


def test_deficit_declarado_y_abstencion_legitima_permite():
    store = _store()
    ledger = store.ledger("s")
    pregunta, _ = ledger.open_question(
        "¿Por qué se cayó a las 3:14?", missing=["el log del kernel"]
    )
    abstencion = render_abstention(
        pregunta,
        AbstentionKind.IGNORANCE,
        missing=["el log del kernel"],
    )

    decision = _gate(store, abstencion.text)

    assert decision.outcome is GateOutcome.PERMITIR


def test_deficit_declarado_y_respuesta_con_hedge_permite():
    store = _store()
    store.ledger("s").open_question("¿Por qué se cayó?", missing=["el log del kernel"])

    decision = _gate(store, "Probablemente fue por OOM, pero no lo pude verificarlo todavía.")

    assert decision.outcome is GateOutcome.PERMITIR


# --- 3. R2: turno sin observaciones --------------------------------------


def test_turno_sin_observaciones_y_respuesta_larga_advierte():
    store = _store()

    decision = _gate(store, _AFIRMATIVA_LARGA)

    assert decision.outcome is GateOutcome.ADVERTIR
    assert decision.reason == "sin_observaciones_en_el_turno"
    assert "Sin verificación" in decision.answer
    assert _AFIRMATIVA_LARGA in decision.answer  # se marca, no se reescribe


def test_una_respuesta_corta_no_se_advierte():
    store = _store()

    assert _gate(store, "Listo, ya está.").outcome is GateOutcome.PERMITIR


def test_una_respuesta_trivial_no_se_advierte():
    store = _store()

    decision = _gate(store, _AFIRMATIVA_LARGA, trivial=True)

    assert decision.outcome is GateOutcome.PERMITIR


def test_con_una_observacion_ingerida_no_se_advierte():
    store = _store()
    store.ingest_tool_result(session_id="s", tool_name="run_code", observation="exit 0")

    decision = _gate(store, _AFIRMATIVA_LARGA, evidence_before=0)

    assert decision.outcome is GateOutcome.PERMITIR


def test_una_observacion_previa_al_turno_no_cuenta_como_base_del_turno():
    """`evidence_before` es lo que hace que 'este turno' signifique este turno."""
    store = _store()
    store.ingest_tool_result(session_id="s", tool_name="run_code", observation="exit 0")
    antes = store.ingested_count("s")

    decision = _gate(store, _AFIRMATIVA_LARGA, evidence_before=antes)

    assert decision.outcome is GateOutcome.ADVERTIR


# --- bordes ---------------------------------------------------------------


def test_respuesta_vacia_permite_y_no_rompe():
    assert _gate(_store(), "").outcome is GateOutcome.PERMITIR
    assert _gate(_store(), "   ").outcome is GateOutcome.PERMITIR


@pytest.mark.parametrize(
    "hedge",
    [
        "No lo sé, me falta el log del kernel.",
        "No puedo determinarlo con lo que tengo.",
        "No tengo información suficiente para afirmarlo.",
        "Es posible que haya sido un OOM, aunque sin evidencia no lo afirmo.",
    ],
)
def test_los_hedges_declarados_no_se_bloquean(hedge):
    store = _store()
    store.ledger("s").open_question("¿Por qué se cayó?", missing=["el log del kernel"])

    assert _gate(store, hedge).outcome is GateOutcome.PERMITIR

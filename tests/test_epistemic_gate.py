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


# --- R3 (C): alternativas incompatibles, las dos respaldadas --------------


def _store_con_conflicto() -> EpistemicStore:
    """
    El caso real, por el camino real: dos candidatas a la misma pregunta, cada una
    respaldada por una observación distinta que entra por `ingest_tool_result`
    (el mismo camino que usa el loop). Nada de `set_status`: ningún camino del
    host pone SUPPORTED, así que una regla que dependiera de ese estado no se
    dispararía nunca en una conversación.
    """
    store = _store()
    ledger = store.ledger("s")
    pregunta, _ = ledger.open_question("¿Por qué se cayó el servicio a las 3:14?")
    from kal_epistemics import ReopenCondition

    primera = ledger.record_hypothesis(
        "Se cayó por falta de memoria",
        question_id=pregunta.id,
        reopen_conditions=[ReopenCondition.from_untrusted(content_keywords=("OutOfMemory",))],
    )
    ledger.record_hypothesis(
        "Se cayó por el deploy",
        question_id=pregunta.id,
        reopen_conditions=[ReopenCondition.from_untrusted(content_keywords=("deploy",))],
        excludes=(primera.id,),
    )
    store.ingest_tool_result(
        session_id="s", tool_name="run_code",
        observation="kernel: OutOfMemory killed process 4821",
    )
    store.ingest_tool_result(
        session_id="s", tool_name="browser",
        observation="el evento coincide con el deploy 2026.09.29-3",
    )
    return store


def test_r3_advierta_que_hay_alternativas_respaldadas_en_conflicto():
    store = _store_con_conflicto()

    decision = _gate(store, _AFIRMATIVA_LARGA, evidence_before=0)

    assert decision.outcome is GateOutcome.ADVERTIR
    assert decision.reason == "alternativas_respaldadas_en_conflicto"
    assert "Conflicto sin resolver" in decision.answer
    assert "mostrá las dos" in decision.answer


def test_r3_no_marca_una_respuesta_que_ya_muestra_las_dos():
    """Una respuesta que ya admite incertidumbre está haciendo lo que la regla
    pide: marcar sería ruido (misma lógica que el filtro de R2, medido en F6)."""
    store = _store_con_conflicto()

    decision = _gate(
        store,
        "Hay dos lecturas y no lo sé resolver: el kernel muestra falta de memoria y el "
        "panel atribuye el evento al deploy de esa madrugada.",
        evidence_before=0,
    )

    assert decision.outcome is GateOutcome.PERMITIR, decision.reason


def test_sin_conflicto_la_regla_no_existe():
    """El control: dos candidatas de la misma pregunta, una sola respaldada."""
    store = _store()
    ledger = store.ledger("s")
    pregunta, _ = ledger.open_question("¿Por qué se cayó?")
    from kal_epistemics import ReopenCondition

    primera = ledger.record_hypothesis(
        "Se cayó por falta de memoria",
        question_id=pregunta.id,
        reopen_conditions=[ReopenCondition.from_untrusted(content_keywords=("OutOfMemory",))],
    )
    ledger.record_hypothesis(
        "Se cayó por el deploy",
        question_id=pregunta.id,
        excludes=(primera.id,),
    )
    store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="kernel: OutOfMemory killed 4821"
    )

    decision = _gate(store, _AFIRMATIVA_LARGA, evidence_before=0)

    assert decision.reason != "alternativas_respaldadas_en_conflicto"


def test_el_expediente_muestra_el_conflicto_aunque_las_dos_esten_resueltas():
    """
    El conflicto va en una sección PROPIA porque `open_hypotheses()` excluye las
    SUPPORTED: si se mostrara solo ahí, el caso que importa —dos alternativas
    incompatibles, las dos respaldadas— no aparecería nunca.
    """
    store = _store_con_conflicto()

    bloque = store.context_block("s", provider="local")

    assert "CONFLICTO SIN RESOLVER" in bloque
    assert "Se cayó por falta de memoria" in bloque
    assert "Se cayó por el deploy" in bloque

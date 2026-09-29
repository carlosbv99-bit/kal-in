"""
Tests de seguridad de la capa epistémica.

Seguridad primero, aplicado a ESTA capa en concreto: cuando `open_question` y
`record_hypothesis` existan como tools (F2a), el modelo —y cualquier
inyección de prompt que lo convenza— pasa a escribir directamente en estos
objetos. Las garantías tienen que ser estructurales y estar fijadas por tests,
no depender de buena voluntad.

1. **El camino no confiable no puede pasar regex.** No es que se filtre: el
   constructor no tiene el parámetro. Un filtro es un heurístico; no ofrecer
   el parámetro es una garantía.
2. **Un patrón catastrófico se rechaza**, y si igual llegara (camino
   confiable), el matcher falla cerrado en vez de colgarse.
3. **Un patrón que no se puede analizar se rechaza**: aceptar algo cuyo costo
   no podemos acotar es peor que no matchear.
4. **El ledger tiene topes explícitos** y falla cerrado al alcanzarlos.
"""
from __future__ import annotations

import re

import pytest

import kal_epistemics as ke
from kal_epistemics import (
    Evidence,
    EvidenceOrigin,
    InvalidCondition,
    LedgerLimitExceeded,
    ReopenCondition,
    should_reopen,
)
from kal_epistemics.safety import pattern_is_unsafe


def _ev(**kwargs) -> Evidence:
    return Evidence(
        id=kwargs.pop("id", "ev1"),
        origin=kwargs.pop("origin", EvidenceOrigin.TOOL_EXTERNAL),
        anchor="evt_1",
        anchor_kind="audit_event_id",
        source=kwargs.pop("source", "journalctl -k"),
        content=kwargs.pop("content", ""),
    )


def _ledger() -> ke.HypothesisLedger:
    return ke.HypothesisLedger(clock=lambda: 1000.0)


# --- 1. el camino no confiable no puede pasar regex -----------------------


def test_el_constructor_no_confiable_no_acepta_patrones():
    """La prueba de que no es un filtro: el parámetro NO EXISTE."""
    with pytest.raises(TypeError):
        ReopenCondition.from_untrusted(content_pattern="Out of memory")
    with pytest.raises(TypeError):
        ReopenCondition.from_untrusted(source_pattern=".*")


def test_el_constructor_no_confiable_produce_una_condicion_sin_regex():
    cond = ReopenCondition.from_untrusted(
        description="si el log del kernel menciona OOM",
        source_in=("journalctl -k",),
        content_keywords=("Out of memory", "oom-kill"),
    )

    assert cond.source_pattern == ""
    assert cond.content_pattern == ""
    assert set(cond.origins) == {
        EvidenceOrigin.KERNEL_OBSERVED,
        EvidenceOrigin.TOOL_EXTERNAL,
        EvidenceOrigin.USER_ASSERTED,
    }


def test_las_palabras_clave_matchean_sin_motor_de_regex():
    cond = ReopenCondition.from_untrusted(content_keywords=("OUT OF MEMORY",))
    ev = _ev(content="03:13:58 kernel: Out of memory: Killed process 4821")

    decision = should_reopen(cond, ev)

    assert decision.matched is True
    assert decision.reason == "matched"


def test_una_palabra_clave_es_texto_literal_no_un_regex():
    """
    La demostración de que no hay motor de expresiones regulares: un patrón
    catastrófico pasado como palabra clave se busca LITERALMENTE, así que ni
    matchea ni cuelga.
    """
    cond = ReopenCondition.from_untrusted(content_keywords=("(a+)+$",))

    decision = should_reopen(cond, _ev(content="a" * 5000))

    assert decision.matched is False
    assert decision.reason == "keywords_not_found"


def test_source_in_es_coincidencia_exacta():
    cond = ReopenCondition.from_untrusted(source_in=("journalctl -k",))

    assert should_reopen(cond, _ev(source="journalctl -k")).matched is True
    assert should_reopen(cond, _ev(source="journalctl")).matched is False


def test_palabras_clave_demasiadas_o_vacias_o_largas_se_rechazan():
    with pytest.raises(InvalidCondition):
        ReopenCondition.from_untrusted(content_keywords=("   ",))
    with pytest.raises(InvalidCondition):
        ReopenCondition.from_untrusted(content_keywords=("k" * 300,))
    with pytest.raises(InvalidCondition):
        ReopenCondition.from_untrusted(content_keywords=tuple(f"k{i}" for i in range(30)))


# --- 2 y 3. patrones del camino confiable ---------------------------------


def test_un_patron_catastrofico_se_rechaza():
    assert pattern_is_unsafe("(a+)+$") == "nested_quantifier"
    assert pattern_is_unsafe("(a*)*b") == "nested_quantifier"


def test_un_patron_catastrofico_que_igual_llegara_falla_cerrado():
    """Defensa en profundidad: una condición construida a mano (camino
    confiable) con un patrón catastrófico no se evalúa."""
    cond = ReopenCondition(
        id="rc_malo",
        origins=(EvidenceOrigin.TOOL_EXTERNAL,),
        content_pattern="(a+)+$",
    )

    decision = should_reopen(cond, _ev(content="a" * 5000))

    assert decision.matched is False
    assert decision.reason == "unsafe_pattern:nested_quantifier"


def test_un_patron_demasiado_largo_se_rechaza():
    assert pattern_is_unsafe("a" * 300) == "pattern_too_long"


def test_un_patron_invalido_se_rechaza_como_invalid_pattern():
    assert pattern_is_unsafe("(sin cerrar") == "invalid_pattern"


def test_un_patron_que_no_se_puede_analizar_se_rechaza(monkeypatch):
    """Fail-closed: aceptar algo cuyo costo no podemos acotar es peor que no
    matchear."""
    def explota(*args, **kwargs):
        raise RuntimeError("parser roto")

    monkeypatch.setattr(re._parser, "parse", explota)

    assert pattern_is_unsafe("algo") == "unanalyzable_pattern"


# --- 4. topes del ledger --------------------------------------------------


def test_el_ledger_rechaza_una_proposicion_demasiado_larga():
    with pytest.raises(LedgerLimitExceeded):
        _ledger().record_hypothesis("x" * 3000)


def test_el_ledger_rechaza_una_pregunta_demasiado_larga():
    with pytest.raises(LedgerLimitExceeded):
        _ledger().open_question("x" * 3000)


def test_el_ledger_rechaza_evidencia_demasiado_grande():
    with pytest.raises(LedgerLimitExceeded):
        _ledger().add_evidence(
            Evidence.from_content("x" * 200_000, origin=EvidenceOrigin.TOOL_EXTERNAL)
        )


def test_el_ledger_rechaza_demasiadas_condiciones_por_hipotesis():
    condiciones = [
        ReopenCondition.from_untrusted(content_keywords=(f"clave{i}",)) for i in range(30)
    ]

    with pytest.raises(LedgerLimitExceeded):
        _ledger().record_hypothesis("A", reopen_conditions=condiciones)


def test_el_ledger_rechaza_demasiadas_dependencias():
    led = _ledger()
    base = led.record_hypothesis("base")

    with pytest.raises(LedgerLimitExceeded):
        led.record_hypothesis("X", depends_on=[base.id] * 51)


def test_el_ledger_rechaza_demasiadas_hipotesis_por_pregunta():
    led = _ledger()
    q, _ = led.open_question("¿por qué?")
    for i in range(50):
        led.record_hypothesis(f"H{i}", question_id=q.id)

    with pytest.raises(LedgerLimitExceeded):
        led.record_hypothesis("una más", question_id=q.id)

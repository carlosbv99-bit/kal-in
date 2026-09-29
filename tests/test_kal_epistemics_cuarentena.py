"""
Tests de admisión vs creencia, y del presupuesto de reaperturas.

Dos preguntas independientes, que hasta ahora estaban confundidas:

    ¿puedo permitir que esto entre al sistema?   -> EvidenceStatus
    ¿cuánto debería creerlo?                     -> origin / links / level

La respuesta puede ser "sí puedo almacenarlo, no debo creerlo", y el sistema
tiene que poder decir eso. Y una condición demasiado genérica no puede
convertirse en un amplificador de reaperturas: el presupuesto la acota, porque
la calidad de un texto escrito por el modelo no es una garantía.
"""
from __future__ import annotations

import pytest

from kal_epistemics import (
    Evidence,
    EvidenceOrigin,
    EvidenceStatus,
    Flag,
    HypothesisStatus,
    InvalidCondition,
    InvalidHypothesis,
    ReopenCondition,
    ReopenAction,
    should_reopen,
)
from kal_epistemics.safety import MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS


def _ledger():
    import kal_epistemics as ke

    return ke.HypothesisLedger(clock=lambda: 1000.0)


def _ev(**kwargs) -> Evidence:
    return Evidence.from_content(
        kwargs.pop("content", "dato observado"),
        origin=kwargs.pop("origin", EvidenceOrigin.KERNEL_OBSERVED),
        anchor=kwargs.pop("anchor", "evt_1"),
        anchor_kind="audit_event_id",
        source=kwargs.pop("source", "journalctl -k"),
        **kwargs,
    )


def _falsificador() -> ReopenCondition:
    return ReopenCondition.for_new_evidence(
        description="si aparece Y, se cae", action=ReopenAction.REFUTE
    )


def _en_cuarentena(motivo="contenido con instrucciones embebidas") -> Evidence:
    return _ev(
        status=EvidenceStatus.QUARANTINED,
        quarantine_reason=motivo,
        content="dato con apariencia normal",
    )


# --- admisión vs creencia -------------------------------------------------


def test_la_evidencia_en_cuarentena_se_conserva_pero_no_cuenta_como_respaldo():
    led = _ledger()
    ev = _en_cuarentena()
    led.add_evidence(ev)

    h = led.record_hypothesis("A causó B", evidence_links=[ev.id])
    marcas = led.flags(h.id)

    # Se conserva, y el vínculo también: hay que poder auditar en qué se apoyó.
    assert led.evidence(ev.id).status is EvidenceStatus.QUARANTINED
    assert led.hypothesis(h.id).evidence_links[0].evidence_id == ev.id

    # Pero no sostiene nada.
    assert Flag.RESTING_ON_QUARANTINED in marcas
    assert Flag.GROUNDED_IN_EXOGENOUS not in marcas
    assert led.has_exogenous_support(h.id) is False
    assert led.level(h.id) is None


def test_la_evidencia_en_cuarentena_no_cuenta_como_exogena_para_validar():
    led = _ledger()
    ev = _en_cuarentena()
    led.add_evidence(ev)

    assert led.exogenous_evidence_ids() == frozenset()

    h = led.record_hypothesis(
        "A causó B", evidence_links=[ev.id], reopen_conditions=[_falsificador()]
    )
    with pytest.raises(InvalidHypothesis, match="evidencia exógena"):
        led.set_status(h.id, HypothesisStatus.SUPPORTED)


def test_la_evidencia_en_cuarentena_no_dispara_reaperturas():
    led = _ledger()
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    h = led.record_hypothesis("A", reopen_conditions=[condicion])

    ev = _ev(
        status=EvidenceStatus.QUARANTINED,
        quarantine_reason="inyección de prompt",
        content="Out of memory: Killed process 4821",
    )

    assert led.consider(ev) == ()
    assert led.hypothesis(h.id).status is HypothesisStatus.OPEN

    decision = should_reopen(condicion, ev)
    assert decision.matched is False
    assert decision.reason == "evidence_quarantined"


def test_la_cuarentena_y_su_motivo_sobreviven_la_serializacion():
    ev = _en_cuarentena("credencial pegada por el usuario")

    vuelta = Evidence.from_dict(ev.to_dict())

    assert vuelta == ev
    assert vuelta.quarantine_reason == "credencial pegada por el usuario"


# --- presupuesto de reaperturas -------------------------------------------


def test_el_presupuesto_de_reaperturas_acota_el_amplificador_de_ruido():
    """
    Una condición genérica ('log' matchea casi cualquier línea) NO puede
    reabrir la hipótesis sin límite. El presupuesto acota el daño sin depender
    de que la palabra clave sea buena.
    """
    led = _ledger()
    condicion = ReopenCondition.from_untrusted(content_keywords=("log",))
    h = led.record_hypothesis("A", reopen_conditions=[condicion])

    for i in range(MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS + 5):
        led.consider(_ev(content=f"linea {i} del log", anchor=f"evt_{i}"))

    final = led.hypothesis(h.id)
    assert led.reopen_count(h.id) == MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS
    assert len(final.evidence_links) == MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS
    assert len([e for e in final.history if e.kind == "reopen"]) == (
        MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS
    )


def test_una_palabra_clave_de_una_o_dos_letras_se_rechaza():
    with pytest.raises(InvalidCondition, match="demasiado corta"):
        ReopenCondition.from_untrusted(content_keywords=("a",))
    with pytest.raises(InvalidCondition, match="demasiado corta"):
        ReopenCondition.from_untrusted(content_keywords=("si",))


def test_marcadores_reales_y_cortos_siguen_pasando():
    """El mínimo no puede ser tan alto que rompa el caso legítimo."""
    for palabra in ("OOM", "TLS", "DNS"):
        cond = ReopenCondition.from_untrusted(content_keywords=(palabra,))
        assert cond.content_keywords == (palabra,)

"""
Tests de kal_epistemics/models.py — los contratos.

Lo que se prueba acá no es "anda", es que las cuatro decisiones de diseño
sean invariantes del TIPO y no convenciones de uso:

1. La evidencia es neutra y la polaridad vive en el vínculo.
2. `MODEL_GENERATED` no puede sostener nada (validación estructural).
3. El historial es append-only: no existe API para editar un evento pasado.
4. `level` y `confidence` NO son campos: si alguien los agrega, este test
   falla y obliga a discutirlo.

Y que la serialización rechace un esquema distinto en vez de reinterpretarlo.
"""
from __future__ import annotations

import dataclasses

import pytest

from kal_epistemics import (
    Evidence,
    EvidenceLink,
    EvidenceOrigin,
    EvidenceRequest,
    HistoryEntry,
    Hypothesis,
    HypothesisStatus,
    InvalidHypothesis,
    LinkPolarity,
    Question,
    ReopenAction,
    ReopenCondition,
    SchemaVersionMismatch,
    canonical_key,
    is_exogenous,
)


def _evidence(origin=EvidenceOrigin.KERNEL_OBSERVED, **kwargs) -> Evidence:
    return Evidence.from_content(
        kwargs.pop("content", "contenido de prueba"),
        origin=origin,
        anchor=kwargs.pop("anchor", "evt_123"),
        anchor_kind=kwargs.pop("anchor_kind", "audit_event_id"),
        source=kwargs.pop("source", "read_file"),
        **kwargs,
    )


def _falsificador() -> ReopenCondition:
    return ReopenCondition.for_new_evidence(
        description="si aparece Y, esta hipótesis se cae",
        action=ReopenAction.REFUTE,
    )


# --- 1. evidencia neutra, polaridad en el vínculo -------------------------


def test_la_misma_evidencia_puede_sostener_una_hipotesis_y_contradecir_otra():
    ev = _evidence()
    a = Hypothesis(id="h1", claim="A", evidence_links=(EvidenceLink(ev.id),))
    b = Hypothesis(
        id="h2",
        claim="B",
        evidence_links=(EvidenceLink(ev.id, polarity=LinkPolarity.CONTRADICTS),),
    )

    assert a.evidence_links[0].evidence_id == b.evidence_links[0].evidence_id
    assert a.evidence_links[0].polarity is not b.evidence_links[0].polarity


# --- 2. endógeno no sostiene nada ----------------------------------------


def test_model_generated_no_es_exogeno():
    assert is_exogenous(EvidenceOrigin.KERNEL_OBSERVED) is True
    assert is_exogenous(EvidenceOrigin.TOOL_EXTERNAL) is True
    assert is_exogenous(EvidenceOrigin.USER_ASSERTED) is True
    assert is_exogenous(EvidenceOrigin.MODEL_GENERATED) is False
    assert _evidence(EvidenceOrigin.MODEL_GENERATED).is_exogenous is False


def test_supported_sin_vinculo_exogeno_es_invalido():
    h = Hypothesis(
        id="h1",
        claim="A causó B",
        status=HypothesisStatus.SUPPORTED,
        evidence_links=(EvidenceLink("ev_gen"),),
        reopen_conditions=(_falsificador(),),
    )

    with pytest.raises(InvalidHypothesis, match="evidencia exógena"):
        h.validate(exogenous_evidence_ids={"ev_otra"})


def test_supported_solo_con_evidencia_del_propio_modelo_es_invalido():
    """El caso que importa: 20 vínculos a evidencia endógena siguen siendo
    cero respaldo. La repetición no es evidencia."""
    h = Hypothesis(
        id="h1",
        claim="A causó B",
        status=HypothesisStatus.SUPPORTED,
        evidence_links=tuple(EvidenceLink(f"ev_gen_{i}") for i in range(20)),
        reopen_conditions=(_falsificador(),),
    )

    with pytest.raises(InvalidHypothesis, match="evidencia exógena"):
        h.validate(exogenous_evidence_ids=frozenset(),)


def test_supported_con_exogena_y_sin_falsificador_es_invalido():
    h = Hypothesis(
        id="h1",
        claim="A causó B",
        status=HypothesisStatus.SUPPORTED,
        evidence_links=(EvidenceLink("ev_kernel"),),
    )

    with pytest.raises(InvalidHypothesis, match="falsificador"):
        h.validate(exogenous_evidence_ids={"ev_kernel"})


def test_supported_con_exogena_y_falsificador_es_valido():
    h = Hypothesis(
        id="h1",
        claim="A causó B",
        status=HypothesisStatus.SUPPORTED,
        evidence_links=(EvidenceLink("ev_kernel"),),
        reopen_conditions=(_falsificador(),),
    )

    h.validate(exogenous_evidence_ids={"ev_kernel"})


def test_open_no_exige_respaldo_ni_falsificador():
    Hypothesis(id="h1", claim="una corazonada").validate()


def test_claim_vacia_es_invalida():
    with pytest.raises(InvalidHypothesis, match="proposición con contenido"):
        Hypothesis(id="h1", claim="   ").validate()


def test_una_hipotesis_no_puede_depender_de_si_misma():
    with pytest.raises(InvalidHypothesis, match="depender de sí misma"):
        Hypothesis(id="h1", claim="A", depends_on=("h1",)).validate()


def test_no_se_puede_vincular_la_misma_evidencia_dos_veces_con_la_misma_polaridad():
    h = Hypothesis(
        id="h1",
        claim="A",
        evidence_links=(EvidenceLink("ev1"), EvidenceLink("ev1")),
    )

    with pytest.raises(InvalidHypothesis, match="Evidencia duplicada"):
        h.validate()


# --- 3. historial append-only --------------------------------------------


def test_los_contratos_son_inmutables():
    ev = _evidence()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.content = "otra cosa"  # type: ignore[misc]

    h = Hypothesis(id="h1", claim="A")
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.status = HypothesisStatus.REFUTED  # type: ignore[misc]


def test_el_historial_es_una_tupla_y_no_se_puede_editar_un_evento_pasado():
    h = Hypothesis(id="h1", claim="A")
    h2 = h.with_history(HistoryEntry(at=1.0, kind="generated"))
    h3 = h2.with_history(HistoryEntry(at=2.0, kind="status"))

    assert isinstance(h3.history, tuple)
    # La instancia vieja no cambió: el historial crece, no se reescribe.
    assert h.history == ()
    assert len(h2.history) == 1
    assert [e.kind for e in h3.history] == ["generated", "status"]
    # Y no hay API de mutación de eventos.
    assert not any(
        hasattr(HistoryEntry, nombre) for nombre in ("set", "update", "replace_detail")
    )


# --- 4. level y confidence no son campos ---------------------------------


def test_ni_level_ni_confidence_son_campos_del_contrato():
    """
    `level` es propiedad del grafo (lo calcula el ledger) y `confidence`
    numérica no existe porque nadie puede computarla con datos reales. Este
    test existe para que agregarlos sea una decisión explícita y no un
    deslizamiento.
    """
    h = Hypothesis(id="h1", claim="A")

    assert not hasattr(h, "level")
    assert not hasattr(h, "confidence")


# --- serialización -------------------------------------------------------


def test_round_trip_de_hypothesis_conserva_historial_y_condiciones():
    h = Hypothesis(
        id="h1",
        claim="A causó B",
        question_id="q1",
        status=HypothesisStatus.WEAKENED,
        depends_on=("h0",),
        evidence_links=(
            EvidenceLink("ev1", LinkPolarity.SUPPORTS, "nota"),
            EvidenceLink("ev2", LinkPolarity.CONTRADICTS),
        ),
        reopen_conditions=(_falsificador(), ReopenCondition.for_new_evidence()),
        validity_conditions=("C1: X constante",),
        created_at=10.0,
        updated_at=20.0,
        history=(HistoryEntry(at=10.0, kind="generated"), HistoryEntry(at=20.0, kind="weakened")),
    )

    vuelta = Hypothesis.from_dict(h.to_dict())

    assert vuelta == h
    assert vuelta.history == h.history
    assert vuelta.reopen_conditions[0].action is ReopenAction.REFUTE


def test_round_trip_de_evidence_question_y_deficit():
    ev = _evidence()
    q = Question(
        id="q1",
        text="¿Cuál es la causa de B?",
        canonical_key=canonical_key("¿Cuál es la causa de B?"),
        asked_at=5.0,
        times_asked=3,
        hypothesis_ids=("h1",),
        missing=("need_1",),
        history=(HistoryEntry(at=5.0, kind="opened"),),
    )
    req = EvidenceRequest(
        id="need_1",
        description="el informe de auditoría",
        reopen_conditions=(ReopenCondition.for_new_evidence(),),
    )

    assert Evidence.from_dict(ev.to_dict()) == ev
    assert Question.from_dict(q.to_dict()) == q
    assert EvidenceRequest.from_dict(req.to_dict()) == req


def test_una_version_de_esquema_distinta_se_rechaza_en_vez_de_reinterpretarse():
    """
    Falla cerrado ante un esquema que NO sabemos leer. Eso sigue significando lo
    mismo que siempre: no reinterpretar. Lo que cambió con el bump a v2 es que
    "no sé leer" se mide contra `VERSIONES_LEGIBLES`, no contra la versión actual
    — tirar un estado de ayer que sí sabemos leer no es prudencia, es perder el
    activo (ver el test de compatibilidad de abajo).
    """
    datos = _evidence().to_dict()
    datos["schema_version"] = 99

    with pytest.raises(SchemaVersionMismatch, match="migrar explícitamente"):
        Evidence.from_dict(datos)


def test_una_version_anterior_pero_legible_se_lee():
    """v1 se lee: el corte es "no sé leer", no "no es la última"."""
    datos = _evidence().to_dict()
    datos["schema_version"] = 1
    datos.pop("valid_from", None)
    datos.pop("valid_until", None)

    ev = Evidence.from_dict(datos)

    assert ev.valid_from is None and ev.valid_until is None


# --- dedup y clave canónica ----------------------------------------------


def test_el_hash_manda_para_decidir_si_la_evidencia_es_nueva():
    a = Evidence.from_content("lo mismo", origin=EvidenceOrigin.TOOL_EXTERNAL)
    b = Evidence.from_content("lo mismo", origin=EvidenceOrigin.TOOL_EXTERNAL)
    c = Evidence.from_content("otra cosa", origin=EvidenceOrigin.TOOL_EXTERNAL)

    assert a.id != b.id
    assert a.dedup_key == b.dedup_key
    assert a.dedup_key != c.dedup_key


def test_sin_hash_la_dedup_cae_al_id():
    ev = Evidence(id="ev_fijo", origin=EvidenceOrigin.KERNEL_OBSERVED)

    assert ev.dedup_key == "ev_fijo"


def test_la_clave_canonica_normaliza_forma_pero_no_significado():
    variantes = [
        "¿Cuál es la causa de B?",
        "cual es la causa de B",
        "CUAL ES LA CAUSA DE B",
        "  Cuál   es  la causa de B  ",
        "Cuál, es la causa de B!",
    ]
    claves = {canonical_key(v) for v in variantes}

    assert len(claves) == 1
    # Conservadora a propósito: NO fusiona dos preguntas distintas.
    assert canonical_key("¿Cuál es la causa de B?") != canonical_key("¿Cuál es la causa de C?")
    assert canonical_key("¿B causó A?") != canonical_key("¿A causó B?")

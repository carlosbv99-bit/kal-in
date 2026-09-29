"""
Tests de kal_epistemics/conditions.py — el matcher determinista.

La propiedad que se prueba es la que hace que todo el diseño funcione:
**decidir si una hipótesis se reabre no requiere llamar al modelo**. Todo lo
que sigue es determinista y por lo tanto testeable de forma exhaustiva.

El test más importante es el enumerativo: para TODOS los orígenes posibles,
`MODEL_GENERATED` no dispara una reapertura — ni siquiera con una condición
que lo declare explícitamente entre los orígenes aceptados. La regla es
estructural, no un default configurable.
"""
from __future__ import annotations

import pytest

from kal_epistemics import Evidence, EvidenceOrigin, ReopenAction, ReopenCondition, should_reopen
from kal_epistemics.conditions import MAX_CONTENT_SCAN


def _ev(origin=EvidenceOrigin.KERNEL_OBSERVED, **kwargs) -> Evidence:
    return Evidence(
        id=kwargs.pop("id", "ev1"),
        origin=origin,
        anchor=kwargs.pop("anchor", "evt_1"),
        anchor_kind=kwargs.pop("anchor_kind", "audit_event_id"),
        source=kwargs.pop("source", "read_file"),
        content=kwargs.pop("content", ""),
        content_hash=kwargs.pop("content_hash", ""),
    )


CONDICION_PERMISIVA = ReopenCondition(
    id="rc_todos",
    origins=tuple(EvidenceOrigin),  # incluye MODEL_GENERATED a propósito
)


@pytest.mark.parametrize("origen", list(EvidenceOrigin))
def test_regla_estructural_el_texto_del_propio_modelo_nunca_reabre_nada(origen):
    decision = should_reopen(CONDICION_PERMISIVA, _ev(origen))

    if origen is EvidenceOrigin.MODEL_GENERATED:
        assert decision.matched is False
        assert decision.reason == "endogenous_never_reopens"
    else:
        assert decision.matched is True


def test_origins_vacio_no_matchea_nada_fail_closed():
    condicion = ReopenCondition(id="rc_vacia")

    decision = should_reopen(condicion, _ev(EvidenceOrigin.KERNEL_OBSERVED))

    assert decision.matched is False
    assert decision.reason == "origin_not_allowed"


def test_un_origen_no_declarado_no_matchea():
    condicion = ReopenCondition(id="rc_solo_kernel", origins=(EvidenceOrigin.KERNEL_OBSERVED,))

    decision = should_reopen(condicion, _ev(EvidenceOrigin.TOOL_EXTERNAL))

    assert decision.matched is False
    assert decision.reason == "origin_not_allowed"


def test_la_evidencia_ya_aplicada_no_es_evidencia_nueva():
    condicion = ReopenCondition.for_new_evidence()
    ev = _ev(EvidenceOrigin.KERNEL_OBSERVED, content_hash="hash_a")

    decision = should_reopen(condicion, ev, already_applied={"hash_a"})

    assert decision.matched is False
    assert decision.reason == "evidence_already_applied"


def test_el_hash_es_lo_que_decide_si_es_nueva():
    condicion = ReopenCondition.for_new_evidence()
    ev = _ev(EvidenceOrigin.KERNEL_OBSERVED, content_hash="hash_a")

    assert should_reopen(condicion, ev, already_applied={"hash_b"}).matched is True


def test_anchor_kinds_filtra_por_tipo_de_ancla():
    condicion = ReopenCondition(
        id="rc_audit",
        origins=(EvidenceOrigin.KERNEL_OBSERVED,),
        anchor_kinds=("audit_event_id",),
    )

    assert should_reopen(condicion, _ev(anchor_kind="uri")).matched is False
    assert should_reopen(condicion, _ev(anchor_kind="audit_event_id")).matched is True


def test_source_pattern_solo_matchea_la_fuente_declarada():
    condicion = ReopenCondition.for_new_evidence(source_pattern=r"^wikipedia\.")

    assert should_reopen(condicion, _ev(source="wikipedia.org")).matched is True
    assert should_reopen(condicion, _ev(source="foro.example")).matched is False


def test_content_pattern_matchea_contenido_externo():
    condicion = ReopenCondition.for_new_evidence(content_pattern=r"acta\s+del\s+3/3")

    assert should_reopen(condicion, _ev(content="... el acta del 3/3 dice ...")).matched is True
    assert should_reopen(condicion, _ev(content="no dice nada relevante")).matched is False


def test_el_escaneo_de_contenido_esta_acotado():
    """
    Texto externo no confiable + regex = superficie de ReDoS. El tope acota
    el daño, y el test lo fija para que nadie lo saque sin discutirlo.
    """
    condicion = ReopenCondition.for_new_evidence(content_pattern="MARCADOR")
    lejos = "x" * (MAX_CONTENT_SCAN + 10) + "MARCADOR"

    assert should_reopen(condicion, _ev(content="MARCADOR")).matched is True
    assert should_reopen(condicion, _ev(content=lejos)).matched is False


def test_un_patron_invalido_falla_cerrado():
    condicion = ReopenCondition.for_new_evidence(source_pattern="(sin cerrar")

    decision = should_reopen(condicion, _ev())

    assert decision.matched is False
    assert decision.reason == "invalid_pattern"


@pytest.mark.parametrize("accion", list(ReopenAction))
def test_la_accion_declarada_es_la_que_devuelve(accion):
    condicion = ReopenCondition.for_new_evidence(action=accion)

    decision = should_reopen(condicion, _ev())

    assert decision.matched is True
    assert decision.action is accion


def test_es_determinista():
    condicion = ReopenCondition.for_new_evidence(source_pattern="x", content_pattern="y")
    ev = _ev(content="y")

    assert should_reopen(condicion, ev) == should_reopen(condicion, ev)

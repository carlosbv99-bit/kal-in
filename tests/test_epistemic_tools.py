"""
Tests de las tools epistémicas (`open_question`, `record_hypothesis`).

Lo que se prueba acá no es que registren bien, es que la FIRMA SEA LA FRONTERA:
que no exista ningún parámetro por donde el modelo pueda declarar un origen,
vincular evidencia o ponerse en estado respaldado. Si algún día alguien agrega
un `**kwargs` "por comodidad", estos tests tienen que fallar.
"""
from __future__ import annotations

import pytest

from agent_core.epistemics import EpistemicStore
from kal_epistemics import HypothesisStatus
from tool_integration.adapters.epistemic_tools import (
    OpenQuestionTool,
    RecordHypothesisTool,
)


def _tools() -> tuple[EpistemicStore, OpenQuestionTool, RecordHypothesisTool]:
    store = EpistemicStore(clock=lambda: 1000.0)
    return store, OpenQuestionTool(store, session_id="s"), RecordHypothesisTool(store, session_id="s")


def _texto(artifact) -> str:
    return artifact.metadata["summary"]


# --- la firma es la frontera ---------------------------------------------


def test_las_tools_no_aceptan_origen_ni_evidencia_por_firma():
    _, abrir, registrar = _tools()

    with pytest.raises(TypeError):
        abrir.execute(question="x", origin="kernel_observed")
    with pytest.raises(TypeError):
        abrir.execute(question="x", evidence=["ev1"])
    with pytest.raises(TypeError):
        registrar.execute(claim="x", origin="kernel_observed")
    with pytest.raises(TypeError):
        registrar.execute(claim="x", evidence_links=["ev1"])


def test_ninguna_tool_expone_origen_evidencia_ni_estado_en_su_schema():
    _, abrir, registrar = _tools()
    prohibidos = {"origin", "evidence", "evidence_links", "trusted", "status", "supported"}

    for tool in (abrir, registrar):
        props = set(tool.manifest.parameters_schema["properties"])
        assert not (props & prohibidos), f"{tool.manifest.name} expone {props & prohibidos}"


def test_las_tools_estan_registradas_como_del_sistema():
    _, abrir, registrar = _tools()

    assert abrir.manifest.created_by == "system"
    assert registrar.manifest.created_by == "system"


# --- abrir pregunta -------------------------------------------------------


def test_open_question_registra_y_dice_que_queda_sin_resolver():
    store, abrir, _ = _tools()

    artifact = abrir.execute(question="¿Por qué se cayó a las 3:14?", missing=["el log del kernel"])

    assert artifact.modality == "text"
    assert "SIN RESOLVER" in _texto(artifact)
    assert "el log del kernel" in _texto(artifact)
    assert store.ledger("s").question_by_key("¿Por qué se cayó a las 3:14?") is not None


def test_la_misma_pregunta_reformulada_no_crea_otro_registro():
    store, abrir, _ = _tools()

    abrir.execute(question="¿Por qué se cayó?")
    artifact = abrir.execute(question="por que se cayo")

    assert "ya existía" in _texto(artifact)
    assert len(store.ledger("s").question_by_key("¿Por qué se cayó?").hypothesis_ids) == 0


# --- registrar hipótesis --------------------------------------------------


def test_record_hypothesis_nace_en_open_y_sin_respaldo():
    store, _, registrar = _tools()

    artifact = registrar.execute(claim="Se cayó por falta de memoria")

    assert "SIN respaldo" in _texto(artifact)
    hipotesis = store.ledger("s").hypotheses()[0]
    assert hipotesis.status is HypothesisStatus.OPEN
    assert store.ledger("s").level(hipotesis.id) is None


def test_las_condiciones_se_declaran_con_marcadores_no_con_regex():
    store, _, registrar = _tools()

    registrar.execute(claim="A", watch_for=["OOM"], refuted_by=["rollback"])

    hipotesis = store.ledger("s").hypotheses()[0]
    assert [rc.content_keywords for rc in hipotesis.reopen_conditions] == [("OOM",), ("rollback",)]
    assert all(rc.content_pattern == "" for rc in hipotesis.reopen_conditions)
    assert all(rc.source_pattern == "" for rc in hipotesis.reopen_conditions)


def test_una_palabra_clave_corta_vuelve_como_error_al_modelo_no_como_excepcion():
    _, _, registrar = _tools()

    artifact = registrar.execute(claim="A", watch_for=["a"])

    assert _texto(artifact).startswith("ERROR:")
    assert "3 letras" in _texto(artifact)


def test_depender_de_algo_inexistente_vuelve_como_error():
    _, _, registrar = _tools()

    artifact = registrar.execute(claim="B", depends_on=["h_inventada"])

    assert _texto(artifact).startswith("ERROR:")


def test_una_proposicion_demasiado_larga_vuelve_como_error():
    _, _, registrar = _tools()

    artifact = registrar.execute(claim="x" * 5000)

    assert _texto(artifact).startswith("ERROR:")


# --- el ciclo completo, a través de las tools -----------------------------


def test_el_ciclo_completo_abrir_registrar_y_reabrir_solo():
    store, abrir, registrar = _tools()

    abrir.execute(question="¿Por qué se cayó a las 3:14?", missing=["el log del kernel"])
    pregunta = store.ledger("s").question_by_key("¿Por qué se cayó a las 3:14?")
    registrar.execute(
        claim="Se cayó por OOM",
        question_id=pregunta.id,
        watch_for=["Out of memory"],
    )
    hipotesis = store.ledger("s").hypotheses()[0]

    # Aparece la observación, sin que el modelo intervenga en el vínculo.
    store.ingest_tool_result(
        session_id="s",
        tool_name="run_code",
        observation="kernel: Out of memory: Killed process 4821",
    )

    final = store.ledger("s").hypothesis(hipotesis.id)
    assert final.status is HypothesisStatus.REACTIVATED
    assert store.ledger("s").level(hipotesis.id) == 0
    # Y el expediente abierto la muestra con evidencia, no como conjetura suelta.
    assert "nivel=0" in store.context_block("s")


# --- D9: la deuda se declara con su condición de pago ---------------------
#
# Hallazgo de F6, cerrado: `open_question` no adjuntaba ninguna condición al
# déficit, así que `unsatisfied_deficits()` no lo soltaba nunca y el gate
# bloqueaba respuestas que ya tenían la evidencia delante. El mecanismo para
# saldarlo (`reopen_conditions` en `EvidenceRequest`) ya existía y estaba
# probado; lo que faltaba era el cableado, que es lo que estos tests fijan.


def test_open_question_sin_resolved_by_avisa_que_la_deuda_no_se_salda():
    _, abrir, _ = _tools()

    resumen = _texto(abrir.execute(question="¿Por qué se cayó?", missing=["el log"]))

    assert "No declaraste con qué se salda" in resumen


def test_open_question_con_resolved_by_salda_la_deuda_cuando_llega_la_evidencia():
    """
    El camino REAL del modelo, de punta a punta: la tool declara la deuda con su
    condición, una observación ingerida por el camino real la salda, y el gate
    deja de ver el déficit pendiente.
    """
    store, abrir, _ = _tools()
    resumen = _texto(
        abrir.execute(
            question="¿Por qué se cayó el servicio?",
            missing=["el log del kernel"],
            resolved_by=["Out of memory"],
        )
    )
    assert "La deuda se salda sola si aparece evidencia con: Out of memory" in resumen
    assert store.unsatisfied_deficits("s") == ("el log del kernel",)

    store.ingest_tool_result(
        session_id="s",
        tool_name="run_code",
        observation="kernel: Out of memory: Killed process 4821",
    )

    assert store.unsatisfied_deficits("s") == ()


def test_open_question_con_un_marcador_muy_corto_devuelve_error():
    """El camino no confiable sigue validando: un marcador de 2 letras se rechaza
    (y no rompe la tarea, vuelve como texto de error)."""
    _, abrir, _ = _tools()

    resumen = _texto(abrir.execute(question="¿?", missing=["x"], resolved_by=["OM"]))

    assert resumen.startswith("ERROR:")
    assert "al menos 3 letras" in resumen


def test_resolved_by_es_un_marcador_no_un_patron():
    """
    La frontera de seguridad se mantiene: `resolved_by` entra como palabra clave
    (substring, sin motor de regex), así que un valor con pinta de patrón
    catastrófico es literal y no puede colgar al matcher.
    """
    store, abrir, _ = _tools()
    abrir.execute(question="¿?", missing=["x"], resolved_by=["(a+)+$"])

    condicion = store.ledger("s").request(store.ledger("s").all_questions()[0].missing[0]).reopen_conditions[0]

    assert condicion.content_keywords == ("(a+)+$",)
    assert condicion.content_pattern == ""
    assert condicion.source_pattern == ""

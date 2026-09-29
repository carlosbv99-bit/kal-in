"""
Tests de kal_epistemics/ledger.py — el registro y sus invariantes.

Los tres tests que más importan de todo el paquete están acá:

1. `test_el_ledger_no_es_un_backend_de_memoria_y_no_expone_recall` — la
   garantía anti-contaminación. Una hipótesis plausible recuperada por
   similaridad y sin marca de origen es una alucinación persistente con
   procedencia, que además el sistema cita. Si algún día alguien mete el
   ledger en el camino de `recall()`, este test lo tiene que atrapar.
2. `test_repetir_la_misma_evidencia_no_la_refuerza` — la repetición no es
   evidencia, y no hay ninguna API para expresar "más confianza por
   insistir".
3. `test_endogeno_no_reabre_ni_con_condicion_permisiva` — el texto del propio
   modelo no dispara reaperturas ni siquiera cuando la condición lo permite
   explícitamente.

Los dos primeros son invariantes del sistema; el tercero es la regla sin la
cual todo lo demás se convierte en un bucle de auto-confirmación.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import kal_epistemics as ke
from kal_epistemics import (
    DuplicateId,
    Evidence,
    EvidenceOrigin,
    Flag,
    HypothesisStatus,
    InvalidHypothesis,
    LinkPolarity,
    ReopenAction,
    ReopenCondition,
    UnknownId,
)


def _ledger() -> ke.HypothesisLedger:
    return ke.HypothesisLedger(clock=lambda: 1000.0)


def _ev(origin=EvidenceOrigin.KERNEL_OBSERVED, **kwargs) -> Evidence:
    """Evidencia de prueba. `content_hash` permite forzar un hash distinto al
    del contenido, que es como se simula 'el mismo dato otra vez'."""
    content_hash = kwargs.pop("content_hash", None)
    ev = Evidence.from_content(
        kwargs.pop("content", "dato observado"),
        origin=origin,
        anchor=kwargs.pop("anchor", "evt_1"),
        anchor_kind=kwargs.pop("anchor_kind", "audit_event_id"),
        source=kwargs.pop("source", "read_file"),
        **kwargs,
    )
    return ev if content_hash is None else replace(ev, content_hash=content_hash)


def _falsificador(descripcion="si aparece Y, se cae") -> ReopenCondition:
    return ReopenCondition.for_new_evidence(description=descripcion, action=ReopenAction.REFUTE)


# --- evidencia: la procedencia no se inventa -----------------------------


def test_no_se_puede_pisar_una_evidencia_con_contenido_distinto():
    led = _ledger()
    led.add_evidence(Evidence(id="ev1", origin=EvidenceOrigin.KERNEL_OBSERVED, content="a"))

    with pytest.raises(DuplicateId):
        led.add_evidence(Evidence(id="ev1", origin=EvidenceOrigin.KERNEL_OBSERVED, content="b"))


def test_no_se_puede_vincular_evidencia_que_no_esta_registrada():
    led = _ledger()
    h = led.record_hypothesis("A causó B")

    with pytest.raises(UnknownId, match="No hay evidencia registrada"):
        led.link_evidence(h.id, "ev_inventada")


def test_no_se_puede_depender_de_una_hipotesis_que_no_existe():
    led = _ledger()

    with pytest.raises(UnknownId, match="no existe en el ledger"):
        led.record_hypothesis("conclusión", depends_on=["h_inventada"])


# --- el que registra no cierra -------------------------------------------


def test_registrar_una_hipotesis_siempre_la_deja_en_open():
    led = _ledger()
    ev = _ev()
    led.add_evidence(ev)

    h = led.record_hypothesis(
        "A causó B",
        evidence_links=[ev.id],
        reopen_conditions=[_falsificador()],
    )

    assert h.status is HypothesisStatus.OPEN


def test_supported_exige_exogena_y_falsificador():
    led = _ledger()
    exogena = _ev(EvidenceOrigin.KERNEL_OBSERVED)
    endogena = _ev(EvidenceOrigin.MODEL_GENERATED)
    led.add_evidence(exogena)
    led.add_evidence(endogena)

    solo_endogena = led.record_hypothesis(
        "A", evidence_links=[endogena.id], reopen_conditions=[_falsificador()]
    )
    with pytest.raises(InvalidHypothesis, match="evidencia exógena"):
        led.set_status(solo_endogena.id, HypothesisStatus.SUPPORTED)

    sin_falsificador = led.record_hypothesis("B", evidence_links=[exogena.id])
    with pytest.raises(InvalidHypothesis, match="falsificador"):
        led.set_status(sin_falsificador.id, HypothesisStatus.SUPPORTED)

    completa = led.record_hypothesis(
        "C", evidence_links=[exogena.id], reopen_conditions=[_falsificador()]
    )
    led.set_status(completa.id, HypothesisStatus.SUPPORTED)
    assert led.hypothesis(completa.id).status is HypothesisStatus.SUPPORTED


# --- preguntas: dedup ----------------------------------------------------


def test_la_misma_pregunta_reformulada_no_crea_un_registro_nuevo():
    led = _ledger()

    q1, creada1 = led.open_question("¿Cuál es la causa de B?")
    q2, creada2 = led.open_question("cual es la causa de B")

    assert creada1 is True
    assert creada2 is False
    assert q1.id == q2.id
    assert q2.times_asked == 2
    assert [e.kind for e in q2.history] == ["opened", "re_asked"]


def test_dos_preguntas_distintas_no_se_fusionan():
    led = _ledger()

    q1, _ = led.open_question("¿A causó B?")
    q2, _ = led.open_question("¿B causó A?")

    assert q1.id != q2.id


# --- grafo: level derivado ---------------------------------------------


def test_el_nivel_se_calcula_y_no_se_declara():
    led = _ledger()
    ev = _ev()
    led.add_evidence(ev)

    h0 = led.record_hypothesis("premisa observada", evidence_links=[ev.id])
    h1 = led.record_hypothesis("inferencia", depends_on=[h0.id])
    h2 = led.record_hypothesis("especulación", depends_on=[h1.id])

    assert led.level(h0.id) == 0
    assert led.level(h1.id) == 1
    assert led.level(h2.id) == 2


def test_una_isla_no_tiene_nivel_y_se_marca_unrooted():
    led = _ledger()
    h = led.record_hypothesis("una corazonada suelta")

    assert led.level(h.id) is None
    assert Flag.UNROOTED in led.flags(h.id)


def test_depender_de_algo_sin_raiz_no_da_nivel():
    led = _ledger()
    isla = led.record_hypothesis("isla")
    colgada = led.record_hypothesis("colgada", depends_on=[isla.id])

    assert led.level(colgada.id) is None


def test_una_cadena_de_pura_especulacion_acumula_nivel():
    led = _ledger()
    endogena = _ev(EvidenceOrigin.MODEL_GENERATED)
    led.add_evidence(endogena)

    h1 = led.record_hypothesis("H1", evidence_links=[endogena.id])
    h2 = led.record_hypothesis("H2", depends_on=[h1.id])
    h3 = led.record_hypothesis("H3", depends_on=[h2.id])
    h4 = led.record_hypothesis("H4", depends_on=[h3.id])

    assert led.level(h1.id) is None
    assert led.level(h4.id) is None
    assert Flag.ENDOGENOUS_ONLY in led.flags(h1.id)


def test_un_ciclo_inyectado_a_mano_no_cuelga_y_se_marca_circular():
    """
    Blanco-legítimo y a propósito: la API pública hace IMPOSIBLE crear un
    ciclo, porque una dependencia tiene que existir antes que su dependiente
    (el grafo es un DAG por construcción). Este test es defensa en
    profundidad: si un ciclo entra por otro camino — una carga desde disco en
    F4, por ejemplo — `level()` no se cuelga y lo marca.
    """
    led = _ledger()
    a = led.record_hypothesis("A")
    b = led.record_hypothesis("B", depends_on=[a.id])
    led._hypotheses[a.id] = replace(led._hypotheses[a.id], depends_on=(b.id,))

    assert led.level(a.id) is None
    assert Flag.CIRCULAR in led.flags(a.id)
    assert Flag.CIRCULAR in led.flags(b.id)


# --- flags ---------------------------------------------------------------


def test_evidencia_exogena_en_contra_marca_contradicted():
    led = _ledger()
    ev = _ev()
    led.add_evidence(ev)
    h = led.record_hypothesis("A")
    led.link_evidence(h.id, ev.id, polarity=LinkPolarity.CONTRADICTS)

    marcas = led.flags(h.id)

    assert Flag.CONTRADICTED in marcas
    assert Flag.GROUNDED_IN_EXOGENOUS not in marcas


def test_evidencia_sin_ancla_marca_procedencia_no_verificable():
    led = _ledger()
    ev = _ev(anchor="")
    led.add_evidence(ev)
    h = led.record_hypothesis("A", evidence_links=[ev.id])

    assert Flag.UNVERIFIABLE_PROVENANCE in led.flags(h.id)


def test_no_declarar_falsificador_se_ve():
    led = _ledger()
    h = led.record_hypothesis("A")

    assert Flag.NO_FALSIFIER in led.flags(h.id)


# --- invalidación y propagación -----------------------------------------


def test_invalidar_una_premisa_suspende_a_quien_colgaba_solo_de_ella():
    led = _ledger()
    premisa = led.record_hypothesis("P1: Y fue la causa")
    conclusion = led.record_hypothesis("H2: por lo tanto Z", depends_on=[premisa.id])

    reporte = led.invalidate(premisa.id, reason="P1 no estaba respaldada")

    assert reporte.suspended == (conclusion.id,)
    assert led.hypothesis(conclusion.id).status is HypothesisStatus.SUSPENDED
    assert Flag.DEPENDENCY_REFUTED in led.flags(conclusion.id)


def test_un_descendiente_con_respaldo_propio_se_marca_pero_no_se_suspende():
    """
    La distinción que evita el arrasamiento: si el descendiente tiene
    evidencia exógena PROPIA, su sostén no dependía de la premisa caída.
    Suspenderlo tiraría conclusiones que seguían siendo válidas.
    """
    led = _ledger()
    ev = _ev()
    led.add_evidence(ev)
    premisa = led.record_hypothesis("P1")
    conclusion = led.record_hypothesis(
        "H2 con respaldo propio", depends_on=[premisa.id], evidence_links=[ev.id]
    )

    reporte = led.invalidate(premisa.id)

    assert reporte.flagged == (conclusion.id,)
    assert reporte.suspended == ()
    assert led.hypothesis(conclusion.id).status is HypothesisStatus.OPEN
    assert Flag.DEPENDENCY_REFUTED in led.flags(conclusion.id)


def test_invalidar_propaga_por_todo_el_arbol():
    led = _ledger()
    h1 = led.record_hypothesis("H1")
    h2 = led.record_hypothesis("H2", depends_on=[h1.id])
    h3 = led.record_hypothesis("H3", depends_on=[h2.id])

    reporte = led.invalidate(h1.id)

    assert set(reporte.suspended) == {h2.id, h3.id}
    assert led.descendants(h1.id) == frozenset({h2.id, h3.id})
    assert led.ancestors(h3.id) == frozenset({h1.id, h2.id})


# --- reapertura ---------------------------------------------------------


def test_una_hipotesis_guardada_se_reabre_cuando_llega_la_evidencia_que_faltaba():
    led = _ledger()
    condicion = ReopenCondition.for_new_evidence(description="si aparece el informe")
    h = led.record_hypothesis("A podría causar B", reopen_conditions=[condicion])

    nueva = _ev(EvidenceOrigin.TOOL_EXTERNAL, source="fetch", content_hash="hash_informe")
    decisiones = led.consider(nueva)

    assert len(decisiones) == 1
    assert decisiones[0].action is ReopenAction.REOPEN
    assert led.hypothesis(h.id).status is HypothesisStatus.REACTIVATED
    assert led.level(h.id) == 0


def test_endogeno_no_reabre_ni_con_condicion_permisiva():
    led = _ledger()
    permisiva = ReopenCondition(id="rc_todos", origins=tuple(EvidenceOrigin))
    h = led.record_hypothesis("A podría causar B", reopen_conditions=[permisiva])

    decisiones = led.consider(_ev(EvidenceOrigin.MODEL_GENERATED, content_hash="hash_gen"))

    assert decisiones == ()
    assert led.hypothesis(h.id).status is HypothesisStatus.OPEN


def test_repetir_la_misma_evidencia_no_la_refuerza():
    """
    Mismo contenido (mismo hash) tres veces = la misma evidencia una vez. No
    hay ningún estado en el ledger que exprese "más respaldo por insistir",
    y ese es el punto: un sistema que confunde frecuencia con respaldo se
    auto-convence en tiempo récord.
    """
    led = _ledger()
    condicion = ReopenCondition.for_new_evidence(description="si aparece el dato")
    h = led.record_hypothesis("A", reopen_conditions=[condicion])

    for _ in range(3):
        led.consider(
            Evidence.from_content(
                "el dato",
                origin=EvidenceOrigin.KERNEL_OBSERVED,
                anchor="evt_1",
                anchor_kind="audit_event_id",
            )
        )

    final = led.hypothesis(h.id)
    assert final.status is HypothesisStatus.REACTIVATED
    assert len(final.evidence_links) == 1
    assert len([e for e in final.history if e.kind == "reopen"]) == 1


def test_una_condicion_de_refutacion_refuta_y_deja_evidencia_en_contra():
    led = _ledger()
    h = led.record_hypothesis("A causó B", reopen_conditions=[_falsificador()])

    led.consider(_ev(EvidenceOrigin.KERNEL_OBSERVED, content_hash="hash_y"))

    final = led.hypothesis(h.id)
    assert final.status is HypothesisStatus.REFUTED
    assert Flag.CONTRADICTED in led.flags(h.id)
    assert final.evidence_links[0].polarity is LinkPolarity.CONTRADICTS


def test_una_condicion_de_debilitamiento_debilia_sin_refutar():
    led = _ledger()
    condicion = ReopenCondition.for_new_evidence(
        description="si aparece un indicio en contra", action=ReopenAction.WEAKEN
    )
    h = led.record_hypothesis("A", reopen_conditions=[condicion])

    led.consider(_ev(EvidenceOrigin.TOOL_EXTERNAL, content_hash="hash_indicio"))

    assert led.hypothesis(h.id).status is HypothesisStatus.WEAKENED


def test_una_evidencia_que_no_matchea_no_genera_decisiones():
    led = _ledger()
    condicion = ReopenCondition.for_new_evidence(source_pattern="^wikipedia\.")
    led.record_hypothesis("A", reopen_conditions=[condicion])

    assert led.consider(_ev(source="foro.example")) == ()


def test_consider_es_determinista_en_el_orden_de_las_decisiones():
    led = _ledger()
    condicion = ReopenCondition.for_new_evidence(description="si aparece algo")
    led.record_hypothesis("A", reopen_conditions=[condicion])
    led.record_hypothesis("B", reopen_conditions=[condicion])

    primera = [d.reason for d in led.consider(_ev(content_hash="hash_1"))]
    segunda = [d.reason for d in led.consider(_ev(content_hash="hash_2"))]

    assert primera == segunda == ["matched", "matched"]


def test_la_evidencia_que_llega_satisface_el_deficit_declarado():
    """
    Cierra el ciclo del diseño original: la pregunta sin resolver guarda QUÉ
    falta (E7); cuando E7 llega, la pregunta deja de estar sin resolver y el
    déficit sale de la lista de faltantes.
    """
    led = _ledger()
    q, _ = led.open_question("¿Cuál es la causa de B?")
    condicion = ReopenCondition.for_new_evidence(
        description="si aparece el acta del 3/3", content_pattern="acta del 3/3"
    )
    deficit = led.add_evidence_request(q.id, "el acta del 3/3", reopen_conditions=[condicion])

    assert led.missing_descriptions(q.id) == ("el acta del 3/3",)

    led.consider(_ev(EvidenceOrigin.TOOL_EXTERNAL, content="... acta del 3/3 ..."))

    assert led.request(deficit.id).is_satisfied is True
    assert led.missing_descriptions(q.id) == ()


# --- la garantía anti-contaminación -------------------------------------


def test_el_ledger_no_es_un_backend_de_memoria_y_no_expone_recall():
    """
    El test más importante del paquete. Si las hipótesis entraran al camino
    de recuperación de conocimiento del agente, el sistema pasaría a citar
    sus propias conjeturas como si fueran datos — y encima con procedencia,
    que las hace más creíbles. Las hipótesis se guardan para el futuro, pero
    NO se recuerdan como conocimiento: son dos caminos distintos, con dos
    APIs distintas.
    """
    from agent_core.memory.base import MemoryBackend

    led = _ledger()

    assert not isinstance(led, MemoryBackend)
    for nombre in ("recall", "retrieve", "store", "remember", "forget", "list_all", "query"):
        assert not hasattr(led, nombre), (
            f"HypothesisLedger expone '{nombre}(): parece haberse colado en el camino "
            "de recuperación de conocimiento del agente."
        )


# --- el resultado no depende del orden de las condiciones ------------------
#
# HALLAZGO DE F6 (2026-09-29): una observación que disparaba DOS acciones
# distintas sobre la misma hipótesis (p. ej. reapertura y refutación) aplicaba
# solo la primera y descartaba la segunda en silencio, así que el estado final
# dependía del orden en que se declararon las condiciones — y el que ganaba era
# el más optimista. Los dos problemas estaban apilados: el dedup era por
# evidencia (no por condición) y `_aplicar()` pisaba el estado sin ninguna regla
# de precedencia. Los tests de abajo fallan si se arregla solo uno de los dos.


def _evidencia_con_los_dos_marcadores() -> Evidence:
    """Un solo dato que dice las dos cosas: sostiene y refuta."""
    return _ev(
        content="el log muestra Out of memory y además el deploy de las 03:12",
        content_hash="hash_doble",
    )


def _condicion_de_reapertura() -> ReopenCondition:
    return ReopenCondition.from_untrusted(
        id="rc_reabre",
        description="observación que menciona 'Out of memory'",
        content_keywords=("Out of memory",),
        action=ReopenAction.REOPEN,
    )


def _condicion_de_refutacion() -> ReopenCondition:
    return ReopenCondition.from_untrusted(
        id="rc_refuta",
        description="observación que menciona 'deploy'",
        content_keywords=("deploy",),
        action=ReopenAction.REFUTE,
    )


@pytest.mark.parametrize(
    "orden",
    [
        (_condicion_de_reapertura(), _condicion_de_refutacion()),
        (_condicion_de_refutacion(), _condicion_de_reapertura()),
    ],
    ids=["reapertura_primero", "refutacion_primero"],
)
def test_una_evidencia_que_dice_dos_cosas_da_el_mismo_estado_en_cualquier_orden(orden):
    """
    La MISMA evidencia, con las condiciones declaradas en los dos órdenes
    posibles, tiene que terminar igual: los dos vínculos, el estado más cauto y
    `Flag.CONTRADICTED`. Es el test que cierra el hallazgo de F6.
    """
    led = _ledger()
    h = led.record_hypothesis("El servicio se cayó por OOM", reopen_conditions=orden)

    decisiones = led.consider(_evidencia_con_los_dos_marcadores())

    final = led.hypothesis(h.id)
    assert final.status is HypothesisStatus.REFUTED  # gana la lectura cauta
    assert len(decisiones) == 2  # las dos condiciones se aplicaron
    assert {(l.polarity, l.evidence_id) for l in final.evidence_links} == {
        (LinkPolarity.SUPPORTS, decisiones[0].evidence_id),
        (LinkPolarity.CONTRADICTS, decisiones[0].evidence_id),
    }
    assert Flag.CONTRADICTED in led.flags(h.id)
    assert led.reopen_count(h.id) == 1, (
        "el presupuesto se consume por evidencia, no por condición: el ruido que "
        "acota es el dato, no cuántas lecturas de ese dato se declararon"
    )


def test_el_historial_deja_ver_que_hubo_precedencia():
    """
    Dos acciones en el historial y un solo estado sin explicación harían que la
    regla de precedencia pareciera arbitraria en una auditoría.
    """
    led = _ledger()
    h = led.record_hypothesis(
        "El servicio se cayó por OOM",
        reopen_conditions=[_condicion_de_reapertura(), _condicion_de_refutacion()],
    )

    led.consider(_evidencia_con_los_dos_marcadores())

    kinds = [e.kind for e in led.hypothesis(h.id).history]
    assert kinds.count("reopen") == 1
    assert kinds.count("refute") == 1
    assert kinds.count("precedence") == 1


def test_la_precedencia_es_refutar_sobre_debilitar_sobre_reabrir():
    """
    Fail-closed un nivel más abajo: ante la misma evidencia, el estado más cauto.
    Se prueban los tres pares que importan.
    """
    pares = [
        (ReopenAction.REFUTE, ReopenAction.WEAKEN, HypothesisStatus.REFUTED),
        (ReopenAction.REFUTE, ReopenAction.REOPEN, HypothesisStatus.REFUTED),
        (ReopenAction.WEAKEN, ReopenAction.REOPEN, HypothesisStatus.WEAKENED),
    ]
    for fuerte, debil, esperado in pares:
        for orden in ((fuerte, debil), (debil, fuerte)):
            led = _ledger()
            condiciones = [
                ReopenCondition.from_untrusted(
                    id=f"rc_{accion.value}",
                    content_keywords=("el dato",),
                    action=accion,
                )
                for accion in orden
            ]
            h = led.record_hypothesis("A causó B", reopen_conditions=condiciones)

            led.consider(_ev(content="el dato", content_hash="hash_mismo"))

            assert led.hypothesis(h.id).status is esperado, (
                f"{fuerte.value} vs {debil.value} en el orden "
                f"{[a.value for a in orden]}: se esperaba {esperado.value}"
            )


def test_la_misma_condicion_sigue_dedupeando_la_evidencia_repetida():
    """
    El arreglo del dedup no puede romper el invariante de F1: para la MISMA
    condición, la misma evidencia sigue contando una sola vez.
    """
    led = _ledger()
    condicion = _condicion_de_reapertura()
    h = led.record_hypothesis("A", reopen_conditions=[condicion])

    for _ in range(3):
        led.consider(_ev(content="Out of memory", content_hash="hash_repetido"))

    final = led.hypothesis(h.id)
    assert final.status is HypothesisStatus.REACTIVATED
    assert len(final.evidence_links) == 1
    assert led.reopen_count(h.id) == 1
    assert led.applied_evidence(h.id) == frozenset({"hash_repetido"})


def test_dos_condiciones_distintas_ven_la_misma_evidencia_cada_una_una_vez():
    """Si la evidencia se repite, cada condición la ve una vez: ninguna se
    descarta por culpa de la otra, y ninguna se aplica dos veces."""
    led = _ledger()
    h = led.record_hypothesis(
        "A",
        reopen_conditions=[_condicion_de_reapertura(), _condicion_de_refutacion()],
    )

    for _ in range(3):
        led.consider(_evidencia_con_los_dos_marcadores())

    assert led.reopen_count(h.id) == 1  # la segunda y la tercera son la misma evidencia
    assert len(led.hypothesis(h.id).evidence_links) == 2  # un sostén y una contradicción


# --- D9: la deuda con condición de pago -----------------------------------


def test_open_question_puede_declarar_con_que_se_salda_el_deficit():
    """
    Sin condiciones, un déficit no se salda NUNCA (el ledger no adivina qué
    evidencia lo cubre). Con la condición de pago, el déficit deja de estar
    pendiente cuando llega la evidencia — que es lo que evita que R1 quede
    enclavado bloqueando respuestas correctas.
    """
    led = _ledger()
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))

    q, _ = led.open_question(
        "¿Por qué se cayó el servicio?",
        missing=["el log del kernel"],
        reopen_conditions=[condicion],
    )

    assert led.missing_descriptions(q.id) == ("el log del kernel",)

    led.consider(_ev(content="kernel: Out of memory: Killed 4821", content_hash="hash_oom"))

    assert led.missing_descriptions(q.id) == ()
    assert led.request(q.missing[0]).is_satisfied is True


def test_open_question_sin_condiciones_deja_el_deficit_pendiente_para_siempre():
    """
    El reverso, y la razón por la que la tool ahora acepta `resolved_by`: sin
    condición de pago, la misma evidencia no salda nada. Queda como test para que
    el comportamiento no se "arregle" por accidente con semántica adivinada.
    """
    led = _ledger()
    q, _ = led.open_question("¿Por qué se cayó el servicio?", missing=["el log del kernel"])

    led.consider(_ev(content="kernel: Out of memory: Killed 4821", content_hash="hash_oom"))

    assert led.missing_descriptions(q.id) == ("el log del kernel",)

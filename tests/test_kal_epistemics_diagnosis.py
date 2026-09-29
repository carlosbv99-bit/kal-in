"""
Mejora A y B (2026-09-29): monitores introspectivos y trayectoria.

De dónde salen: de una revisión de literatura que encontró dos ideas ya
existentes en investigación —los monitores sobre las creencias propias (Herd,
Kelly y Stolle, "Temporal Monitoring of Agent Beliefs Under Uncertainty with
Subjective LTL", AAAI-SS 2026) y la trayectoria de una creencia en vez de su
estado puntual—. Se trajo la pregunta, no el formalismo: acá no hay grados de
creencia ni números que nadie pueda computar.

Lo que estos tests tienen que probar, y en este orden:

1. **Cada invariante dispara.** Un monitor que nunca se vio fallar es un monitor
   que no se sabe si funciona. Cada uno tiene un estado hostil que lo enciende, y
   ese estado llega por el LOADER REAL (`from_snapshot`) para probar que pasa la
   validación estructural y que igual es absurdo: es exactamente el hueco que los
   monitores tapan.
2. **No hay falsos positivos.** Un invariant no verificable (historial
   compactado) NO vuelve insano al ledger: confundir "no lo sé" con "está roto"
   es el error que esta capa existe para no cometer.
3. **No duplican reglas.** Lo que ya se valida en la escritura no se re-chequea
   acá; hay un test que fija esa división.
"""
from __future__ import annotations

import pytest

from kal_epistemics import (
    INCONSISTENCIA,
    NO_VERIFICABLE,
    Evidence,
    EvidenceOrigin,
    HypothesisLedger,
    HypothesisStatus,
    LedgerDurable,
    LinkPolarity,
    PresupuestoDeCompactacion,
    ReopenCondition,
    diagnosticar,
)
from kal_epistemics.safety import (
    MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS,
    MAX_REOPEN_APPLICATIONS_PER_QUESTION,
)


def _ledger() -> HypothesisLedger:
    return HypothesisLedger(clock=lambda: 1000.0)


def _con_reapertura(*, con_ancla: bool = True):
    """Un ledger con una reapertura real: base para casi todos los tests."""
    led = _ledger()
    h = led.record_hypothesis(
        "el servicio se cayó por el deploy",
        reopen_conditions=[ReopenCondition.from_untrusted(content_keywords=("deploy",))],
    )
    led.consider(
        Evidence.from_content(
            "el deploy de las 03:12 rompió el esquema",
            origin=EvidenceOrigin.KERNEL_OBSERVED,
            source="run_code",
            anchor="a" * 64 if con_ancla else "",
            anchor_kind="audit_event_hash" if con_ancla else "",
        )
    )
    return led, h


def _recargar(led: HypothesisLedger, **cambios) -> HypothesisLedger:
    """Pasa por el LOADER REAL con el snapshot alterado: si esto no levantara,
    el estado pasó la validación estructural y los monitores son lo único que
    puede verlo."""
    snap = led.snapshot()
    snap.update(cambios)
    return HypothesisLedger.from_snapshot(snap, clock=lambda: 1000.0)


def _terminales_con_historial_largo(cuantas: int = 4):
    """
    Para que el compactador planifique algo hacen falta DOS condiciones: la
    hipótesis tiene que ser terminal (una viva no se compacta nunca) y tiene que
    haber excedente sobre el presupuesto por pregunta (por defecto conserva 3
    terminales). De ahí las cuatro.
    """
    led = _ledger()
    q, _ = led.open_question("¿por qué se cayó?")
    ids = []
    for i in range(cuantas):
        h = led.record_hypothesis(f"candidata {i}", question_id=q.id)
        for vuelta in range(3):
            led.set_status(h.id, HypothesisStatus.OPEN, reason=f"vuelta {vuelta}")
            led.set_status(h.id, HypothesisStatus.REFUTED, reason=f"refutación {vuelta}")
        ids.append(h.id)
    return led, q, ids


def _invariantes(diagnostico) -> set[str]:
    return {h.invariante for h in diagnostico.hallazgos}


# --------------------------------------------------------------------------
# 1. Cada invariante dispara
# --------------------------------------------------------------------------


def test_un_ledger_sano_verifica_los_cinco_invariantes():
    led, _ = _con_reapertura()

    d = diagnosticar(led)

    assert d.sano is True
    assert d.hallazgos == ()
    assert set(d.verificados) == {
        "reaperturas_vs_historial",
        "estado_declarado",
        "aplicado_coherente",
        "deficit_saldado",
        "techo_de_reaperturas",
    }


def test_el_contador_de_reaperturas_no_puede_mentir_sobre_el_historial():
    """
    El contador es el que acota el ruido (R8) y el que decide el dedup. Un
    snapshot que dice 7 reaperturas sin historial que las explique agota un
    presupuesto que nadie gastó: la hipótesis no se va a poder reabrir nunca más.
    """
    led, h = _con_reapertura()

    roto = _recargar(led, reopen_count=[(h.id, 7)])
    d = diagnosticar(roto)

    assert d.sano is False
    assert "reaperturas_vs_historial" in _invariantes(d)
    assert any(x.severidad == INCONSISTENCIA for x in d.hallazgos)


def test_reaperturas_contadas_sin_clave_de_dedup():
    """
    Contador y dedup se escriben juntos. Si el contador sube y el dedup no, la
    MISMA evidencia vuelve a contar: es el bug D10 reintroducido por un estado
    que llegó de afuera.
    """
    led, h = _con_reapertura()

    roto = _recargar(led, applied=[])
    d = diagnosticar(roto)

    assert "reaperturas_sin_dedup" in _invariantes(d)


def test_dedup_sin_reaperturas_contadas():
    """
    La inversa: claves de dedup con el contador en cero suprimen una aplicación
    que nunca se contó — el presupuesto se gasta sin registrarse.
    """
    led, _ = _con_reapertura()

    roto = _recargar(led, reopen_count=[])
    d = diagnosticar(roto)

    assert "dedup_sin_reaperturas" in _invariantes(d)


def test_el_estado_declarado_tiene_que_ser_el_que_la_historia_produce():
    """
    Un snapshot puede ser estructuralmente impecable y mentir sobre el estado: el
    campo dice `open`, el historial termina en una refutación. El campo es el que
    el gate y el expediente leen, así que la mentira se propaga.
    """
    led = _ledger()
    h = led.record_hypothesis("algo pasó")
    led.set_status(h.id, HypothesisStatus.REFUTED, reason="la evidencia lo desmintió")

    roto = _recargar(led, hypotheses=[dict(led.snapshot()["hypotheses"][0], status="open")])
    d = diagnosticar(roto)

    assert "estado_declarado" in _invariantes(d)
    assert any("refuted" in x.detalle for x in d.hallazgos)


def test_una_clave_de_dedup_sin_evidencia():
    """La clave huérfana suprime para siempre la aplicación de un dato que ya no
    existe: la hipótesis queda sorda a esa evidencia."""
    led, h = _con_reapertura()

    roto = _recargar(led, applied=[[h.id, "cond_x", "clave_que_no_existe"]])
    d = diagnosticar(roto)

    assert "aplicado_sin_evidencia" in _invariantes(d)


def test_una_clave_de_dedup_sin_vinculo():
    """
    Evidencia que existe y no está vinculada a la hipótesis, pero figura como
    aplicada: se suprimió una aplicación sin que nada la respalde.
    """
    led, h = _con_reapertura()
    suelta = led.add_evidence(
        Evidence.from_content("otra observación", origin=EvidenceOrigin.KERNEL_OBSERVED)
    )

    roto = _recargar(led, applied=[[h.id, "cond_x", suelta.dedup_key]])
    d = diagnosticar(roto)

    assert "aplicado_sin_vinculo" in _invariantes(d)


def test_una_clave_de_dedup_sin_historial():
    """
    Con el historial completo, una aplicación tiene que estar registrada. Un
    vínculo suelto (`evidence_linked`, que no es una aplicación) no alcanza: el
    dedup se apoya en una aplicación que no ocurrió.
    """
    led = _ledger()
    h = led.record_hypothesis("algo pasó")
    ev = led.add_evidence(
        Evidence.from_content("observación", origin=EvidenceOrigin.KERNEL_OBSERVED)
    )
    led.link_evidence(h.id, ev.id, polarity=LinkPolarity.SUPPORTS)

    roto = _recargar(led, applied=[[h.id, "cond_x", ev.dedup_key]])
    d = diagnosticar(roto)

    assert "aplicado_sin_historial" in _invariantes(d)


def test_un_deficit_saldado_apoyado_en_evidencia_inexistente():
    led = _ledger()
    q, _ = led.open_question("¿por qué se cayó?", missing=["el log"])
    assert q.missing, "el déficit tiene que existir para poder saldarlo mal"

    roto = _recargar(led, applied_requests=[[q.missing[0], "clave_falsa"]])
    d = diagnosticar(roto)

    assert "deficit_saldado_sin_evidencia" in _invariantes(d)


def test_los_techos_de_reapertura_no_se_pueden_inflar():
    """
    Los techos (R8) se midieron para acotar ruido. Un contador por encima es un
    presupuesto agotado por un estado que no pasó por el camino que lo consume.
    """
    led = _ledger()
    h = led.record_hypothesis("algo pasó")

    roto = _recargar(led, reopen_count=[(h.id, MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS + 1)])

    assert "techo_de_reaperturas" in _invariantes(diagnosticar(roto))


def test_el_techo_por_pregunta_tambien_se_chequea():
    """El techo de la PREGUNTA es la suma de sus hipótesis: cuatro hipótesis con
    8 reaperturas cada una pasan el techo por hipótesis y rompen el de pregunta."""
    led = _ledger()
    q, _ = led.open_question("¿por qué se cayó?")
    ids = [
        led.record_hypothesis(f"hipótesis {i}", question_id=q.id).id for i in range(5)
    ]

    roto = _recargar(led, reopen_count=[(hid, 8) for hid in ids])

    d = diagnosticar(roto)

    assert "techo_de_reaperturas" in _invariantes(d)
    assert any(x.sujeto == q.id for x in d.hallazgos)
    assert sum(8 for _ in ids) > MAX_REOPEN_APPLICATIONS_PER_QUESTION


def test_el_monitor_no_depende_del_orden_de_las_condiciones():
    """
    BUG REAL encontrado escribiendo estos tests, del mismo tipo que D10: cuando
    la misma evidencia dispara dos condiciones, el ledger deja UNA ENTRADA POR
    LECTURA y el estado lo decide la precedencia. Una primera versión de este
    módulo leía las entradas como transiciones secuenciales, así que con las
    condiciones declaradas en el orden inverso derivaba `reactivated` donde el
    ledger dice `refuted` y reportaba una inconsistencia que no existía.

    O sea: el monitor acusaba al ledger de un problema que era del monitor. Acá
    se fija que las dos declaraciones dan el mismo resultado.
    """
    def construir(invertido: bool):
        condiciones = [
            ReopenCondition.from_untrusted(content_keywords=("rollback",)),
            ReopenCondition.from_untrusted(
                content_keywords=("deploy sin rollback",), action=_refutar()
            ),
        ]
        led = _ledger()
        h = led.record_hypothesis(
            "el servicio se cayó por el deploy",
            reopen_conditions=tuple(reversed(condiciones)) if invertido else tuple(condiciones),
        )
        led.consider(
            Evidence.from_content(
                "deploy sin rollback aplicado", origin=EvidenceOrigin.KERNEL_OBSERVED
            )
        )
        return led, h

    normal, h1 = construir(invertido=False)
    inverso, h2 = construir(invertido=True)

    assert normal.hypothesis(h1.id).status is inverso.hypothesis(h2.id).status
    assert diagnosticar(normal).sano is True
    assert diagnosticar(inverso).sano is True
    # `generated` es la primera transición (nace `open`), después la refutación.
    assert _estados_de(normal, h1.id) == _estados_de(inverso, h2.id) == ["open", "refuted"]


def _estados_de(led, hid) -> list[str]:
    return [
        t.hacia for t in led.trayectoria_de(hid).transiciones if t.cambia_estado
    ]


# --------------------------------------------------------------------------
# 2. Sin falsos positivos: "no verificable" no es "roto"
# --------------------------------------------------------------------------


def test_un_historial_podado_no_se_reporta_como_inconsistencia():
    """
    Después de compactar, el detalle viejo no está: los invariantes que dependen
    del historial completo pasan a "no verificable" con su motivo, y el ledger
    sigue siendo sano. Confundir "no lo sé" con "está roto" es el error que esta
    capa existe para no cometer.
    """
    led, _, _ = _terminales_con_historial_largo()
    reporte = led.compact(PresupuestoDeCompactacion(max_entradas_de_historial=1))

    d = diagnosticar(led)

    assert reporte.hubo_cambios
    compactada = reporte.hipotesis_compactadas[0]
    assert led.hypothesis(compactada).history[-1].kind == "compacted"
    assert d.sano is True, [x.detalle for x in d.hallazgos]
    assert "reaperturas_vs_historial" in d.no_verificables
    assert any(x.severidad == NO_VERIFICABLE for x in d.hallazgos)


def test_un_historial_sin_entradas_de_estado_no_se_adivina():
    """Si el historial no alcanza para derivar el estado, el monitor dice que no
    se sabe en vez de inventar un estado y comparar contra él."""
    led = _ledger()
    h = led.record_hypothesis("algo pasó")

    roto = _recargar(led, hypotheses=[dict(led.snapshot()["hypotheses"][0], history=[])])
    d = diagnosticar(roto)

    assert d.sano is True
    assert "estado_declarado" in d.no_verificables
    assert any(x.severidad == NO_VERIFICABLE and x.sujeto == h.id for x in d.hallazgos)


# --------------------------------------------------------------------------
# 3. No duplican las reglas de escritura
# --------------------------------------------------------------------------


def test_lo_que_la_escritura_ya_prohibe_no_lo_re_chequea_el_monitor():
    """
    `validate()` impide llegar a SUPPORTED sin respaldo exógeno y sin
    falsificador. Esa regla tiene un solo dueño (la escritura); los monitores
    miran lo que la escritura NO mira — que la historia y los contadores sean
    coherentes. Este test fija la división: la regla sigue viva en la escritura.
    """
    from kal_epistemics import InvalidHypothesis

    led = _ledger()
    h = led.record_hypothesis("algo pasó")  # sin falsificador, sin evidencia

    with pytest.raises(InvalidHypothesis):
        led.set_status(h.id, HypothesisStatus.SUPPORTED)

    # Y el ledger quedó sano: el rechazo no dejó un estado raro.
    assert diagnosticar(led).sano is True


def test_el_diagnostico_no_muta_nada():
    led, h = _con_reapertura()
    antes = led.snapshot()

    diagnosticar(led)

    assert led.snapshot() == antes


# --------------------------------------------------------------------------
# Visibilidad en la carga (mejora A, lado durable y lado host)
# --------------------------------------------------------------------------


def test_el_ledger_durable_diagnostica_lo_que_carga():
    from kal_epistemics import AlmacenEnMemoria

    almacen = AlmacenEnMemoria()
    d = LedgerDurable(almacen=almacen)
    h = d.ledger.record_hypothesis("algo pasó")
    d.guardar()

    snap = dict(almacen.cargar("instalacion"))
    snap["reopen_count"] = [(h.id, 7)]
    almacen.guardar("instalacion", snap)

    reabierto = LedgerDurable(almacen=almacen)

    assert reabierto.ledger.hypotheses(), "reporta, no rechaza: el estado se carga igual"
    assert reabierto.diagnostico_en_la_carga.sano is False
    assert "reaperturas_vs_historial" in _invariantes(reabierto.diagnostico_en_la_carga)


def test_el_host_reporta_la_carga_insana(caplog):
    from agent_core.epistemics import EpistemicStore
    from kal_epistemics import AlmacenEnMemoria

    almacen = AlmacenEnMemoria()
    primero = EpistemicStore(almacen=almacen)
    h = primero.ledger("s").record_hypothesis("algo pasó")
    primero.guardar()
    snap = dict(almacen.cargar("instalacion"))
    snap["reopen_count"] = [(h.id, 7)]
    almacen.guardar("instalacion", snap)

    with caplog.at_level("WARNING"):
        segundo = EpistemicStore(almacen=almacen)

    assert segundo.diagnostico_de_carga.sano is False
    assert any("inconsistencias" in r.getMessage() for r in caplog.records)
    # Y en vivo se puede volver a preguntar.
    assert segundo.diagnosticar("s").sano is False


# --------------------------------------------------------------------------
# B. Trayectoria
# --------------------------------------------------------------------------


def test_la_trayectoria_muestra_lo_que_un_estado_no_puede():
    """
    El caso que motiva la mejora: una hipótesis refutada y REABIERTA. Su estado
    actual dice `reactivated`; la trayectoria dice que volvió de una refutación y
    por qué. Sin eso, la hipótesis parece más firme de lo que es.
    """
    led = _ledger()
    h = led.record_hypothesis(
        "el servicio se cayó por el deploy",
        reopen_conditions=[
            ReopenCondition.from_untrusted(content_keywords=("rollback",)),
            ReopenCondition.from_untrusted(
                content_keywords=("deploy sin rollback",), action=_refutar()
            ),
        ],
    )
    led.consider(
        Evidence.from_content(
            "deploy sin rollback aplicado", origin=EvidenceOrigin.KERNEL_OBSERVED
        )
    )
    led.consider(
        Evidence.from_content(
            "rollback completado con éxito", origin=EvidenceOrigin.TOOL_EXTERNAL
        )
    )

    t = led.trayectoria_de(h.id)

    cambios = [x for x in t.transiciones if x.cambia_estado]
    assert [(x.desde, x.hacia) for x in cambios] == [
        (None, "open"),
        ("open", "refuted"),
        ("refuted", "reactivated"),
    ]
    assert t.estado == "reactivated"
    assert t.historial_podado is False
    # La primera evidencia disparó DOS condiciones ("rollback" y "deploy sin
    # rollback"): el estado lo decidió la precedencia (REFUTE) y la transición
    # conserva las dos lecturas en vez de mostrarlas como dos pasos.
    assert cambios[1].lecturas == ("refute", "reopen")
    assert "gana refute" in cambios[1].motivo
    assert cambios[1].evidencia.startswith("ev_")
    assert cambios[1].condicion.startswith("rc_")


def _refutar():
    from kal_epistemics import ReopenAction

    return ReopenAction.REFUTE


def test_la_trayectoria_marca_que_falta_historial():
    """Una trayectoria podada no puede leerse como si fuera completa."""
    led, _, _ = _terminales_con_historial_largo()
    reporte = led.compact(PresupuestoDeCompactacion(max_entradas_de_historial=1))
    compactada = reporte.hipotesis_compactadas[0]
    antes = len(led.hypothesis(compactada).history)

    t = led.trayectoria_de(compactada)

    assert t.historial_podado is True
    assert t.entradas_omitidas > 0
    assert len(t.transiciones) < antes


def test_la_trayectoria_se_puede_serializar():
    led, h = _con_reapertura()

    datos = led.trayectoria_de(h.id).to_dict()

    assert datos["hipotesis_id"] == h.id
    assert datos["estado"] == "reactivated"
    assert datos["nivel"] == 0
    assert "grounded_in_exogenous" in datos["flags"]
    assert datos["transiciones"][-1]["cambia_estado"] is True

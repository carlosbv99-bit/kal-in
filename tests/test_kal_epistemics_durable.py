"""
Tests del estado durable: `snapshot()` / `from_snapshot()` y `compact()`.

Es la parte de F4 que NO depende de dónde se guarda (decisión D4, todavía
abierta): el estado serializable y la política de qué se tira. El backend va a
escribir y leer esto, así que lo que se prueba acá es el contrato del que
depende cualquier backend.

Los cuatro criterios de aceptación de F4 que se pueden verificar sin backend
están acá, con el nombre del roadmap:

- **"Promover/compactar no pierde `missing[]`"** → `test_compactar_no_pierde_ningun_deficit`
- **"Una hipótesis nunca se promueve a conocimiento por antigüedad, por
  repetición, ni por haber sobrevivido sin ser refutada"** →
  `test_compactar_no_promueve_nada_y_no_cambia_ningun_estado`
- **La misma evidencia repetida no reabre después de recargar** (R8: el bucle de
  reaperturas) → `test_recargar_no_reabre_lo_ya_reabierto`
- **Un estado guardado inconsistente no se reinterpreta** → el bloque de
  fail-closed.

El test de no contaminación (`el ledger no es un MemoryBackend`) ya existe en
`test_kal_epistemics_ledger.py` y se aplica igual al ledger durable: el estado
serializado no gana ninguna API de recuperación por el hecho de persistirse.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

import kal_epistemics as ke
from kal_epistemics import (
    DuplicateId,
    Evidence,
    EvidenceOrigin,
    Flag,
    HypothesisStatus,
    LinkPolarity,
    PresupuestoDeCompactacion,
    ReopenAction,
    ReopenCondition,
    SchemaVersionMismatch,
    UnknownId,
    conserva_lo_irrenunciable,
)


def _ledger() -> ke.HypothesisLedger:
    return ke.HypothesisLedger(clock=lambda: 1000.0)


def _ev(contenido="dato observado", **kwargs) -> Evidence:
    return Evidence.from_content(
        contenido,
        origin=kwargs.pop("origin", EvidenceOrigin.KERNEL_OBSERVED),
        anchor=kwargs.pop("anchor", "evt_1"),
        anchor_kind=kwargs.pop("anchor_kind", "audit_event_hash"),
        source=kwargs.pop("source", "run_code"),
        **kwargs,
    )


def _condicion(clave="Out of memory", accion=ReopenAction.REOPEN) -> ReopenCondition:
    return ReopenCondition.from_untrusted(
        description=f"observación que menciona '{clave}'",
        content_keywords=(clave,),
        action=accion,
    )


def _ledger_poblado() -> ke.HypothesisLedger:
    """Un ledger con todo lo que un snapshot tiene que preservar: pregunta con
    déficit pendiente y déficit saldado, hipótesis reabierta, refutada y sin
    respaldo, dependencias, evidencia repetida y evidencia en cuarentena."""
    led = _ledger()
    pregunta, _ = led.open_question("¿Por qué se cayó el servicio?", missing=["el log del kernel"])
    otra, _ = led.open_question("¿Cuánta memoria libre hay?", missing=["la salida de free -m"])

    # Déficit que se salda por evidencia.
    condicion_deficit = _condicion("MemAvailable")
    led.add_evidence_request(otra.id, "la medición del nodo", reopen_conditions=[condicion_deficit])

    base = led.record_hypothesis(
        "El servicio se cayó por falta de memoria",
        question_id=pregunta.id,
        reopen_conditions=[_condicion("Out of memory"), _condicion("deploy", ReopenAction.REFUTE)],
    )
    derivada = led.record_hypothesis("El reinicio automático tardó más de lo previsto", depends_on=[base.id])

    evidencia = _ev("kernel: Out of memory: Killed process 4821")
    led.consider(evidencia)
    led.consider(evidencia)  # repetida: no suma
    led.consider(_ev("deploy 2026.09.29-3 aplicado a las 03:12"))
    led.consider(_ev("MemAvailable: 9123456 kB", source="run_code"))

    # Evidencia en cuarentena: se conserva, se vincula, y marca.
    en_cuarentena = replace(
        _ev("panel externo: memoria estable", source="browser"),
        status=ke.EvidenceStatus.QUARANTINED,
        quarantine_reason="origen no verificado",
    )
    led.add_evidence(en_cuarentena)
    led.link_evidence(derivada.id, en_cuarentena.id, polarity=LinkPolarity.CONTRADICTS)

    led.record_hypothesis("El disco también estaba lleno", question_id=pregunta.id)
    return led


# --------------------------------------------------------------------------
# Snapshot / restore
# --------------------------------------------------------------------------


def test_el_snapshot_es_completo_y_determinista():
    led = _ledger_poblado()

    primero = led.snapshot()
    segundo = led.snapshot()

    assert primero == segundo
    assert json.dumps(primero, sort_keys=True) == json.dumps(segundo, sort_keys=True)
    assert set(primero) == {
        "schema_version",
        "evidence",
        "hypotheses",
        "questions",
        "requests",
        "applied",
        "applied_requests",
        "reopen_count",
    }
    # El estado no-oficial viaja: sin esto, recargar perdería el dedup de
    # evidencia aplicada y el presupuesto de reaperturas consumido.
    assert primero["applied"] and primero["reopen_count"]


def test_recargar_reconstruye_el_mismo_estado_y_el_mismo_hash():
    led = _ledger_poblado()
    snapshot = led.snapshot()

    recargado = ke.HypothesisLedger.from_snapshot(snapshot, clock=lambda: 1000.0)

    assert recargado.snapshot() == snapshot
    assert json.dumps(recargado.snapshot(), sort_keys=True) == json.dumps(snapshot, sort_keys=True)


def test_el_estado_recargado_se_comporta_igual():
    """
    Lo que importa no es que el diccionario sea igual, sino que el ledger
    reconstruido DECIDA igual. Un round-trip que preserva bytes y pierde
    comportamiento no serviría para nada.
    """
    led = _ledger_poblado()
    recargado = ke.HypothesisLedger.from_snapshot(led.snapshot(), clock=lambda: 1000.0)

    for original in led.hypotheses():
        copia = recargado.hypothesis(original.id)
        assert copia == original
        assert recargado.level(original.id) == led.level(original.id)
        assert recargado.flags(original.id) == led.flags(original.id)
        assert recargado.reopen_count(original.id) == led.reopen_count(original.id)
        assert recargado.applied_evidence(original.id) == led.applied_evidence(original.id)
    for original in led.all_questions():
        copia = recargado.question(original.id)
        assert copia == original
        assert recargado.missing_descriptions(original.id) == led.missing_descriptions(original.id)


def test_recargar_no_reabre_lo_ya_reabierto():
    """
    R8 (bucles de reapertura): si el dedup de evidencia aplicada no viajara en el
    snapshot, recargar y volver a ingerir la MISMA observación reabriría otra vez
    la hipótesis — el mismo dato contaría dos veces solo porque hubo un reinicio.
    """
    led = _ledger_poblado()
    recargado = ke.HypothesisLedger.from_snapshot(led.snapshot(), clock=lambda: 1000.0)
    hipotesis = [h for h in recargado.hypotheses() if h.claim.startswith("El servicio")][0]
    antes = recargado.reopen_count(hipotesis.id)

    decisiones = recargado.consider(_ev("kernel: Out of memory: Killed process 4821"))

    assert decisiones == ()
    assert recargado.reopen_count(hipotesis.id) == antes


def test_una_hipotesis_refutada_sigue_siendo_reabrible_despues_de_recargar():
    """Las condiciones de reapertura viajan: 'refutada' no es 'cerrada'."""
    led = _ledger_poblado()
    recargado = ke.HypothesisLedger.from_snapshot(led.snapshot(), clock=lambda: 1000.0)
    hipotesis = [h for h in recargado.hypotheses() if h.claim.startswith("El servicio")][0]
    assert recargado.hypothesis(hipotesis.id).status is HypothesisStatus.REFUTED

    recargado.consider(_ev("nuevo indicio de Out of memory en el log rotado"))

    assert recargado.hypothesis(hipotesis.id).status is HypothesisStatus.REACTIVATED


def test_el_snapshot_es_una_copia_y_no_una_ventana_al_estado():
    led = _ledger_poblado()
    snapshot = led.snapshot()
    antes = led.snapshot()

    snapshot["hypotheses"].clear()
    snapshot["reopen_count"].append(["h_inventada", 99])

    assert led.snapshot() == antes


def test_un_ciclo_inyectado_a_mano_sobrevive_al_round_trip_sin_colgarse():
    """
    Los ciclos son inconstruibles por la API, pero al cargar desde disco el DAG
    ya no está garantizado por el orden de construcción (lo anotó F1). `level()`
    tiene que devolver `None` y marcar `CIRCULAR`, no colgarse — ni antes ni
    después del round-trip.
    """
    led = _ledger()
    a = led.record_hypothesis("A")
    b = led.record_hypothesis("B", depends_on=[a.id])
    # Se inyecta el ciclo por el atributo privado, a propósito.
    led._hypotheses[a.id] = replace(led.hypothesis(a.id), depends_on=(b.id,))

    recargado = ke.HypothesisLedger.from_snapshot(led.snapshot(), clock=lambda: 1000.0)

    for hid in (a.id, b.id):
        assert recargado.level(hid) is None
        assert Flag.CIRCULAR in recargado.flags(hid)


# --- fail-closed: un estado guardado inconsistente NO se reinterpreta ------


def test_otra_version_de_esquema_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["schema_version"] = 99

    with pytest.raises(SchemaVersionMismatch):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_una_hipotesis_que_apunta_a_evidencia_inexistente_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["evidence"] = []

    with pytest.raises(UnknownId):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_una_clave_canonica_que_no_corresponde_al_texto_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["questions"][0]["canonical_key"] = "otra cosa"

    with pytest.raises(SchemaVersionMismatch):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_dos_preguntas_con_la_misma_clave_canonica_levantan():
    snapshot = _ledger_poblado().snapshot()
    snapshot["questions"][1]["canonical_key"] = snapshot["questions"][0]["canonical_key"]
    snapshot["questions"][1]["text"] = snapshot["questions"][0]["text"]

    with pytest.raises(DuplicateId):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_una_hipotesis_que_apunta_a_una_pregunta_inexistente_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["questions"] = []

    with pytest.raises(UnknownId):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_un_deficit_huerfano_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["requests"].append(
        {
            "schema_version": ke.SCHEMA_VERSION,
            "id": "need_huerfano",
            "description": "algo que no pertenece a ninguna pregunta",
            "satisfied_by": "",
            "reopen_conditions": [],
        }
    )

    with pytest.raises(UnknownId):
        ke.HypothesisLedger.from_snapshot(snapshot)


def test_evidencia_aplicada_a_una_hipotesis_inexistente_levanta():
    snapshot = _ledger_poblado().snapshot()
    snapshot["applied"].append(["h_inventada", "rc_inventada", "hash"])

    with pytest.raises(UnknownId):
        ke.HypothesisLedger.from_snapshot(snapshot)


# --------------------------------------------------------------------------
# Compactación
# --------------------------------------------------------------------------


def _ledger_con_muchas_terminales(cantidad: int = 5, *, largas: bool = True) -> ke.HypothesisLedger:
    led = _ledger()
    pregunta, _ = led.open_question("¿Qué pasó con el índice?", missing=["el estado del índice"])
    for i in range(cantidad):
        claim = f"Hipótesis {i} sobre el índice"
        if largas:
            claim += " " + "x" * 400
        h = led.record_hypothesis(claim, question_id=pregunta.id,
                                  reopen_conditions=[_condicion("rebuild")])
        led.set_status(h.id, HypothesisStatus.REFUTED, reason=f"descartada {i}")
    return led


def test_la_compactacion_no_toca_ninguna_hipotesis_viva():
    led = _ledger()
    pregunta, _ = led.open_question("¿Por qué?")
    vivas = [
        led.record_hypothesis("abierta " + "y" * 400, question_id=pregunta.id),
        led.record_hypothesis("otra " + "z" * 400, question_id=pregunta.id),
    ]

    reporte = led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0))

    assert reporte.hipotesis_compactadas == ()
    for h in vivas:
        # Intacta de punta a punta, no solo en el texto: una hipótesis viva es una
        # deuda abierta y no se toca.
        assert led.hypothesis(h.id) == h


def test_compacta_las_terminales_mas_viejas_y_deja_las_recientes():
    led = _ledger_con_muchas_terminales(5)
    pregunta = led.all_questions()[0]
    orden = sorted(led.hypotheses_of(pregunta.id), key=lambda h: (h.created_at, h.id))

    reporte = led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=2))

    assert reporte.hipotesis_compactadas == tuple(h.id for h in orden[:3])
    assert reporte.caracteres_ahorrados > 0
    # Las dos más recientes quedan intactas.
    for h in orden[3:]:
        assert led.hypothesis(h.id) == h


def test_compactar_no_pierde_ningun_deficit():
    """
    Criterio de aceptación textual del roadmap: "Promover/compactar no pierde
    `missing[]`". El déficit es lo único que hay que conservar sí o sí, porque es
    lo que evita reinventar la misma investigación.
    """
    led = _ledger_con_muchas_terminales(5)
    antes = {q.id: led.missing_descriptions(q.id) for q in led.all_questions()}

    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=1))

    assert {q.id: led.missing_descriptions(q.id) for q in led.all_questions()} == antes
    assert any(d for d in antes.values()), "el caso de prueba tiene que tener déficits"


def test_compactar_no_promueve_nada_y_no_cambia_ningun_estado():
    """
    Criterio de aceptación textual: una hipótesis nunca se promueve a
    conocimiento por antigüedad, por repetición, ni por haber sobrevivido sin ser
    refutada. La forma mecánica de verificarlo: ningún estado cambia, no aparece
    evidencia nueva, y no aparece ningún `SUPPORTED`.
    """
    led = _ledger_poblado()
    estados_antes = {h.id: h.status for h in led.hypotheses()}
    evidencia_antes = len(led.exogenous_evidence_ids())

    for _ in range(5):
        led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0))

    assert {h.id: h.status for h in led.hypotheses()} == estados_antes
    assert len(led.exogenous_evidence_ids()) == evidencia_antes
    assert HypothesisStatus.SUPPORTED not in {h.status for h in led.hypotheses()}


def test_compactar_no_toca_nada_estructural():
    led = _ledger_con_muchas_terminales(4)
    antes = {h.id: h for h in led.hypotheses()}

    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=1))

    for h in led.hypotheses():
        assert conserva_lo_irrenunciable(antes[h.id], h), (
            f"'{h.id}': la compactación tocó estado, vínculos, dependencias o condiciones"
        )


def test_las_marcas_derivadas_sobreviven_a_la_compactacion():
    """Si la compactación tocara el grafo, `flags()` cambiaría. No puede."""
    led = _ledger()
    pregunta, _ = led.open_question("¿Se cayó por OOM?")
    h = led.record_hypothesis("Se cayó por OOM " + "x" * 400, question_id=pregunta.id,
                              reopen_conditions=[_condicion("deploy", ReopenAction.REFUTE)])
    led.consider(_ev("deploy aplicado a las 03:12"))
    led.set_status(h.id, HypothesisStatus.REFUTED, reason="refutada")
    flags_antes = led.flags(h.id)
    assert Flag.CONTRADICTED in flags_antes

    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0))

    assert led.flags(h.id) == flags_antes


def test_una_hipotesis_compactada_sigue_siendo_reabrible():
    """
    El criterio que separa "compactar" de "tirar": una refutada compactada tiene
    que poder reabrirse cuando llegue la evidencia. Si la compactación se llevara
    las condiciones, la hipótesis quedaría muerta y el déficit que la justificaba
    sería irrecuperable.
    """
    led = _ledger_con_muchas_terminales(2)
    pregunta = led.all_questions()[0]
    objetivo = sorted(led.hypotheses_of(pregunta.id), key=lambda h: (h.created_at, h.id))[0]
    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0))
    assert len(led.hypothesis(objetivo.id).claim) < len(objetivo.claim)

    led.consider(_ev("el rebuild del índice terminó a las 04:20"))

    assert led.hypothesis(objetivo.id).status is HypothesisStatus.REACTIVATED
    assert any(
        l.evidence_id for l in led.hypothesis(objetivo.id).evidence_links
        if l.polarity is LinkPolarity.SUPPORTS
    )


def test_compactar_es_idempotente():
    """
    Sin esto, cada escritura del store durable dejaría una marca nueva en el
    historial y el archivo crecería igual que sin compactar.
    """
    led = _ledger_con_muchas_terminales(5)
    presupuesto = PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=2)

    primero = led.compact(presupuesto)
    estado = led.snapshot()
    segundo = led.compact(presupuesto)
    tercero = led.compact(presupuesto)

    assert primero.hubo_cambios is True
    assert segundo.hipotesis_compactadas == ()
    assert tercero.hipotesis_compactadas == ()
    assert led.snapshot() == estado


def test_la_marca_de_compactacion_conserva_el_tamano_original():
    """La marca es la auditoría de cuánto se perdió: tiene que decir el tamaño
    ORIGINAL, no el de la última corrida."""
    led = _ledger_con_muchas_terminales(2, largas=True)
    original = sorted(led.hypotheses(), key=lambda h: (h.created_at, h.id))[0]
    largo_original = len(original.claim)

    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0))
    marca = [e for e in led.hypothesis(original.id).history if e.kind == "compacted"]
    assert marca

    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0, max_caracteres_de_claim=50))
    marcas = [e for e in led.hypothesis(original.id).history if e.kind == "compacted"]

    assert len(marcas) == 1, "no se acumulan marcas"
    assert ("claim_chars_originales", str(largo_original)) in marcas[0].detail


def test_el_historial_compactado_respeta_el_presupuesto():
    led = _ledger_con_muchas_terminales(2)
    presupuesto = PresupuestoDeCompactacion(
        max_hipotesis_terminales_por_pregunta=0, max_entradas_de_historial=2
    )

    led.compact(presupuesto)

    for h in led.hypotheses():
        assert len(h.history) <= presupuesto.max_entradas_de_historial


def test_el_estado_compactado_sobrevive_al_round_trip():
    led = _ledger_con_muchas_terminales(4)
    led.compact(PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=1))
    snapshot = led.snapshot()

    recargado = ke.HypothesisLedger.from_snapshot(snapshot, clock=lambda: 1000.0)

    assert recargado.snapshot() == snapshot
    assert recargado.compact(
        PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=1)
    ).hipotesis_compactadas == ()


def test_el_reporte_dice_cuantos_deficits_quedaron_pendientes():
    led = _ledger_con_muchas_terminales(3)

    reporte = led.compact()

    assert reporte.deficits_pendientes_conservados == 1
    assert "el estado del índice" in led.missing_descriptions(led.all_questions()[0].id)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_hipotesis_terminales_por_pregunta": -1},
        {"max_caracteres_de_claim": 0},
        {"max_entradas_de_historial": 0},
    ],
)
def test_un_presupuesto_invalido_levanta(kwargs):
    with pytest.raises(ValueError):
        PresupuestoDeCompactacion(**kwargs)


# --------------------------------------------------------------------------
# El almacén: port, ámbito y dedup entre sesiones
# --------------------------------------------------------------------------


def test_el_almacen_en_memoria_pasa_por_json_y_no_comparte_objetos():
    """
    El doble fiel: lo guardado son DATOS, no referencias al estado vivo. Si el
    ledger y el almacén compartieran las mismas estructuras, un test podría pasar
    acá y fallar con un backend de verdad.
    """
    almacen = ke.AlmacenEnMemoria()
    registro = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    registro.ledger.record_hypothesis("una conjetura")
    snapshot = registro.guardar()

    cargado = almacen.cargar(registro.ambito)
    assert cargado == snapshot
    assert cargado is not snapshot

    cargado["hypotheses"].clear()
    assert almacen.cargar(registro.ambito)["hypotheses"] != []


def test_un_ambito_nuevo_no_es_un_error():
    almacen = ke.AlmacenEnMemoria()

    assert almacen.cargar("nadie") is None
    assert almacen.ambitos() == ()
    assert ke.LedgerDurable(almacen=almacen).ledger.hypotheses() == ()


def test_la_misma_pregunta_en_otra_sesion_no_crea_un_registro_nuevo():
    """
    Criterio de aceptación de F4, el que F6 medía en 0.00: la misma pregunta en
    otra sesión cae en el MISMO registro. Se cumple porque las sesiones del mismo
    ámbito comparten el estado guardado, y `canonical_key` es la clave — y la
    reformulación cosmética también cae en el mismo registro, que es lo que hacía
    falta para que el "no lo sé" sea repetible entre sesiones.
    """
    almacen = ke.AlmacenEnMemoria()

    primera = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    pregunta, creada = primera.ledger.open_question(
        "¿Por qué se cayó el servicio a las 3:14?", missing=["el log del kernel"]
    )
    assert creada is True
    primera.guardar()

    # Otra sesión, mismo ámbito: proceso nuevo, mismo almacén.
    segunda = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    otra_vez, creada_otra_vez = segunda.ledger.open_question(
        "¿por que se cayo el servicio a las 3:14?", missing=["el log del kernel"]
    )

    assert creada_otra_vez is False
    assert otra_vez.id == pregunta.id
    assert otra_vez.times_asked == 2
    assert segunda.ledger.missing_descriptions(otra_vez.id) == ("el log del kernel",)


def test_dos_ambitos_no_se_ven_entre_si():
    """
    El ámbito es lo que hace que "compartir" sea una decisión y no un accidente.
    Si esto fallara, dos usuarios de la misma instalación verían las hipótesis del
    otro — el mismo bug de aislamiento que A-4, con otro nombre.
    """
    almacen = ke.AlmacenEnMemoria()
    ana = ke.LedgerDurable(almacen=almacen, ambito="ana", clock=lambda: 1000.0)
    pregunta_de_ana, _ = ana.ledger.open_question("¿Cuánto gana Marta?")
    ana.ledger.record_hypothesis("Marta cobra 2300", question_id=pregunta_de_ana.id)
    ana.guardar()

    beto = ke.LedgerDurable(almacen=almacen, ambito="beto", clock=lambda: 1000.0)

    assert beto.ledger.all_questions() == ()
    assert beto.ledger.hypotheses() == ()
    propia, creada = beto.ledger.open_question("¿Cuánto gana Marta?")
    assert creada is True
    assert propia.id != pregunta_de_ana.id
    beto.guardar()
    assert sorted(almacen.ambitos()) == ["ana", "beto"]


def test_el_estado_sobrevive_a_un_reinicio_del_proceso():
    """
    Lo que F4 promete: que el "no lo sé" y las hipótesis sobrevivan al proceso.
    Se simula con un objeto nuevo sobre el mismo almacén, que es exactamente lo
    que pasa en un reinicio con un backend de archivo.
    """
    almacen = ke.AlmacenEnMemoria()
    antes = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    pregunta, _ = antes.ledger.open_question("¿Por qué se cayó?", missing=["el log"])
    hipotesis = antes.ledger.record_hypothesis(
        "Se cayó por OOM", question_id=pregunta.id,
        reopen_conditions=[ke.ReopenCondition.from_untrusted(content_keywords=("Out of memory",))],
    )
    antes.ledger.consider(_ev("kernel: Out of memory: Killed 4821"))
    antes.ledger.record_hypothesis("sin respaldo")
    antes.guardar()

    despues = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)

    assert despues.ledger.hypothesis(hipotesis.id).status is HypothesisStatus.REACTIVATED
    assert despues.ledger.hypothesis(hipotesis.id).claim == "Se cayó por OOM"
    assert len(despues.ledger.hypotheses()) == 2
    assert despues.ledger.missing_descriptions(pregunta.id) == ("el log",)
    # Y el comportamiento se preserva: la misma evidencia no vuelve a reabrir.
    assert despues.ledger.consider(_ev("kernel: Out of memory: Killed 4821")) == ()


def test_guardar_si_cambio_no_reescribe_de_gusto():
    almacen = ke.AlmacenEnMemoria()
    registro = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)

    # Un ledger vacío no tiene nada que guardar: no se crea un ámbito por abrir
    # una sesión que no dijo nada.
    assert registro.guardar_si_cambio() is False
    registro.ledger.record_hypothesis("algo nuevo")
    assert registro.guardar_si_cambio() is True
    assert registro.guardar_si_cambio() is False  # nada cambió desde el guardado


def test_compactar_guarda_el_estado_compactado():
    almacen = ke.AlmacenEnMemoria()
    registro = ke.LedgerDurable(
        almacen=almacen,
        clock=lambda: 1000.0,
        presupuesto=ke.PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0),
    )
    pregunta, _ = registro.ledger.open_question("¿Qué pasó con el índice?")
    for i in range(3):
        h = registro.ledger.record_hypothesis(f"Hipótesis {i} " + "x" * 400, question_id=pregunta.id)
        registro.ledger.set_status(h.id, HypothesisStatus.REFUTED, reason="descartada")
    registro.guardar()

    reporte = registro.compactar()

    assert reporte.hubo_cambios is True
    # Lo que quedó en el almacén ya está compactado: si compactar no guardara, el
    # almacén conservaría justo el detalle que se acaba de tirar de memoria.
    recargado = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    conservado = recargado.presupuesto.max_caracteres_de_claim
    assert all(
        len(recargado.ledger.hypothesis(h.id).claim) <= conservado + 1  # +1 por el "…"
        for h in recargado.ledger.hypotheses()
    )
    assert recargado.ledger.compact(
        ke.PresupuestoDeCompactacion(max_hipotesis_terminales_por_pregunta=0)
    ).hubo_cambios is False


def test_recargar_descarta_lo_que_hay_en_memoria():
    almacen = ke.AlmacenEnMemoria()
    registro = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    registro.ledger.record_hypothesis("en memoria")
    registro.guardar()

    otra = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    otra.ledger.record_hypothesis("otra cosa")
    otra.guardar()

    registro.recargar()

    assert {h.claim for h in registro.ledger.hypotheses()} == {"en memoria", "otra cosa"}
    assert len(registro.ledger.hypotheses()) == 2


def test_un_estado_guardado_corrupto_no_se_reinterpreta():
    """El fail-closed del ledger vale igual a través del almacén: es el mismo
    camino."""
    almacen = ke.AlmacenEnMemoria()
    almacen.guardar("instalacion", {"schema_version": 99})

    with pytest.raises(SchemaVersionMismatch):
        ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)


def test_el_almacen_durable_no_es_un_backend_de_memoria():
    """
    El test más importante del proyecto, extendido a la capa durable: persistir
    el ledger no puede convertirlo en una fuente de recuperación de conocimiento.
    Si el almacén (o el registro) expusiera `recall`/`store`, una hipótesis
    guardada podría entrar al contexto como si fuera un dato — con procedencia,
    que es lo que la haría creíble.
    """
    from agent_core.memory.base import MemoryBackend

    almacen = ke.AlmacenEnMemoria()
    registro = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
    prohibidos = ("recall", "retrieve", "store", "remember", "forget", "list_all", "query")

    for objeto in (almacen, registro, registro.ledger):
        assert not isinstance(objeto, MemoryBackend)
        for nombre in prohibidos:
            assert not hasattr(objeto, nombre), (
                f"{type(objeto).__name__} expone '{nombre}(): el ledger durable se está "
                "colando en el camino de recuperación de conocimiento."
            )


def test_el_port_son_cuatro_operaciones_y_nada_mas():
    """
    Un port que crece es un port que ata a un backend. Estas cuatro operaciones
    son lo que el ledger necesita; cualquier otra cosa es de la implementación.
    """
    publicos = {
        nombre
        for nombre in dir(ke.AlmacenDurable)
        if not nombre.startswith("_")
    }

    assert publicos == {"cargar", "guardar", "descartar", "ambitos"}


def test_dos_escritores_del_mismo_ambito_se_pisan_y_esta_declarado():
    """
    LÍMITE CONOCIDO, con test y con nombre: `guardar()` escribe el snapshot
    completo, así que dos objetos vivos sobre el MISMO ámbito se pisan (gana el
    último que guarda).

    Hoy no ocurre en kal-in porque hay un único ledger vivo por ámbito, y por eso
    el port no inventa un protocolo de concurrencia. El día que haya más de un
    proceso escribiendo —o si el backend elegido lo permite— lo tiene que resolver
    el backend: lock de archivo, transacción, o revisión optimista. Este test
    documenta el comportamiento actual para que no se descubra en producción.
    """
    almacen = ke.AlmacenEnMemoria()
    a = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)
    b = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)

    a.ledger.record_hypothesis("de A")
    a.guardar()
    b.ledger.record_hypothesis("de B")
    b.guardar()  # pisa lo de A: B nunca recargó

    final = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)
    assert [h.claim for h in final.ledger.hypotheses()] == ["de B"]


# --------------------------------------------------------------------------
# Compatibilidad de esquema (decisión del 2026-09-29: leer v1 y v2, escribir v2)
# --------------------------------------------------------------------------


def _snapshot_v1(led: ke.HypothesisLedger) -> dict:
    """
    Un snapshot v1 de verdad: el estado actual SIN los campos que en v1 no
    existían, y con la versión en 1. Es exactamente lo que producía el código de
    v1 —`to_dict` no emitía esas claves— y es lo que hay guardado en los almacenes
    de quien corrió la versión anterior.
    """
    datos = json.loads(json.dumps(led.snapshot()))
    datos["schema_version"] = 1
    for crudo in datos.get("evidence", ()):
        crudo.pop("valid_from", None)
        crudo.pop("valid_until", None)
    for crudo in datos.get("hypotheses", ()):
        crudo.pop("excludes", None)
    return datos


def test_un_snapshot_v1_se_lee_y_se_reguarda_como_v2_sin_perder_nada():
    """
    El bump de esquema de C+D1 no puede costar los ledgers guardados. `from_snapshot`
    falla cerrado ante lo que NO sabemos leer (una versión futura), no ante lo que
    sí sabemos leer y es de ayer: tirar el estado ES perder el activo que toda esta
    capa existe para no perder.

    Lo que se verifica, en orden: (1) el v1 real carga, (2) se re-guarda como v2,
    (3) los campos nuevos quedan en su default de v1 (`None` = sin vencimiento
    declarado, `()` = sin exclusiones), y (4) NADA más cambió — el snapshot v2
    recién escrito es idéntico al que produce el código de hoy.
    """
    poblado = _ledger_poblado()
    v1 = _snapshot_v1(poblado)
    esperado_v2 = poblado.snapshot()

    releido = ke.HypothesisLedger.from_snapshot(v1, clock=lambda: 1000.0)
    reguardado = releido.snapshot()

    assert reguardado["schema_version"] == ke.SCHEMA_VERSION == 2
    assert all(e["valid_from"] is None and e["valid_until"] is None for e in reguardado["evidence"])
    assert all(h["excludes"] == [] for h in reguardado["hypotheses"])
    assert reguardado == esperado_v2, "el round-trip v1→v2 perdió o cambió algo"


def test_un_snapshot_v1_llega_al_ledger_durable_y_queda_en_v2():
    """El camino real: el almacén tiene un v1 guardado (lo que dejó la versión
    anterior) y el ledger durable lo abre, lo usa y lo reescribe en v2."""
    poblado = _ledger_poblado()
    almacen = ke.AlmacenEnMemoria()
    almacen.guardar("instalacion", _snapshot_v1(poblado))

    durable = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)

    assert len(durable.ledger.hypotheses()) == len(poblado.hypotheses())
    assert durable.diagnostico_en_la_carga.sano is True

    durable.guardar()

    assert almacen.cargar("instalacion")["schema_version"] == 2


def test_una_version_futura_sigue_rechazandose():
    """La promesa que importa no se tocó: un esquema que no sabemos leer no se
    reinterpreta."""
    datos = _ledger_poblado().snapshot()
    datos["schema_version"] = 3

    with pytest.raises(ke.SchemaVersionMismatch):
        ke.HypothesisLedger.from_snapshot(datos)

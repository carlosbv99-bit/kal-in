"""
Tests del backend SQLite (`kal_epistemics/sqlite_store.py`).

Además de "anda", acá se verifican las tres afirmaciones que decidieron D4 y que
serían falsas si nadie las midiera:

1. **Persiste de verdad**, contra un ARCHIVO (no un dict): una instancia nueva
   sobre la misma ruta ve el mismo estado — que es lo que pasa en un reinicio.
2. **Es inspeccionable con `sqlite3` a secas**, sin la librería: la fila guardada
   es el snapshot en JSON.
3. **La concurrencia que aporta es la que dice aportar**: varios hilos escribiendo
   ámbitos distintos no se corrompen ni se traban, y aun así el
   last-write-wins POR ÁMBITO sigue existiendo (es del contrato del port, no del
   backend). Las dos cosas se testean, porque confundirlas es la forma de creer
   que SQLite resolvió un problema que no resolvió.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import kal_epistemics as ke


def _almacen(tmp_path: Path, nombre: str = "ledger.db") -> ke.AlmacenSQLite:
    return ke.AlmacenSQLite(tmp_path / nombre)


def _poblado(
    almacen: ke.AlmacenSQLite, ambito: str = "instalacion"
) -> tuple[ke.LedgerDurable, object, object]:
    registro = ke.LedgerDurable(almacen=almacen, ambito=ambito, clock=lambda: 1000.0)
    pregunta, _ = registro.ledger.open_question("¿Por qué se cayó el servicio?", missing=["el log"])
    hipotesis = registro.ledger.record_hypothesis(
        "Se cayó por OOM",
        question_id=pregunta.id,
        reopen_conditions=[ke.ReopenCondition.from_untrusted(content_keywords=("Out of memory",))],
    )
    registro.ledger.consider(
        ke.Evidence.from_content(
            "kernel: Out of memory: Killed 4821",
            origin=ke.EvidenceOrigin.KERNEL_OBSERVED,
            anchor="evt_1",
            anchor_kind="audit_event_hash",
        )
    )
    registro.guardar()
    return registro, pregunta, hipotesis


# --- 1. persiste contra un archivo ----------------------------------------


def test_el_estado_sobrevive_a_una_instancia_nueva_sobre_la_misma_ruta(tmp_path):
    almacen = _almacen(tmp_path)
    registro, pregunta, hipotesis = _poblado(almacen)
    almacen.cerrar()

    otro = _almacen(tmp_path)
    try:
        recargado = ke.LedgerDurable(almacen=otro, clock=lambda: 1000.0)

        assert recargado.ledger.hypothesis(hipotesis.id).status is ke.HypothesisStatus.REACTIVATED
        assert recargado.ledger.missing_descriptions(pregunta.id) == ("el log",)
        assert recargado.ledger.snapshot() == registro.ledger.snapshot()
    finally:
        otro.cerrar()


def test_el_archivo_se_crea_con_su_directorio(tmp_path):
    ruta = tmp_path / "anidado" / "epistemics" / "ledger.db"

    almacen = ke.AlmacenSQLite(ruta)
    try:
        almacen.guardar("instalacion", {"schema_version": ke.SCHEMA_VERSION})
        assert ruta.exists()
        assert almacen.ambitos() == ("instalacion",)
    finally:
        almacen.cerrar()


def test_un_ambito_que_no_existe_no_es_un_error(tmp_path):
    with _almacen(tmp_path) as almacen:
        assert almacen.cargar("nadie") is None
        assert almacen.ambitos() == ()
        assert almacen.actualizado_en("nadie") is None


def test_descartar_borra_solo_ese_ambito(tmp_path):
    with _almacen(tmp_path) as almacen:
        almacen.guardar("ana", {"schema_version": ke.SCHEMA_VERSION})
        almacen.guardar("beto", {"schema_version": ke.SCHEMA_VERSION})

        almacen.descartar("ana")

        assert almacen.ambitos() == ("beto",)
        assert almacen.cargar("ana") is None


def test_la_escritura_es_reemplazo_no_mezcla(tmp_path):
    """`guardar` reemplaza el snapshot del ámbito: no acumula ni mergea. Es el
    contrato del port, y el backend no lo cambia."""
    with _almacen(tmp_path) as almacen:
        almacen.guardar("instalacion", {"schema_version": 1, "evidence": ["vieja"]})
        almacen.guardar("instalacion", {"schema_version": 1, "evidence": ["nueva"]})

        assert almacen.cargar("instalacion") == {"schema_version": 1, "evidence": ["nueva"]}


# --- 2. inspeccionable con sqlite3 a secas --------------------------------


def test_el_snapshot_se_puede_leer_con_sqlite3_sin_la_libreria(tmp_path):
    """
    La razón por la que se eligió (b) JSONL no se pierde eligiendo SQLite: la fila
    es el snapshot en JSON y se lee con `sqlite3` crudo, sin importar
    `kal_epistemics`.
    """
    ruta = tmp_path / "ledger.db"
    almacen = ke.AlmacenSQLite(ruta)
    _, pregunta, _ = _poblado(almacen)
    almacen.cerrar()

    conexion = sqlite3.connect(ruta)
    try:
        fila = conexion.execute(
            "SELECT snapshot FROM ledger WHERE ambito = ?", ("instalacion",)
        ).fetchone()
    finally:
        conexion.close()

    assert fila is not None
    snapshot = json.loads(fila[0])
    assert snapshot["schema_version"] == ke.SCHEMA_VERSION
    assert any(q["id"] == pregunta.id for q in snapshot["questions"])


def test_usa_wal(tmp_path):
    """WAL es lo que hace que un lector no bloquee a un escritor; si alguien
    cambia el modo, este test lo dice."""
    with _almacen(tmp_path) as almacen:
        assert almacen.modo_journal() == "wal"


# --- 3. la concurrencia que aporta (y la que no) --------------------------


def test_varios_hilos_escribiendo_ambitos_distintos_no_se_corrompen(tmp_path):
    """
    Con WAL + transacciones, N hilos que escriben ámbitos DISTINTOS no se pisan ni
    se traban. Es la garantía que aporta el backend.
    """
    almacen = _almacen(tmp_path)
    errores: list[BaseException] = []
    cantidad = 8

    def escribir(indice: int) -> None:
        try:
            for vuelta in range(5):
                almacen.guardar(f"ambito-{indice}", {"schema_version": 1, "vuelta": vuelta})
        except BaseException as e:  # noqa: BLE001 — se reporta, no se traga
            errores.append(e)

    hilos = [threading.Thread(target=escribir, args=(i,)) for i in range(cantidad)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    try:
        assert errores == []
        assert almacen.ambitos() == tuple(f"ambito-{i}" for i in range(cantidad))
        assert all(almacen.cargar(f"ambito-{i}")["vuelta"] == 4 for i in range(cantidad))
    finally:
        almacen.cerrar()


def test_el_last_write_wins_por_ambito_sigue_existiendo(tmp_path):
    """
    LO QUE EL BACKEND **NO** RESUELVE, con test y con nombre: dos escritores vivos
    del MISMO ámbito siguen pisándose, porque `guardar()` reemplaza el snapshot
    entero y eso es contrato del port, no del motor.

    SQLite aporta atomicidad (una escritura entra entera o no entra) y evita la
    corrupción y el "database is locked"; no aporta merge. Confundir las dos cosas
    es creer que un problema de diseño quedó resuelto por elegir una biblioteca.
    """
    almacen = _almacen(tmp_path)
    a = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)
    b = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)
    try:
        a.ledger.record_hypothesis("de A")
        a.guardar()
        b.ledger.record_hypothesis("de B")
        b.guardar()  # pisa lo de A: B nunca recargó

        final = ke.LedgerDurable(almacen=almacen, ambito="instalacion", clock=lambda: 1000.0)
        assert [h.claim for h in final.ledger.hypotheses()] == ["de B"]
    finally:
        almacen.cerrar()


# --- fail-closed ----------------------------------------------------------


def test_una_fila_corrupta_levanta_y_no_se_reinterpreta(tmp_path):
    ruta = tmp_path / "ledger.db"
    with _almacen(tmp_path) as almacen:
        almacen.guardar("instalacion", {"schema_version": ke.SCHEMA_VERSION})

    conexion = sqlite3.connect(ruta)
    try:
        with conexion:
            conexion.execute(
                "UPDATE ledger SET snapshot = ? WHERE ambito = ?", ("{no es json", "instalacion")
            )
    finally:
        conexion.close()

    with _almacen(tmp_path) as almacen:
        with pytest.raises(ke.EstadoIlegible):
            almacen.cargar("instalacion")


def test_una_fila_que_no_es_objeto_levanta(tmp_path):
    """JSON válido pero que no es un objeto (una lista, un número): se entiende,
    pero no es un estado. Tampoco se reinterpreta."""
    ruta = tmp_path / "ledger.db"
    with _almacen(tmp_path):
        pass
    conexion = sqlite3.connect(ruta)
    try:
        with conexion:
            conexion.execute(
                "INSERT INTO ledger (ambito, snapshot, actualizado_en) VALUES (?, ?, ?)",
                ("raro", "[1, 2, 3]", 1000.0),
            )
    finally:
        conexion.close()

    with _almacen(tmp_path) as almacen:
        with pytest.raises(ke.EstadoIlegible):
            almacen.cargar("raro")


def test_el_ledger_durable_sobre_sqlite_es_del_mismo_port(tmp_path):
    """El backend es intercambiable: el mismo `LedgerDurable` y los mismos tests
    de dedup entre sesiones valen igual sobre SQLite."""
    with _almacen(tmp_path) as almacen:
        primera = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
        pregunta, _ = primera.ledger.open_question("¿Por qué se cayó el servicio?")
        primera.guardar()

        segunda = ke.LedgerDurable(almacen=almacen, clock=lambda: 1000.0)
        otra_vez, creada = segunda.ledger.open_question("¿por que se cayo el servicio?")

        assert creada is False
        assert otra_vez.id == pregunta.id
        assert otra_vez.times_asked == 2
        assert isinstance(almacen, ke.AlmacenDurable)

"""
Tests de audit/audit_log.py — no requieren Docker, corren en cualquier
entorno con Python. Usan un archivo temporal por test (fixture `log`)
para no tocar logs/audit.log real del proyecto.
"""
from __future__ import annotations

import json

import pytest

from audit.audit_log import AuditEvent, AuditLog
from utils.correlation import set_correlation_id


@pytest.fixture
def log(tmp_path):
    return AuditLog(path=tmp_path / "audit.log")


def _event(summary="evento de prueba", outcome="success") -> AuditEvent:
    return AuditEvent(
        event_type="sandbox_execution",
        summary=summary,
        context={"key": "value"},
        outcome=outcome,
    )


def test_empty_log_verifies_true(log):
    assert log.verify_chain() is True


def test_single_event_verifies_true(log):
    log.record(_event())
    assert log.verify_chain() is True


def test_chain_of_events_verifies_true(log):
    for i in range(5):
        log.record(_event(summary=f"evento {i}"))
    assert log.verify_chain() is True


def test_events_are_correctly_linked(log):
    e1 = log.record(_event(summary="primero"))
    e2 = log.record(_event(summary="segundo"))
    assert e1.prev_hash == "genesis"
    assert e2.prev_hash == e1.event_hash


def test_tampering_with_content_is_detected(log):
    """
    Este es el caso que exponía el bug original: editar un campo de
    contenido (outcome) de una entrada existente SIN recalcular su hash
    debe romper la verificación, aunque el encadenamiento prev_hash siga
    intacto.
    """
    log.record(_event(outcome="failure"))
    assert log.verify_chain() is True

    # Simula un intento de adulteración: cambiar "failure" por "success"
    # directamente en el archivo, sin pasar por record().
    lines = log.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["outcome"] = "success"  # contenido alterado
    # event_hash y prev_hash quedan sin tocar — así es como se vería un
    # ataque real que no conoce el algoritmo de hashing.
    log.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_tampering_with_hash_is_detected(log):
    log.record(_event())
    lines = log.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["event_hash"] = "0" * 64  # hash falsificado
    log.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_inserting_forged_entry_breaks_chain(log):
    log.record(_event(summary="original"))
    forged = _event(summary="entrada forjada")
    forged.prev_hash = "genesis"  # no encadena con la entrada real anterior
    forged.event_hash = forged.compute_hash(log._hmac_key)

    with open(log.path, "a", encoding="utf-8") as f:
        from dataclasses import asdict
        f.write(json.dumps(asdict(forged)) + "\n")

    assert log.verify_chain() is False


def test_deleting_middle_entry_breaks_chain(log):
    log.record(_event(summary="uno"))
    log.record(_event(summary="dos"))
    log.record(_event(summary="tres"))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # Elimina la entrada del medio: rompe el encadenamiento prev_hash
    # de la tercera entrada, que apuntaba al hash de la segunda.
    remaining = [lines[0], lines[2]]
    log.path.write_text("\n".join(remaining) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_reordering_entries_breaks_chain(log):
    log.record(_event(summary="uno"))
    log.record(_event(summary="dos"))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    swapped = [lines[1], lines[0]]
    log.path.write_text("\n".join(swapped) + "\n", encoding="utf-8")

    assert log.verify_chain() is False


def test_repeated_records_on_same_instance_stay_chained(log):
    """Varias llamadas a record() sobre la misma instancia se encadenan bien."""
    events = [log.record(_event(summary=f"e{i}")) for i in range(10)]
    for i in range(1, len(events)):
        assert events[i].prev_hash == events[i - 1].event_hash
    assert log.verify_chain() is True


def test_new_instance_reading_existing_log_still_verifies(log):
    """
    Una instancia nueva de AuditLog apuntando al mismo archivo debe poder
    seguir agregando eventos correctamente encadenados, leyendo el último
    hash desde disco.
    """
    log.record(_event(summary="primero"))
    log.record(_event(summary="segundo"))

    fresh_instance = AuditLog(path=log.path)
    e3 = fresh_instance.record(_event(summary="tercero"))

    original_lines = log.path.read_text(encoding="utf-8").splitlines()
    e2_hash = json.loads(original_lines[1])["event_hash"]
    assert e3.prev_hash == e2_hash
    assert fresh_instance.verify_chain() is True


def test_interleaved_writes_from_two_instances_never_break_chain(log):
    """
    BUG REAL ENCONTRADO EN USO: dos instancias de AuditLog (simulando dos
    procesos, p.ej. el servidor real + un script de verificación aparte)
    escribiendo al mismo archivo intercaladamente rompían la cadena —
    cada una cacheaba su propio "último hash" en memoria y quedaba
    desincronizada de lo que la otra ya había escrito. Con el fix (leer
    siempre del disco bajo lock exclusivo, nunca de un caché), intercalar
    record() entre dos instancias distintas sobre el mismo archivo debe
    seguir encadenando correctamente.
    """
    instance_a = AuditLog(path=log.path)
    instance_b = AuditLog(path=log.path)

    for i in range(20):
        writer = instance_a if i % 2 == 0 else instance_b
        writer.record(_event(summary=f"evento {i}"))

    assert instance_a.verify_chain() is True
    assert instance_b.verify_chain() is True
    assert len(instance_a.path.read_text(encoding="utf-8").strip().splitlines()) == 20


# --- diagnose_chain(): distingue manipulación de contenido vs condición de carrera ---


def test_diagnose_chain_on_empty_log_is_valid(log):
    diagnosis = log.diagnose_chain()
    assert diagnosis.is_valid is True
    assert diagnosis.total_entries == 0
    assert diagnosis.breaks == []


def test_diagnose_chain_on_healthy_chain_is_valid(log):
    for i in range(5):
        log.record(_event(summary=f"evento {i}"))

    diagnosis = log.diagnose_chain()

    assert diagnosis.is_valid is True
    assert diagnosis.total_entries == 5
    assert diagnosis.breaks == []
    assert "íntegra" in diagnosis.summary()


def test_diagnose_chain_classifies_content_tampering(log):
    """Editar un campo sin recalcular el hash: hash_ok=False para esa entrada."""
    log.record(_event(outcome="failure"))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["outcome"] = "success"  # contenido alterado, event_hash sin tocar
    log.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    diagnosis = log.diagnose_chain()

    assert diagnosis.is_valid is False
    assert len(diagnosis.breaks) == 1
    assert diagnosis.breaks[0].hash_ok is False
    assert "manipulación" in diagnosis.summary()


def test_diagnose_chain_classifies_race_condition_style_break(log):
    """
    Reordenar dos entradas ya escritas (cada una sigue siendo internamente
    íntegra, solo el encadenamiento queda mal) simula exactamente el patrón
    que causó el bug real: hash_ok=True pero chain_ok=False.
    """
    log.record(_event(summary="uno"))
    log.record(_event(summary="dos"))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    swapped = [lines[1], lines[0]]
    log.path.write_text("\n".join(swapped) + "\n", encoding="utf-8")

    diagnosis = log.diagnose_chain()

    assert diagnosis.is_valid is False
    assert any(b.hash_ok is True and b.chain_ok is False for b in diagnosis.breaks)
    assert "condición de carrera" in diagnosis.summary()
    assert "manipulación real" not in diagnosis.summary()


def test_diagnose_chain_break_records_index_and_event_type(log):
    log.record(_event(summary="uno"))
    log.record(_event(summary="dos"))

    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join([lines[1], lines[0]]) + "\n", encoding="utf-8")

    diagnosis = log.diagnose_chain()

    assert diagnosis.breaks[0].index == 0
    assert diagnosis.breaks[0].event_type == "sandbox_execution"


# --- Correlation ID (ver utils/correlation.py) — propagación automática 2026-07-20 ---


@pytest.fixture(autouse=True)
def _reset_correlation_id():
    """Ningún test de este archivo debe filtrar su propio correlation_id
    a los que corren después en el mismo proceso de pytest."""
    set_correlation_id(None)
    yield
    set_correlation_id(None)


def test_record_injects_the_bound_correlation_id(log):
    set_correlation_id("abc123")
    event = log.record(_event())
    assert event.context["correlation_id"] == "abc123"


def test_record_without_a_bound_correlation_id_does_not_add_the_key(log):
    event = log.record(_event())
    assert "correlation_id" not in event.context


def test_record_never_overwrites_a_correlation_id_the_caller_already_set(log):
    set_correlation_id("del-contextvar")
    explicit_event = AuditEvent(
        event_type="sandbox_execution", summary="explícito",
        context={"correlation_id": "puesto-a-mano"}, outcome="success",
    )
    event = log.record(explicit_event)
    assert event.context["correlation_id"] == "puesto-a-mano"


# --- A-10/M-12 (auditoría externa Likay-OS, 2026-09-26): cadena firmada
# con HMAC (no SHA-256 sin clave) + resiliencia a un renglón corrupto ---


def test_hash_chain_is_keyed_not_plain_sha256(tmp_path):
    """
    Sin clave, cualquiera con acceso de escritura al archivo podía
    recalcular la cadena entera y verify_chain() seguía diciendo
    "íntegra" — un atacante que NO conoce la clave HMAC no puede
    reconstruir un event_hash válido, aunque reescriba todo el archivo.
    """
    log_a = AuditLog(path=tmp_path / "audit.log", hmac_key=b"clave-uno")
    log_b = AuditLog(path=tmp_path / "audit.log", hmac_key=b"clave-dos")

    log_a.record(_event())
    assert log_a.verify_chain() is True
    assert log_b.verify_chain() is False  # misma cadena, clave distinta


def test_a_corrupt_last_line_does_not_break_future_recording(log):
    """
    Antes: json.loads() sin try sobre la última línea significaba que
    UNA escritura parcial (crash a mitad de un f.write()) dejaba
    record() lanzando JSONDecodeError para siempre — auditoría muerta.
    """
    log.record(_event(summary="antes del crash"))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("esto no es json\n")  # simula una escritura parcial/corrupta

    event = log.record(_event(summary="después del crash"))
    assert event.event_hash  # record() no explotó
    assert event.prev_hash == "genesis"  # no pudo encadenar con la línea ilegible


def test_diagnose_chain_reports_unparseable_line_instead_of_raising(log):
    log.record(_event(summary="uno"))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("esto no es json\n")

    diagnosis = log.diagnose_chain()

    assert diagnosis.is_valid is False
    assert diagnosis.total_entries == 2
    assert diagnosis.breaks[-1].event_type == "(ilegible)"


def test_tail_reports_unparseable_line_instead_of_raising(log):
    log.record(_event(summary="uno"))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("esto no es json\n")

    entries = log.tail(10)

    assert len(entries) == 2
    assert entries[0]["event_type"] == "(ilegible)"

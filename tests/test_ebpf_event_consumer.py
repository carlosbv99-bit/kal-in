"""
Tests de kernel/lifecycle/ebpf/event_consumer.py (Fase E2 del plan eBPF, + el
filtro de cgroup adelantado de la Fase E7 tras el hallazgo real de la
evaluación de E4 — ver el docstring del módulo).

100% sin bpftrace ni Docker reales — alimenta al parser/consumidor con
líneas de texto fijas, exactamente lo que emitiría
kernel/lifecycle/ebpf/syscall_events.bt (ver ese archivo) o el ruido normal de
bpftrace (banners, warnings) que hay que poder ignorar sin romper el
consumo del resto del stream. `is_sandboxed_container_process` se
prueba con archivos de cgroup fixture (tmp_path), nunca /proc real.
"""
from __future__ import annotations

import pytest

from audit.audit_log import audit_log
from kernel.lifecycle.ebpf import event_consumer
from kernel.lifecycle.ebpf.event_consumer import (
    VIOLATION_SYSCALLS,
    SyscallEvent,
    consume_stream,
    is_sandboxed_container_process,
    parse_event_line,
    record_syscall_event,
)


@pytest.fixture
def assume_sandboxed_container(monkeypatch):
    """
    La mayoría de los tests de record_syscall_event/consume_stream
    quieren probar la lógica de violación vs. telemetría SIN depender
    de que el pid de prueba corresponda a un contenedor Docker real —
    este fixture fuerza is_sandboxed_container_process() a True, y los
    tests que específicamente quieren probar el filtro de cgroup lo
    dejan sin aplicar (o lo fuerzan a False).
    """
    monkeypatch.setattr(event_consumer, "is_sandboxed_container_process", lambda pid: True)


# --- parse_event_line ---


def test_parses_a_valid_connect_event():
    line = '{"syscall":"connect","pid":123,"comm":"python","family":2,"ip":"127.0.0.1","port":80}'
    event = parse_event_line(line)

    assert event == SyscallEvent(
        syscall="connect", pid=123,
        detail={"syscall": "connect", "pid": 123, "comm": "python", "family": 2, "ip": "127.0.0.1", "port": 80},
    )


def test_parses_a_valid_execve_event():
    line = '{"syscall":"execve","pid":456,"filename":"/usr/bin/docker"}'
    event = parse_event_line(line)

    assert event.syscall == "execve"
    assert event.pid == 456
    assert event.detail["filename"] == "/usr/bin/docker"


def test_ignores_empty_lines():
    assert parse_event_line("") is None
    assert parse_event_line("   \n") is None


def test_ignores_non_json_bpftrace_noise():
    # Ruido real visto en la corrida de la Fase E1: banners y warnings
    # de bpftrace que no son eventos.
    assert parse_event_line("Attached 7 probes") is None
    assert parse_event_line("[E1] prototipo detenido") is None


def test_ignores_json_without_required_fields():
    assert parse_event_line('{"algo": "que no es un evento"}') is None
    assert parse_event_line('{"syscall": "connect"}') is None  # falta pid
    assert parse_event_line('{"pid": 1}') is None  # falta syscall


def test_ignores_json_that_is_not_an_object():
    assert parse_event_line('[1, 2, 3]') is None
    assert parse_event_line('"un string cualquiera"') is None


# --- is_sandboxed_container_process: el filtro de cgroup (adelantado de E7) ---


def test_recognizes_a_systemd_driver_docker_cgroup_v2(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/system.slice/docker-abc123def456.scope\n", encoding="utf-8")

    assert is_sandboxed_container_process(1, cgroup_file=cgroup_file) is True


def test_recognizes_a_cgroupfs_driver_docker_cgroup_v1(tmp_path):
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text(
        "12:pids:/docker/abc123def456\n11:memory:/docker/abc123def456\n1:name=systemd:/docker/abc123def456\n",
        encoding="utf-8",
    )

    assert is_sandboxed_container_process(1, cgroup_file=cgroup_file) is True


def test_a_normal_host_process_is_not_a_sandboxed_container(tmp_path):
    """
    El caso real encontrado en la evaluación de E4: el proceso
    principal de kal (uvicorn) vive en el cgroup normal del usuario,
    no en uno de Docker.
    """
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/user.slice/user-1000.slice/session-2.scope\n", encoding="utf-8")

    assert is_sandboxed_container_process(105204, cgroup_file=cgroup_file) is False


def test_missing_cgroup_file_is_not_a_sandboxed_container(tmp_path):
    # El proceso ya terminó (o el pid nunca existió) — fail-closed, no explota.
    assert is_sandboxed_container_process(999999, cgroup_file=tmp_path / "no_existe") is False


# --- record_syscall_event: violación vs. telemetría rutinaria ---


def test_connect_setuid_setresuid_ptrace_are_recorded_as_violations(tmp_path, monkeypatch, assume_sandboxed_container):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    for syscall in VIOLATION_SYSCALLS:
        event = SyscallEvent(syscall=syscall, pid=1, detail={"syscall": syscall, "pid": 1})
        assert record_syscall_event(event) is True

    entries = audit_log.tail(10)
    assert len(entries) == len(VIOLATION_SYSCALLS)
    assert all(e["event_type"] == "syscall_policy_violation" for e in entries)
    assert all(e["outcome"] == "failure" for e in entries)


def test_execve_is_not_recorded_as_a_violation(tmp_path, monkeypatch, assume_sandboxed_container):
    """
    Decisión de diseño de la Fase E2: execve es mayormente maquinaria
    normal de Docker (docker/runc/containerd-shim, ver README) —
    auditarlo como "violación" diluiría el evento. Se descarta acá.
    """
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    event = SyscallEvent(syscall="execve", pid=1, detail={"syscall": "execve", "pid": 1, "filename": "/usr/bin/runc"})
    assert record_syscall_event(event) is False
    assert audit_log.tail(10) == []


def test_violation_syscall_from_a_non_container_process_is_not_recorded(tmp_path, monkeypatch):
    """
    HALLAZGO REAL de la evaluación de la Fase E4: sin este filtro, un
    connect() legítimo del propio proceso de kal (hablando con Ollama)
    se registraba como "violación". Este test es la regresión de ese
    hallazgo.
    """
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    monkeypatch.setattr(event_consumer, "is_sandboxed_container_process", lambda pid: False)

    event = SyscallEvent(syscall="connect", pid=105204, detail={"syscall": "connect", "pid": 105204})
    assert record_syscall_event(event) is False
    assert audit_log.tail(10) == []


def test_recorded_violation_includes_syscall_specific_detail_in_context(tmp_path, monkeypatch, assume_sandboxed_container):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    event = SyscallEvent(
        syscall="connect", pid=999,
        detail={"syscall": "connect", "pid": 999, "ip": "169.254.169.254", "port": 80},
    )
    record_syscall_event(event)

    entry = audit_log.tail(1)[0]
    assert entry["context"]["ip"] == "169.254.169.254"
    assert entry["context"]["pid"] == 999


# --- consume_stream: procesa un stream completo, tolera ruido mezclado ---


def test_consume_stream_processes_a_realistic_mixed_stream(tmp_path, monkeypatch, assume_sandboxed_container):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    stream = [
        "Attached 7 probes\n",
        '{"syscall":"execve","pid":1,"filename":"/usr/bin/docker"}\n',
        '{"syscall":"setuid","pid":2,"comm":"python","requested_uid":0}\n',
        "",
        '{"syscall":"execve","pid":3,"filename":"/usr/sbin/runc"}\n',
        '{"syscall":"connect","pid":2,"comm":"python","family":2,"ip":"8.8.8.8","port":53}\n',
        "[E1] prototipo detenido\n",
    ]

    recorded_count = consume_stream(stream)

    assert recorded_count == 2  # setuid + connect; los dos execve se descartan
    entries = audit_log.tail(10)
    assert {e["context"]["syscall"] for e in entries} == {"setuid", "connect"}


def test_consume_stream_returns_zero_for_a_stream_with_no_violations():
    assert consume_stream(['{"syscall":"execve","pid":1,"filename":"/bin/sh"}\n']) == 0


def test_consume_stream_returns_zero_for_an_empty_stream():
    assert consume_stream([]) == 0

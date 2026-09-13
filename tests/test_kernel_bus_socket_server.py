"""
Tests de kernel/api/socket_server.py::KernelBusSocketServer — la
plomería del socket Unix en sí (permisos declarados, auditoría,
límites), con un cliente de socket real pero SIN Docker (el contenedor
solo le agrega una frontera de proceso/filesystem al mismo socket, la
lógica de arriba es independiente de eso — ver
tests/test_sandboxed_skill.py para el caso con Docker real de punta a
punta).
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from kernel.api.bus import KernelServiceBus
from kernel.api.socket_server import _MAX_LINE_BYTES, KernelBusSocketServer, LineTooLongError
from utils.correlation import set_correlation_id


class FakeService:
    ALLOWED_ACTIONS = frozenset({"echo", "boom", "whoami"})

    def echo(self, text):
        return {"echoed": text}

    def boom(self):
        raise RuntimeError("boom interno")

    def whoami(self, skill_name):
        return {"skill_name": skill_name}


@pytest.fixture
def bus():
    b = KernelServiceBus()
    b.register("test", FakeService())
    return b


@pytest.fixture
def socket_path(tmp_path):
    return tmp_path / "kernel.sock"


def _send(sock_path: Path, message: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(str(sock_path))
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    sock.close()
    line, _, _ = buf.partition(b"\n")
    return json.loads(line.decode("utf-8"))


def test_allowed_method_dispatches_successfully(bus, socket_path):
    server = KernelBusSocketServer(bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="prueba")
    server.start()
    try:
        response = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "hola"}})
    finally:
        server.stop()

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"echoed": "hola"}}


def test_skill_name_of_the_calling_skill_reaches_the_bus_dispatch(bus, socket_path):
    """DownloadService (tool_integration/services.py) necesita saber
    QUÉ skill lo llama para el permiso de red por-skill — el servidor
    de socket ya sabe su propio `self.skill_name` (viene de
    sandboxed_skill.py al construirlo), esto confirma que llega hasta
    dispatch() sin que la skill misma tenga que mandarlo como
    parámetro (no debería poder suplantar a otra skill)."""
    server = KernelBusSocketServer(bus, allowed_methods=["test.whoami"], socket_path=socket_path, skill_name="download_via_kernel")
    server.start()
    try:
        response = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.whoami", "params": {}})
    finally:
        server.stop()

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"skill_name": "download_via_kernel"}}


def test_method_not_declared_is_rejected_before_touching_the_bus(bus, socket_path):
    server = KernelBusSocketServer(bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="prueba")
    server.start()
    try:
        response = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.boom", "params": {}})
    finally:
        server.stop()

    assert response["error"]["code"] == -32001  # PERMISSION_DENIED
    assert "no está declarado" in response["error"]["message"]


def test_action_exception_becomes_a_sanitized_error_response(bus, socket_path):
    """
    Hallazgo de la revisión de seguridad 2026-07-09: el detalle real de
    la excepción (que podría revelar rutas u otros detalles del host) ya
    no cruza el socket hacia la skill — solo un mensaje genérico. El
    detalle completo sigue disponible server-side (ver
    test_calls_and_denials_are_audited).
    """
    server = KernelBusSocketServer(bus, allowed_methods=["test.boom"], socket_path=socket_path, skill_name="prueba")
    server.start()
    try:
        response = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.boom", "params": {}})
    finally:
        server.stop()

    assert response["error"]["code"] == -32000  # INTERNAL_ERROR
    assert "boom interno" not in response["error"]["message"]
    assert "test.boom" in response["error"]["message"]


def test_calls_and_denials_are_audited(bus, socket_path, monkeypatch, tmp_path):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    server = KernelBusSocketServer(bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="auditada")
    server.start()
    try:
        _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "x"}})
        _send(socket_path, {"jsonrpc": "2.0", "id": 2, "method": "test.no_declarado", "params": {}})
    finally:
        server.stop()

    entries = audit_log.tail(5)
    event_types = {e["event_type"] for e in entries}
    assert "kernel_service_call" in event_types
    assert "kernel_service_denied" in event_types


def test_read_line_raises_when_no_newline_within_max_bytes():
    """
    Unit test directo de _read_line (sin socket real): una conexión que
    nunca manda un '\\n' no puede hacer crecer el buffer sin límite —
    hallazgo real de la revisión de seguridad 2026-07-09 (DoS de
    memoria en el proceso HOST, disparable por una skill del market,
    la confianza más baja del sistema).
    """
    class NeverEndingConn:
        def recv(self, n):
            return b"a" * n  # nunca manda un salto de línea

    with pytest.raises(LineTooLongError):
        KernelBusSocketServer._read_line(NeverEndingConn())


def test_read_line_still_accepts_a_large_but_legitimate_line():
    """Un prompt largo y real (bastante por debajo del límite) no debe
    verse afectado por el fix — no es un límite de "línea corta"."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "x" * 200_000}})

    class OneShotConn:
        def __init__(self, data):
            self._buf = data
        def recv(self, n):
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk

    line = KernelBusSocketServer._read_line(OneShotConn((payload + "\n").encode("utf-8")))
    assert line == payload


def test_oversized_line_is_rejected_over_a_real_socket_and_audited(bus, socket_path, monkeypatch, tmp_path):
    """
    De punta a punta con un socket Unix real (no el unit test de
    arriba): una skill que manda bytes sin salto de línea nunca hace
    crecer memoria sin límite, la conexión se corta, queda auditado, y
    el servidor SIGUE atendiendo conexiones siguientes con normalidad
    (una skill hostil no puede tumbar el servicio para las demás).
    """
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    server = KernelBusSocketServer(
        bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="atacante", idle_timeout=5,
    )
    server.start()
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(socket_path))
        try:
            sock.sendall(b"a" * (_MAX_LINE_BYTES + 1024))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # el servidor puede cortar antes de que termine de mandar todo — también válido
        try:
            data = sock.recv(1024)
        except (ConnectionResetError, OSError):
            data = b""
        sock.close()
        assert data == b""  # el servidor cerró la conexión, nunca respondió nada

        # La conexión siguiente (legítima) funciona con normalidad.
        response = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "sigo vivo"}})
    finally:
        server.stop()

    assert response["result"] == {"echoed": "sigo vivo"}
    entries = audit_log.tail(5)
    event_types = {e["event_type"] for e in entries}
    assert "kernel_line_too_long" in event_types


def test_max_requests_stops_accepting_after_the_limit(bus, socket_path):
    server = KernelBusSocketServer(
        bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="prueba",
        max_requests=1, idle_timeout=2,
    )
    server.start()
    try:
        first = _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "1"}})
        assert first["result"] == {"echoed": "1"}

        with pytest.raises(ConnectionRefusedError):
            _send(socket_path, {"jsonrpc": "2.0", "id": 2, "method": "test.echo", "params": {"text": "2"}})
    finally:
        server.stop()


def test_correlation_id_crosses_into_the_background_serving_thread(bus, socket_path, monkeypatch, tmp_path):
    """
    Hallazgo real (ver utils/correlation.py): _serve() corre en un
    thread de background propio (KernelBusSocketServer.start()) —
    contextvars NO cruza automáticamente a un thread nuevo. Sin
    set_correlation_id() explícito al principio de _serve(), la
    auditoría de esta llamada quedaría sin correlation_id pese a que el
    thread que llamó a start() sí tenía uno bien seteado.
    """
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    set_correlation_id("desde-el-thread-original")
    try:
        server = KernelBusSocketServer(
            bus, allowed_methods=["test.echo"], socket_path=socket_path, skill_name="prueba",
            correlation_id="desde-el-thread-original",
        )
        server.start()
        try:
            _send(socket_path, {"jsonrpc": "2.0", "id": 1, "method": "test.echo", "params": {"text": "x"}})
        finally:
            server.stop()
    finally:
        set_correlation_id(None)

    entries = audit_log.tail(5)
    call_entry = next(e for e in entries if e["event_type"] == "kernel_service_call")
    assert call_entry["context"]["correlation_id"] == "desde-el-thread-original"

"""
Tests de kernel/lifecycle/docker_runner.py::DockerSandboxRunner.run() —
el timeout ahora también cubre containers.run() en sí (incluido un pull
implícito de imagen), no solo container.wait().

BUG REAL ENCONTRADO EN USO (kal-in issue #4, 2026-09-21): containers.run()
dispara un pull IMPLÍCITO de la imagen si no está cacheada localmente —
sin red (o con red caída a mitad del pull), esa llamada podía colgar sin
ningún timeout propio. container.wait(timeout=...), usado más abajo, solo
acota la espera de un contenedor que YA arrancó — nunca cubría este caso.
Confirmado en un entorno real de Likay-OS sin red: varios minutos sin
respuesta ni error visible al usuario.

Fix: containers.run() ahora corre en un thread con
future.result(timeout=...), acotado con el mismo timeout_seconds que ya
se confía para la ejecución.
"""
from __future__ import annotations

import time

import docker

from kernel.lifecycle.docker_runner import DockerSandboxRunner
from tests.conftest import requires_docker


class _HangingContainers:
    """Simula un pull de imagen colgado: containers.run() nunca retorna
    dentro de la ventana de tiempo del test."""

    def __init__(self, hang_seconds: float):
        self.hang_seconds = hang_seconds
        self.called = False

    def run(self, *args, **kwargs):
        self.called = True
        time.sleep(self.hang_seconds)
        raise AssertionError("containers.run() no debería completarse dentro de este test")


class _FakeClient:
    def __init__(self, containers):
        self.containers = containers


def test_run_times_out_if_containers_run_itself_hangs(monkeypatch):
    hanging = _HangingContainers(hang_seconds=2.0)
    monkeypatch.setattr(docker, "from_env", lambda: _FakeClient(hanging))
    runner = DockerSandboxRunner()

    start = time.time()
    result = runner.run("print('hola')", timeout_seconds=1)
    elapsed = time.time() - start

    assert result.status == "timeout"
    assert "timed out" in result.stderr.lower() or "excedió" in result.stderr.lower()
    # El timeout debe respetarse (no esperar los 2s completos del hang) —
    # margen generoso para no ser flaky en una máquina cargada.
    assert elapsed < 1.9
    assert hanging.called


@requires_docker
def test_real_docker_run_still_completes_normally_within_the_timeout():
    """Regresión real: con Docker de verdad disponible y una imagen ya
    cacheada, el wrapper con thread no agrega latencia perceptible ni
    rompe el camino feliz."""
    runner = DockerSandboxRunner()

    result = runner.run("print('funciona')", timeout_seconds=30)

    assert result.status == "success"
    assert "funciona" in result.stdout

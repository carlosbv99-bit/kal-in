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


class _FakeContainer:
    def __init__(self):
        self.id = "fake-container-id"
        self.killed = False
        self.removed = False

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class _SlowThenSucceedsContainers:
    """Simula un pull LENTO pero no realmente colgado: tarda más que el
    timeout, pero eventualmente termina con un contenedor real — el
    escenario del bug de recursos huérfanos (2026-09-27)."""

    def __init__(self, delay_seconds: float, container: _FakeContainer):
        self.delay_seconds = delay_seconds
        self.container = container

    def run(self, *args, **kwargs):
        time.sleep(self.delay_seconds)
        return self.container


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


def test_container_created_after_the_timeout_gets_killed_and_removed(monkeypatch):
    """
    BUG REAL ENCONTRADO EN REVISIÓN (2026-09-27, preparando una auditoría
    externa): si containers.run() termina DESPUÉS del timeout (un pull
    lento, no realmente colgado), el contenedor resultante quedaba
    huérfano — nadie lo mataba ni lo removía. El callback de limpieza
    debe atraparlo, aunque el timeout ya se haya devuelto al llamador.
    """
    container = _FakeContainer()
    slow = _SlowThenSucceedsContainers(delay_seconds=0.3, container=container)
    monkeypatch.setattr(docker, "from_env", lambda: _FakeClient(slow))
    runner = DockerSandboxRunner()

    result = runner.run("print('hola')", timeout_seconds=0.1)

    assert result.status == "timeout"
    # El contenedor todavía no existía cuando run() devolvió el timeout.
    assert not container.killed
    assert not container.removed

    # containers.run() todavía sigue corriendo en el thread de fondo —
    # esperar a que termine (0.3s) y a que el callback corra.
    time.sleep(0.4)

    assert container.killed
    assert container.removed


@requires_docker
def test_real_docker_run_still_completes_normally_within_the_timeout():
    """Regresión real: con Docker de verdad disponible y una imagen ya
    cacheada, el wrapper con thread no agrega latencia perceptible ni
    rompe el camino feliz."""
    runner = DockerSandboxRunner()

    result = runner.run("print('funciona')", timeout_seconds=30)

    assert result.status == "success"
    assert "funciona" in result.stdout

"""
Tests de kernel/lifecycle/docker_runner.py::DockerSandboxRunner — la
conexión al daemon de Docker.

BUG REAL ENCONTRADO EN USO (2026-09-12, validando el arranque de kal en
una ISO live-boot de Likay-OS sin Docker instalado): DockerSandboxRunner
conectaba a Docker en su __init__ — sin Docker disponible, construir el
Orchestrator singleton (agent_core/orchestrator.py::Orchestrator())
tiraba abajo TODA la aplicación al importar el módulo, mucho antes de
que nadie intentara usar el sandbox de verdad. Confirmado en vivo
apuntando DOCKER_HOST a un puerto inalcanzable: ni /health respondía.

Fix: la conexión es perezosa (property `client`, se conecta recién al
primer uso real, dentro de run() — que ya atrapa DockerException/APIError
y devuelve un SandboxResult de error en vez de propagar). La ausencia de
Docker ahora degrada la llamada puntual al sandbox, nunca el arranque.
"""
from __future__ import annotations

import docker
from docker.errors import DockerException

from kernel.lifecycle.docker_runner import DockerSandboxRunner
from tests.conftest import requires_docker


def test_construction_never_connects_to_docker(monkeypatch):
    """
    El punto central del fix: instanciar DockerSandboxRunner() no debe
    tocar la red/el daemon de Docker en absoluto — si lo hiciera, este
    test lo detectaría porque docker.from_env() explotaría (mockeado
    para reventar si se llama).
    """
    def _explode(*a, **kw):
        raise AssertionError("DockerSandboxRunner() no debería conectar a Docker en el constructor")

    monkeypatch.setattr(docker, "from_env", _explode)

    DockerSandboxRunner()  # no debe levantar nada


def test_construction_succeeds_even_when_docker_is_completely_unreachable(monkeypatch):
    monkeypatch.setattr(docker, "from_env", lambda: (_ for _ in ()).throw(DockerException("no daemon")))

    runner = DockerSandboxRunner()  # no debe levantar nada

    assert runner is not None


def test_run_degrades_gracefully_instead_of_raising_when_docker_is_unreachable(monkeypatch):
    """
    La ausencia de Docker debe degradar ESTA llamada puntual (un
    SandboxResult de error), nunca propagar una excepción sin atrapar.
    """
    monkeypatch.setattr(docker, "from_env", lambda: (_ for _ in ()).throw(DockerException("no daemon")))
    runner = DockerSandboxRunner()

    result = runner.run("print('hola')")

    assert result.status == "error"
    assert "docker" in result.stderr.lower() or "daemon" in result.stderr.lower()


def test_client_property_connects_lazily_and_caches_the_client(monkeypatch):
    calls = {"n": 0}

    class _FakeClient:
        pass

    def _fake_from_env():
        calls["n"] += 1
        return _FakeClient()

    monkeypatch.setattr(docker, "from_env", _fake_from_env)
    runner = DockerSandboxRunner()

    assert calls["n"] == 0  # todavía no conectó
    first = runner.client
    assert calls["n"] == 1
    second = runner.client
    assert calls["n"] == 1  # reusa la conexión, no reconecta
    assert first is second


@requires_docker
def test_real_docker_still_works_end_to_end_after_the_lazy_connection_fix():
    """Regresión real: con Docker de verdad disponible, todo sigue
    funcionando igual que antes del fix — no es solo un cambio para el
    caso sin Docker, la conexión real no debe romperse."""
    runner = DockerSandboxRunner()
    result = runner.run("print('funciona con docker real')")

    assert result.status == "success"
    assert "funciona con docker real" in result.stdout

"""
Tests de `sandbox.refuse_root` (docker_runner.py::_rechazo_por_root).

Contexto: `run()` fija `user=f"{os.getuid()}:{os.getgid()}"`, así que un kal
corriendo como root produce contenedores root. Antes solo había una
ADVERTENCIA en el log — un control que nadie lee. Con `refuse_root: true` la
ejecución se rechaza, y queda auditada.

Apagado por defecto a propósito: correr kal dentro de un contenedor (donde el
PID 1 suele ser root) es un caso legítimo.
"""
from __future__ import annotations

from kernel.lifecycle.docker_runner import DockerSandboxRunner


def _runner_con_refuse(monkeypatch, *, uid: int, refuse: bool) -> DockerSandboxRunner:
    monkeypatch.setattr("os.getuid", lambda: uid)
    runner = DockerSandboxRunner()
    monkeypatch.setattr(runner.cfg, "refuse_root", refuse)
    return runner


def test_con_refuse_root_y_uid_0_rechaza(monkeypatch):
    runner = _runner_con_refuse(monkeypatch, uid=0, refuse=True)
    auditados = []
    monkeypatch.setattr(
        "kernel.lifecycle.docker_runner.audit_log.record",
        lambda evento: auditados.append(evento),
    )

    rechazo = runner._rechazo_por_root()

    assert rechazo is not None
    assert rechazo.status == "error"
    assert "refuse_root" in rechazo.stderr
    assert "root" in rechazo.stderr
    assert auditados and auditados[0].outcome == "failure"
    assert auditados[0].context["uid"] == 0


def test_con_refuse_root_apagado_no_rechaza(monkeypatch):
    runner = _runner_con_refuse(monkeypatch, uid=0, refuse=False)

    assert runner._rechazo_por_root() is None


def test_con_uid_sin_privilegios_no_rechaza_aunque_este_activo(monkeypatch):
    runner = _runner_con_refuse(monkeypatch, uid=1000, refuse=True)

    assert runner._rechazo_por_root() is None


def test_run_devuelve_el_rechazo_sin_tocar_docker(monkeypatch):
    runner = _runner_con_refuse(monkeypatch, uid=0, refuse=True)
    monkeypatch.setattr("kernel.lifecycle.docker_runner.audit_log.record", lambda evento: None)

    resultado = runner.run("print('no debería ejecutarse')")

    assert resultado.status == "error"
    assert "refuse_root" in resultado.stderr

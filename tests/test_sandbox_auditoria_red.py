"""
Tests de que el modo de red quede en la auditoría.

Hallazgo del 2026-11-02: el evento `sandbox_execution` registraba `context` y
`exit_code`, pero **no** `network_mode` ni los permisos concedidos. En el audit
log, una ejecución con red era indistinguible de una sin red — justo la
excepción que hay que poder reconstruir después.
"""
from __future__ import annotations

from kernel.lifecycle.docker_runner import SandboxResult
from kernel.lifecycle.executor import SandboxExecutor
from sdk.permissions import Permission


class _RunnerFalso:
    def run(self, *args, **kwargs) -> SandboxResult:
        return SandboxResult(
            status="success", stdout="ok", stderr="", exit_code=0, resource_usage={}
        )


def test_el_contexto_incluye_network_mode_y_permisos():
    contexto = SandboxExecutor._contexto_auditoria(
        {"skill": "x"}, "bridge", frozenset({Permission.NETWORK}), exit_code=0
    )

    assert contexto["network_mode"] == "bridge"
    assert contexto["permissions"] == ["network"]
    assert contexto["skill"] == "x"
    assert contexto["exit_code"] == 0


def test_network_mode_none_se_normaliza_a_none_textual():
    contexto = SandboxExecutor._contexto_auditoria({}, None, frozenset())

    assert contexto["network_mode"] == "none"
    assert contexto["permissions"] == []


def test_execute_trusted_audita_el_modo_de_red(monkeypatch):
    eventos = []
    monkeypatch.setattr(
        "kernel.lifecycle.executor.audit_log.record", lambda evento: eventos.append(evento)
    )
    executor = SandboxExecutor(runner=_RunnerFalso())

    executor.execute_trusted(
        "print('x')", context={"skill": "testigo"}, network_mode="bridge"
    )

    assert eventos, "no se auditó la ejecución"
    assert eventos[-1].context["network_mode"] == "bridge"
    assert eventos[-1].context["skill"] == "testigo"


def test_execute_sin_red_audita_none(monkeypatch):
    eventos = []
    monkeypatch.setattr(
        "kernel.lifecycle.executor.audit_log.record", lambda evento: eventos.append(evento)
    )
    executor = SandboxExecutor(runner=_RunnerFalso())

    executor.execute_trusted("print('x')")

    assert eventos[-1].context["network_mode"] == "none"

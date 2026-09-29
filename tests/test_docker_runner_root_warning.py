"""
Tests de kernel/lifecycle/docker_runner.py::DockerSandboxRunner.__init__
— advertencia si el proceso corre como root (B-5 en kal, auditoría
externa 2026-09-27, portado acá vía scripts/check_kernel_drift.py).

BUG REAL ENCONTRADO EN AUDITORÍA EXTERNA: run() fija
user=f"{os.getuid()}:{os.getgid()}" para el contenedor — si kal-in
mismo corre como uid 0, el contenedor sandboxeado TAMBIÉN corre como
root, sin ninguna advertencia (cap_drop=ALL/no-new-privileges/read_only
reducen el impacto, pero no lo eliminan).
"""
from __future__ import annotations

import logging

from kernel.lifecycle.docker_runner import DockerSandboxRunner


def test_warns_when_running_as_root(monkeypatch, caplog):
    monkeypatch.setattr("os.getuid", lambda: 0)

    with caplog.at_level(logging.WARNING):
        DockerSandboxRunner()

    assert any("root" in r.message for r in caplog.records)


def test_does_not_warn_for_a_non_root_uid(monkeypatch, caplog):
    monkeypatch.setattr("os.getuid", lambda: 1000)

    with caplog.at_level(logging.WARNING):
        DockerSandboxRunner()

    assert not any("root" in r.message for r in caplog.records)

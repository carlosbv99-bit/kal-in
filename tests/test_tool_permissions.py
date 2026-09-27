"""
Tests del modelo de permisos granulares (sdk/permissions.py)
y de cómo ToolManifest y SandboxExecutor lo aplican.
"""
from __future__ import annotations

import pytest

from kernel.lifecycle.docker_runner import SandboxResult
from kernel.lifecycle.executor import SandboxExecutor
from sdk.permissions import UNSUPPORTED_RUNTIME_PERMISSIONS, Permission
from sdk.skill import ToolManifest


class FakeRunner:
    """Runner falso: si se invoca, la prueba debería haber fallado antes."""

    def __init__(self):
        self.calls = 0

    def run(self, source_code, image=None, network_mode=None):
        self.calls += 1
        return SandboxResult(status="success", stdout="ok", stderr="", exit_code=0)


@pytest.fixture
def fake_runner():
    return FakeRunner()


@pytest.fixture
def executor(fake_runner):
    return SandboxExecutor(runner=fake_runner)


def test_manifest_always_implies_filesystem_read():
    manifest = ToolManifest(name="t", description="d")
    assert Permission.FILESYSTEM_READ in manifest.permissions


def test_requires_network_bool_derives_network_permission():
    manifest = ToolManifest(name="t", description="d", requires_network=True)
    assert Permission.NETWORK in manifest.permissions


def test_requires_filesystem_write_bool_derives_permission():
    manifest = ToolManifest(name="t", description="d", requires_filesystem_write=True)
    assert Permission.FILESYSTEM_WRITE in manifest.permissions


def test_explicit_permissions_combine_with_derived_ones():
    manifest = ToolManifest(
        name="t", description="d", requires_network=True, permissions=frozenset({Permission.CAMERA})
    )
    assert manifest.permissions == frozenset(
        {Permission.CAMERA, Permission.NETWORK, Permission.FILESYSTEM_READ}
    )


@pytest.mark.parametrize("permission", sorted(UNSUPPORTED_RUNTIME_PERMISSIONS, key=lambda p: p.value))
def test_unsupported_permissions_are_rejected_before_running_anything(executor, fake_runner, permission):
    result = executor.execute("print('hola')", granted_permissions=frozenset({permission}))

    assert result.status == "error"
    assert permission.value in result.stderr
    assert fake_runner.calls == 0  # rechazado ANTES de tocar el runner


def test_network_and_filesystem_write_are_not_rejected(executor, fake_runner):
    result = executor.execute(
        "print('hola')",
        granted_permissions=frozenset({Permission.NETWORK, Permission.FILESYSTEM_WRITE}),
    )

    assert result.status == "success"
    assert fake_runner.calls == 1


def test_no_granted_permissions_runs_normally(executor, fake_runner):
    result = executor.execute("print('hola')")

    assert result.status == "success"
    assert fake_runner.calls == 1

"""
Tests de kernel/registry/registry.py.

Usan un SandboxExecutor falso (inyectado) para no requerir Docker real
— lo que se está probando aquí es la LÓGICA del pipeline de
validación/aprobación, no el aislamiento del sandbox en sí (eso ya
está cubierto en tests/test_sandbox_*.py).
"""
from __future__ import annotations

import pytest

from kernel.lifecycle.docker_runner import SandboxResult
from kernel.registry.registry import ToolRegistry
from sdk.skill import ToolManifest
from utils.config import settings


class FakeSandboxExecutor:
    """
    Doble de prueba para SandboxExecutor: devuelve un resultado fijo en
    vez de ejecutar código real en Docker. Registra las llamadas para
    poder confirmar qué network_mode se pidió en cada ejecución.
    """

    def __init__(self, result: SandboxResult | None = None):
        self.result = result or SandboxResult(status="success", stdout="ok", stderr="", exit_code=0)
        self.calls: list[dict] = []

    def execute(self, source_code, context=None, network_mode=None, image=None, granted_permissions=None):
        self.calls.append({
            "source_code": source_code, "context": context, "network_mode": network_mode,
            "image": image, "granted_permissions": granted_permissions,
        })
        return self.result


@pytest.fixture
def fake_sandbox():
    return FakeSandboxExecutor()


@pytest.fixture
def registry(fake_sandbox):
    return ToolRegistry(sandbox=fake_sandbox)


def _manifest(**overrides) -> ToolManifest:
    defaults = {"name": "herramienta_de_prueba", "description": "una herramienta de prueba", "created_by": "agent"}
    defaults.update(overrides)
    return ToolManifest(**defaults)


def test_safe_code_without_sensitive_permissions_auto_activates(registry):
    manifest = _manifest()
    pending = registry.propose_dynamic_tool(manifest, "print('hola')")

    assert pending.status == "active"
    assert registry.get("herramienta_de_prueba") is not None


def test_activated_tool_is_actually_callable(registry, fake_sandbox):
    """
    Confirma el bug real corregido: antes, 'activar' solo cambiaba un
    string de estado — la herramienta nunca quedaba en _active_tools
    ni era invocable. Ahora sí debe poder llamarse execute().
    """
    manifest = _manifest()
    registry.propose_dynamic_tool(manifest, "print('hola')")

    tool = registry.get("herramienta_de_prueba")
    artifact = tool.execute()

    assert artifact.metadata["status"] == "success"
    assert artifact.metadata["stdout"] == "ok"


def test_unsafe_code_is_rejected_and_never_activated(registry):
    manifest = _manifest()
    pending = registry.propose_dynamic_tool(manifest, "import os\nos.system('ls')")

    assert pending.status == "rejected"
    assert registry.get("herramienta_de_prueba") is None


def test_sandbox_failure_during_trial_run_is_rejected(registry, fake_sandbox):
    fake_sandbox.result = SandboxResult(status="error", stdout="", stderr="boom", exit_code=1)
    manifest = _manifest()

    pending = registry.propose_dynamic_tool(manifest, "raise ValueError('boom')")

    assert pending.status == "rejected"
    assert pending.reason == "boom"
    assert registry.get("herramienta_de_prueba") is None


def test_tool_requiring_network_is_pending_by_default(registry, monkeypatch):
    monkeypatch.setattr(settings.tool_integration, "require_human_approval_for", ["network"])
    manifest = _manifest(requires_network=True)

    pending = registry.propose_dynamic_tool(manifest, "print('necesito red')")

    assert pending.status == "pending_approval"
    assert registry.get("herramienta_de_prueba") is None  # NO debe estar activa todavía


def test_approving_pending_tool_activates_it(registry, monkeypatch):
    monkeypatch.setattr(settings.tool_integration, "require_human_approval_for", ["network"])
    manifest = _manifest(requires_network=True)
    registry.propose_dynamic_tool(manifest, "print('necesito red')")

    assert registry.get("herramienta_de_prueba") is None

    registry.approve_pending_tool("herramienta_de_prueba", approved_by="kalin")

    assert registry.get("herramienta_de_prueba") is not None


def test_approved_network_tool_actually_gets_network_mode_at_execution(registry, fake_sandbox, monkeypatch):
    """
    Confirma el segundo bug real corregido: aprobar una herramienta con
    requires_network=True debe traducirse en network_mode="bridge" al
    ejecutarla de verdad, no solo cambiar su status.
    """
    monkeypatch.setattr(settings.tool_integration, "require_human_approval_for", ["network"])
    manifest = _manifest(requires_network=True)
    registry.propose_dynamic_tool(manifest, "print('necesito red')")
    registry.approve_pending_tool("herramienta_de_prueba", approved_by="kalin")

    tool = registry.get("herramienta_de_prueba")
    tool.execute()

    assert len(fake_sandbox.calls) == 2  # una del trial run + una de la ejecución real
    assert fake_sandbox.calls[-1]["network_mode"] == "bridge"


def test_tool_without_network_never_gets_bridge_mode(registry, fake_sandbox):
    manifest = _manifest(requires_network=False)
    registry.propose_dynamic_tool(manifest, "print('sin red')")

    tool = registry.get("herramienta_de_prueba")
    tool.execute()

    assert fake_sandbox.calls[-1]["network_mode"] is None


def test_cannot_approve_a_tool_that_is_not_pending(registry):
    with pytest.raises(ValueError):
        registry.approve_pending_tool("no_existe", approved_by="kalin")


def test_cannot_double_approve_same_tool(registry, monkeypatch):
    monkeypatch.setattr(settings.tool_integration, "require_human_approval_for", ["network"])
    manifest = _manifest(requires_network=True)
    registry.propose_dynamic_tool(manifest, "print('necesito red')")
    registry.approve_pending_tool("herramienta_de_prueba", approved_by="kalin")

    with pytest.raises(ValueError):
        registry.approve_pending_tool("herramienta_de_prueba", approved_by="alguien_mas")


def test_filesystem_write_permission_also_gates_approval(registry, monkeypatch):
    monkeypatch.setattr(
        settings.tool_integration, "require_human_approval_for", ["filesystem_write"]
    )
    manifest = _manifest(requires_filesystem_write=True)

    pending = registry.propose_dynamic_tool(manifest, "print('necesito escribir')")

    assert pending.status == "pending_approval"


def test_static_tool_registration_bypasses_pipeline_entirely(registry):
    from sdk.artifacts import Artifact
    from sdk.skill import Tool

    class DummyStaticTool(Tool):
        manifest = _manifest(name="estatica", created_by="system")

        def execute(self, **kwargs):
            return Artifact(modality="text", uri="", metadata={})

    registry.register_static_tool(DummyStaticTool())

    assert registry.get("estatica") is not None

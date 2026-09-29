"""
Tests de kernel/permissions/permission_cascade.py — la cascada de
permisos de varios niveles (global -> nivel de confianza -> sesión ->
manifiesto de la herramienta), y de trust_tier_for(), que decide el
nivel de confianza por el TIPO del wrapper registrado, nunca por
manifest.created_by (autodeclarado por la propia herramienta/skill).

Separado deliberadamente de tests/test_tool_permissions.py: ese archivo
prueba sdk/permissions.py, que se copia tal cual dentro de
cada contenedor de skill y por eso debe seguir siendo 100% stdlib —
este módulo (permission_cascade.py) SÍ depende de utils.config y nunca
se envía a un contenedor.
"""
from __future__ import annotations

from kernel.permissions.permission_cascade import PermissionCascade, trust_tier_for
from kernel.registry.registry import DynamicSandboxedTool
from kernel.registry.sandboxed_skill import SandboxedSkillTool
from sdk.artifacts import Artifact
from sdk.permissions import Permission
from sdk.skill import Tool, ToolManifest
from utils.config import settings

# --- trust_tier_for() — la señal de confianza viene del TIPO del wrapper,
# nunca de manifest.created_by (que la propia herramienta/skill autodeclara) ---


class _PlainTool(Tool):
    manifest = ToolManifest(name="plain", description="d", created_by="system")

    def execute(self, **kwargs) -> Artifact:
        return Artifact(modality="text", uri="", metadata={})


def test_static_first_party_tool_is_system_tier():
    assert trust_tier_for(_PlainTool()) == "system"


def test_dynamic_tool_is_agent_tier_regardless_of_created_by():
    manifest = ToolManifest(name="d", description="d", created_by="system")  # autodeclarado, no debería importar
    tool = DynamicSandboxedTool(manifest, "print(1)", sandbox=object())
    assert trust_tier_for(tool) == "agent"


def test_skill_tool_is_skill_tier_regardless_of_created_by(tmp_path):
    manifest = ToolManifest(name="s", description="d", created_by="system")  # ídem: autodeclarado
    tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=tmp_path, entry_point="tool:X",
        image="img", sandbox=object(), artifacts_root=tmp_path / "artifacts",
    )
    assert trust_tier_for(tool) == "skill"


# --- PermissionCascade ---


class _FakeCascadeConfig:
    def __init__(self, globally_denied=(), trust_tier_caps=None):
        self.globally_denied = list(globally_denied)
        self.trust_tier_caps = trust_tier_caps or {
            "system": [p.value for p in Permission],
            "agent": ["filesystem_read", "filesystem_write", "network"],
            "skill": ["filesystem_read"],
        }


def test_cascade_allows_when_every_level_covers_it():
    cascade = PermissionCascade(_FakeCascadeConfig())
    missing = cascade.missing_permissions(frozenset({Permission.FILESYSTEM_READ}), "system")
    assert missing == frozenset()


def test_cascade_denies_when_trust_tier_does_not_cover_it():
    cascade = PermissionCascade(_FakeCascadeConfig())
    missing = cascade.missing_permissions(frozenset({Permission.NETWORK}), "skill")
    assert missing == frozenset({Permission.NETWORK})


def test_cascade_globally_denied_wins_even_for_system_tier():
    cascade = PermissionCascade(_FakeCascadeConfig(globally_denied=["network"]))
    missing = cascade.missing_permissions(frozenset({Permission.NETWORK}), "system")
    assert missing == frozenset({Permission.NETWORK})


def test_cascade_session_denied_wins_even_when_tier_allows_it():
    cascade = PermissionCascade(_FakeCascadeConfig())
    missing = cascade.missing_permissions(
        frozenset({Permission.NETWORK}), "system", session_denied=frozenset({Permission.NETWORK}),
    )
    assert missing == frozenset({Permission.NETWORK})


def test_cascade_unknown_trust_tier_denies_everything():
    cascade = PermissionCascade(_FakeCascadeConfig())
    missing = cascade.missing_permissions(frozenset({Permission.FILESYSTEM_READ}), "tier_que_no_existe")
    assert missing == frozenset({Permission.FILESYSTEM_READ})


def test_cascade_only_reports_what_was_actually_requested():
    """Un tier restrictivo no debería 'inventar' permisos que la
    herramienta ni pidió."""
    cascade = PermissionCascade(_FakeCascadeConfig())
    missing = cascade.missing_permissions(frozenset({Permission.FILESYSTEM_READ}), "skill")
    assert missing == frozenset()  # skill SÍ cubre filesystem_read


# --- Test de contrato (A-5, auditoría externa Likay-OS, 2026-09-26) ---
#
# Contra la config REAL (config/config.yaml vía utils.config.settings,
# no un _FakeCascadeConfig) — si alguien alguna vez vacía
# globally_denied "temporalmente" para probar algo y se olvida de
# revertirlo, o trust_tier_for() alguna vez clasifica mal una
# herramienta como "system", este test falla en vez de dejar pasar en
# silencio el permiso más peligroso del sistema (docker: acceso
# directo al daemon del host, fuera del sandbox que todo lo demás
# respeta).


def test_dangerous_permissions_are_globally_denied_by_default_even_for_system_tier():
    cascade = PermissionCascade(settings.permissions)
    dangerous = frozenset({Permission.DOCKER, Permission.CAMERA, Permission.MICROPHONE, Permission.CLIPBOARD})
    missing = cascade.missing_permissions(dangerous, "system")
    assert missing == dangerous


# --- M-1 (auditoría externa de kal, 2026-09-27 — encontrado por
# scripts/check_kernel_drift.py, sin equivalente propio en la
# auditoría de este repo): DynamicSandboxedTool.execute() no
# consultaba la cascada en absoluto — a diferencia de
# SandboxedSkillTool.execute() (K-4), network_mode se derivaba SOLO de
# manifest.permissions. globally_denied documentado como "pase lo que
# pase" era falso para este wrapper. ---


class _FakeSandbox:
    """Doble de prueba: registra si execute() se llegó a invocar."""

    def __init__(self):
        self.calls: list[dict] = []

    def execute(self, source_code, context=None, network_mode=None, image=None, granted_permissions=None):
        self.calls.append({"network_mode": network_mode})
        from kernel.lifecycle.docker_runner import SandboxResult
        return SandboxResult(status="success", stdout="ok", stderr="", exit_code=0)


def test_dynamic_tool_execute_is_denied_when_cascade_forbids_the_permission(monkeypatch):
    import kernel.registry.registry as registry_module

    restrictive_cascade = PermissionCascade(_FakeCascadeConfig(globally_denied=["network"]))
    monkeypatch.setattr(registry_module, "permission_cascade", restrictive_cascade)

    manifest = ToolManifest(name="agente_con_red", description="d", created_by="agent", requires_network=True)
    fake_sandbox = _FakeSandbox()
    tool = DynamicSandboxedTool(manifest, "print('hola')", sandbox=fake_sandbox)

    artifact = tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "network" in artifact.metadata["stderr"]
    # La cascada debe rechazar ANTES de tocar el sandbox — nunca se
    # ejecuta código con menos permisos de los que la herramienta asume.
    assert fake_sandbox.calls == []


def test_dynamic_tool_execute_still_works_when_cascade_allows_it(monkeypatch):
    import kernel.registry.registry as registry_module

    permissive_cascade = PermissionCascade(_FakeCascadeConfig())  # default: agent cubre network
    monkeypatch.setattr(registry_module, "permission_cascade", permissive_cascade)

    manifest = ToolManifest(name="agente_con_red", description="d", created_by="agent", requires_network=True)
    fake_sandbox = _FakeSandbox()
    tool = DynamicSandboxedTool(manifest, "print('hola')", sandbox=fake_sandbox)

    artifact = tool.execute()

    assert artifact.metadata["status"] == "success"
    assert fake_sandbox.calls == [{"network_mode": "bridge"}]

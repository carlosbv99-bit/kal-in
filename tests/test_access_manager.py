"""
Tests de kernel/permissions/access_manager.py::AccessManager — el motor
genérico de arbitraje de acceso (política + escalamiento + concesiones
en 4 escalas + auditoría) del que
kernel/permissions/filesystem_access_manager.py y
kernel/permissions/network_access_manager.py son los dos adaptadores
reales. Estos tests ejercitan el motor DIRECTO, con una política de
prueba simple — el comportamiento equivalente ya probado contra el
adaptador de filesystem vive en tests/test_filesystem_access_manager.py
(y confirma que el refactor no cambió nada observable).
"""
from __future__ import annotations

import json

import pytest

from audit.audit_log import audit_log
from kernel.permissions.access_manager import AccessManager, AccessManagerError


@pytest.fixture(autouse=True)
def _redirect_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")


def _is_auto_allowed(scope: str, action: str, _resource_key: str) -> bool:
    """Política de prueba: alcance 'safe' + acción 'read' se auto-permite; cualquier otra cosa escala."""
    return scope == "safe" and action == "read"


@pytest.fixture
def manager(tmp_path):
    return AccessManager(
        resource_kind="test_resource",
        grants_path=tmp_path / "test_grants.json",
        is_auto_allowed=_is_auto_allowed,
        event_type_prefix="filesystem_access",  # reusa un EventType real ya existente
    )


# --- Política ---


def test_covered_by_policy_is_auto_allowed(manager):
    assert manager.evaluate("skill_x", "safe", "read", "algo") == "auto_allowed"


def test_not_covered_by_policy_requires_approval(manager):
    assert manager.evaluate("skill_x", "risky", "write", "algo") == "requires_approval"


# --- Solicitudes pendientes ---


def test_pending_request_appears_in_list_pending(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    assert pending in manager.list_pending()


def test_approving_removes_from_pending_list(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="once")
    assert manager.list_pending() == []


def test_denying_removes_from_pending_list(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.deny(pending.id)
    assert manager.list_pending() == []


def test_approving_an_unknown_request_raises_a_clear_error(manager):
    with pytest.raises(AccessManagerError, match="no-existe"):
        manager.approve("no-existe", level="once")


# --- Las 4 escalas de concesión ---


def test_once_level_never_persists_the_grant(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="once")
    assert manager.evaluate("skill_x", "risky", "write", "algo") == "requires_approval"


def test_session_level_grant_is_remembered_within_the_same_instance(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="session")
    assert manager.evaluate("skill_x", "risky", "write", "algo") == "auto_allowed"


def test_session_level_grant_is_lost_on_a_new_instance(tmp_path):
    grants_path = tmp_path / "test_grants.json"
    first = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    pending = first.create_pending_request("skill_x", "risky", "write", "algo")
    first.approve(pending.id, level="session")

    second = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    assert second.evaluate("skill_x", "risky", "write", "algo") == "requires_approval"


def test_project_level_grant_is_scoped_to_the_exact_resource_key(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "recurso_a")
    manager.approve(pending.id, level="project")

    assert manager.evaluate("skill_x", "risky", "write", "recurso_a") == "auto_allowed"
    assert manager.evaluate("skill_x", "risky", "write", "recurso_b") == "requires_approval"


def test_project_level_grant_survives_a_new_instance_over_the_same_file(tmp_path):
    grants_path = tmp_path / "test_grants.json"
    first = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    pending = first.create_pending_request("skill_x", "risky", "write", "recurso_a")
    first.approve(pending.id, level="project")

    second = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    assert second.evaluate("skill_x", "risky", "write", "recurso_a") == "auto_allowed"


def test_skill_level_grant_applies_to_any_resource_key_for_that_skill(tmp_path):
    grants_path = tmp_path / "test_grants.json"
    first = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    pending = first.create_pending_request("skill_x", "risky", "write", "recurso_a")
    first.approve(pending.id, level="skill")

    second = AccessManager("test_resource", grants_path, _is_auto_allowed, "filesystem_access")
    assert second.evaluate("skill_x", "risky", "write", "recurso_b") == "auto_allowed"
    assert second.evaluate("skill_y", "risky", "write", "recurso_b") == "requires_approval"
    assert second.evaluate("skill_x", "risky", "read", "recurso_b") == "requires_approval"


# --- Auditoría ---


def test_auto_allowed_decision_is_audited_with_the_given_prefix(manager):
    manager.evaluate("skill_x", "safe", "read", "algo")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_requested" in event_types
    assert "filesystem_access_granted" in event_types


def test_escalated_decision_is_audited(manager):
    manager.evaluate("skill_x", "risky", "write", "algo")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_escalated" in event_types


def test_approval_and_denial_are_audited(manager):
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="once")

    another_pending = manager.create_pending_request("skill_x", "risky", "read", "algo")
    manager.deny(another_pending.id)

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_granted" in event_types
    assert "filesystem_access_denied" in event_types


def test_audit_context_includes_resource_kind(manager):
    manager.evaluate("skill_x", "safe", "read", "algo")

    entry = audit_log.tail(10)[0]
    assert entry["context"]["resource_kind"] == "test_resource"


# --- M-4 (auditoría externa de kal, 2026-09-27, portada acá vía
# scripts/check_kernel_drift.py): un archivo de grants corrupto no
# debe inutilizar el motor de decisión entero ---


def test_corrupted_grants_file_is_treated_as_no_grants_not_a_crash(manager):
    """
    Sin manejo de errores, JSON inválido en el archivo de grants hacía
    que JSONDecodeError se propagara sin atrapar desde evaluate() —
    TODO pedido posterior fallaba, no solo el de la skill afectada.
    Fail-safe: se trata como "sin grants" (la dirección segura,
    evaluate() cae a requires_approval), nunca como un crash.
    """
    manager._grants_path.parent.mkdir(parents=True, exist_ok=True)
    manager._grants_path.write_text("esto no es json valido", encoding="utf-8")

    decision = manager.evaluate("skill_x", "risky", "write", "algo")

    assert decision == "requires_approval"


def test_a_grant_with_an_unexpected_field_is_discarded_not_a_crash(manager):
    """Un grant individual con un campo extra/faltante (TypeError al
    construir _Grant) se descarta solo a ÉL — no debe tirar abajo la
    lectura de los demás grants válidos en el mismo archivo."""
    manager.evaluate("skill_x", "risky", "write", "algo")
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="project")  # 1 grant válido persistido

    raw = json.loads(manager._grants_path.read_text(encoding="utf-8"))
    raw.append({"skill_name": "otra_skill", "campo_que_no_existe": "valor"})
    manager._grants_path.write_text(json.dumps(raw), encoding="utf-8")

    # El grant válido original sigue funcionando — el inválido no lo arrastra.
    decision = manager.evaluate("skill_x", "risky", "write", "algo")
    assert decision == "auto_allowed"


def test_grants_directory_is_created_with_restrictive_permissions(tmp_path):
    """M-4/B-6: el directorio de grants persistidos quedaba con lo que
    diera el umask del proceso (típicamente escribible por grupo) —
    ahora se fuerza 0700 al persistir el primer grant."""
    grants_path = tmp_path / "nested" / "grants.json"
    manager = AccessManager(
        resource_kind="test_resource", grants_path=grants_path,
        is_auto_allowed=_is_auto_allowed, event_type_prefix="filesystem_access",
    )
    pending = manager.create_pending_request("skill_x", "risky", "write", "algo")
    manager.approve(pending.id, level="project")

    assert oct(grants_path.parent.stat().st_mode)[-3:] == "700"

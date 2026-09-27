"""
Tests de kernel/permissions/filesystem_access_manager.py — Permission
Manager del Kernel para filesystem: decide auto_allowed vs
requires_approval por política (scope×acción), recuerda concesiones ya
otorgadas en 4 escalas (once/session/project/skill), y audita cada
decisión.
"""
from __future__ import annotations

import pytest

from audit.audit_log import audit_log
from kernel.permissions.filesystem_access_manager import (
    FilesystemAccessError,
    FilesystemAccessManager,
)
from kernel.permissions.filesystem_permissions import FilesystemAction, FilesystemScope


@pytest.fixture(autouse=True)
def _redirect_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")


@pytest.fixture
def manager(tmp_path):
    return FilesystemAccessManager(grants_path=tmp_path / "filesystem_grants.json")


# --- Política default ---


def test_workspace_create_is_auto_allowed_by_default_policy(manager):
    decision = manager.evaluate("vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.CREATE, "workspace")
    assert decision == "auto_allowed"


def test_workspace_modify_is_auto_allowed_by_default_policy(manager):
    decision = manager.evaluate("vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.MODIFY, "workspace")
    assert decision == "auto_allowed"


def test_workspace_delete_requires_approval_by_default_policy(manager):
    decision = manager.evaluate("vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.DELETE, "workspace")
    assert decision == "requires_approval"


@pytest.mark.parametrize("scope", [FilesystemScope.HOME, FilesystemScope.EXTERNAL])
@pytest.mark.parametrize("action", [FilesystemAction.CREATE, FilesystemAction.MODIFY, FilesystemAction.READ])
def test_home_and_external_always_require_approval_by_default_policy(manager, scope, action):
    decision = manager.evaluate("some_skill", scope, action, "/home/user/algo")
    assert decision == "requires_approval"


# --- Solicitudes pendientes ---


def test_pending_request_appears_in_list_pending(manager):
    pending = manager.create_pending_request(
        "vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.DELETE, "workspace"
    )
    assert pending in manager.list_pending()


def test_approving_a_pending_request_removes_it_from_the_pending_list(manager):
    pending = manager.create_pending_request(
        "vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.DELETE, "workspace"
    )
    manager.approve(pending.id, level="once")
    assert manager.list_pending() == []


def test_denying_a_pending_request_removes_it_from_the_pending_list(manager):
    pending = manager.create_pending_request(
        "vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.DELETE, "workspace"
    )
    manager.deny(pending.id)
    assert manager.list_pending() == []


def test_approving_an_unknown_request_raises_a_clear_error(manager):
    with pytest.raises(FilesystemAccessError, match="no-existe"):
        manager.approve("no-existe", level="once")


# --- Las 4 escalas de concesión ---


def test_once_level_never_persists_the_grant(manager):
    pending = manager.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    manager.approve(pending.id, level="once")

    decision = manager.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    assert decision == "requires_approval"  # se vuelve a preguntar, "once" no se recuerda


def test_session_level_grant_is_remembered_within_the_same_manager_instance(manager):
    pending = manager.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    manager.approve(pending.id, level="session")

    decision = manager.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    assert decision == "auto_allowed"


def test_session_level_grant_is_lost_on_a_new_manager_instance(tmp_path):
    grants_path = tmp_path / "filesystem_grants.json"
    first = FilesystemAccessManager(grants_path=grants_path)
    pending = first.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    first.approve(pending.id, level="session")

    second = FilesystemAccessManager(grants_path=grants_path)
    decision = second.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    assert decision == "requires_approval"


def test_project_level_grant_is_scoped_to_the_exact_resource_key(manager):
    pending = manager.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/a")
    manager.approve(pending.id, level="project")

    assert manager.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/a") == "auto_allowed"
    # Un resource_key DISTINTO no queda cubierto por la misma concesión.
    assert manager.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/b") == "requires_approval"


def test_project_level_grant_survives_a_new_manager_instance_over_the_same_file(tmp_path):
    grants_path = tmp_path / "filesystem_grants.json"
    first = FilesystemAccessManager(grants_path=grants_path)
    pending = first.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/a")
    first.approve(pending.id, level="project")

    second = FilesystemAccessManager(grants_path=grants_path)
    decision = second.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/a")
    assert decision == "auto_allowed"


def test_skill_level_grant_applies_to_any_resource_key_for_that_skill(tmp_path):
    grants_path = tmp_path / "filesystem_grants.json"
    first = FilesystemAccessManager(grants_path=grants_path)
    pending = first.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/a")
    first.approve(pending.id, level="skill")

    second = FilesystemAccessManager(grants_path=grants_path)
    # Persiste Y cubre un resource_key DISTINTO del que se pidió originalmente.
    assert second.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/b") == "auto_allowed"
    # Pero no otra skill, ni otra acción.
    assert second.evaluate("skill_y", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user/b") == "requires_approval"
    assert second.evaluate("skill_x", FilesystemScope.HOME, FilesystemAction.DELETE, "/home/user/b") == "requires_approval"


# --- Auditoría ---


def test_auto_allowed_decision_is_audited(manager):
    manager.evaluate("vscode_integration", FilesystemScope.WORKSPACE, FilesystemAction.CREATE, "workspace")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_requested" in event_types
    assert "filesystem_access_granted" in event_types


def test_escalated_decision_is_audited(manager):
    manager.evaluate("some_skill", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_escalated" in event_types


def test_approval_and_denial_are_audited(manager):
    pending = manager.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.CREATE, "/home/user")
    manager.approve(pending.id, level="once")

    another_pending = manager.create_pending_request("skill_x", FilesystemScope.HOME, FilesystemAction.DELETE, "/home/user")
    manager.deny(another_pending.id)

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "filesystem_access_granted" in event_types
    assert "filesystem_access_denied" in event_types

"""
Tests de kernel/permissions/network_access_manager.py — segundo
adaptador real de kernel/permissions/access_manager.py::AccessManager (el
primero es filesystem_access_manager.py). Antes de este módulo, un
dominio no permitido rechazaba con un error inmediato, sin ningún
camino de escalar a un humano ni de recordar una concesión — acá se
prueba que ahora sí lo tiene, con las mismas 4 escalas que filesystem.
"""
from __future__ import annotations

import pytest

from audit.audit_log import audit_log
from kernel.permissions.network_access_manager import (
    NetworkAccessError,
    NetworkAccessManager,
)
from kernel.permissions.network_permissions import NetworkAction, NetworkScope
from utils.config import settings


@pytest.fixture(autouse=True)
def _redirect_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")


@pytest.fixture(autouse=True)
def _fixed_allowed_domains(monkeypatch):
    monkeypatch.setattr(settings.downloads, "allowed_domains", ["unsplash.com"])
    monkeypatch.setattr(settings.browser, "allowed_domains", ["example.com"])


@pytest.fixture
def manager(tmp_path):
    return NetworkAccessManager(grants_path=tmp_path / "network_grants.json")


# --- Política (reusa settings.downloads.allowed_domains, sin config nueva) ---


def test_domain_in_the_allowlist_is_auto_allowed(manager):
    decision = manager.evaluate("vscode_integration", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "unsplash.com")
    assert decision == "auto_allowed"


def test_domain_not_in_the_allowlist_requires_approval(manager):
    decision = manager.evaluate("vscode_integration", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")
    assert decision == "requires_approval"


def test_subdomain_of_an_allowed_domain_is_auto_allowed(manager):
    decision = manager.evaluate("vscode_integration", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "images.unsplash.com")
    assert decision == "auto_allowed"


def test_browse_action_reuses_browser_allowed_domains_policy(manager):
    """BrowserTool (tool_integration/adapters/browser.py) adoptó este
    mecanismo — BROWSE reusa browser.allowed_domains, DOWNLOAD reusa
    downloads.allowed_domains (listas independientes, cada una con su
    propia semántica)."""
    assert manager.evaluate("browser", NetworkScope.INTERNET, NetworkAction.BROWSE, "example.com") == "auto_allowed"
    assert manager.evaluate("browser", NetworkScope.INTERNET, NetworkAction.BROWSE, "unsplash.com") == "requires_approval"
    assert manager.evaluate("browser", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "example.com") == "requires_approval"


# --- Solicitudes pendientes ---


def test_pending_request_appears_in_list_pending(manager):
    pending = manager.create_pending_request("vscode_integration", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")
    assert pending in manager.list_pending()


def test_approving_an_unknown_request_raises_a_clear_error(manager):
    with pytest.raises(NetworkAccessError, match="no-existe"):
        manager.approve("no-existe", level="once")


# --- Las 4 escalas de concesión ---


def test_once_level_never_persists_the_grant(manager):
    pending = manager.create_pending_request("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")
    manager.approve(pending.id, level="once")
    assert manager.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com") == "requires_approval"


def test_project_level_grant_is_remembered_for_the_exact_domain(manager):
    pending = manager.create_pending_request("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")
    manager.approve(pending.id, level="project")

    assert manager.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com") == "auto_allowed"
    assert manager.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "otro-evil.com") == "requires_approval"


def test_project_level_grant_survives_a_new_instance_over_the_same_file(tmp_path):
    grants_path = tmp_path / "network_grants.json"
    first = NetworkAccessManager(grants_path=grants_path)
    pending = first.create_pending_request("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")
    first.approve(pending.id, level="project")

    second = NetworkAccessManager(grants_path=grants_path)
    assert second.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com") == "auto_allowed"


# --- Auditoría ---


def test_escalated_decision_is_audited(manager):
    manager.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "evil.com")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "network_access_requested" in event_types
    assert "network_access_escalated" in event_types


def test_auto_allowed_decision_is_audited(manager):
    manager.evaluate("skill_x", NetworkScope.INTERNET, NetworkAction.DOWNLOAD, "unsplash.com")

    event_types = [e["event_type"] for e in audit_log.tail(10)]
    assert "network_access_granted" in event_types

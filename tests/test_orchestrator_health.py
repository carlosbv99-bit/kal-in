"""
Tests de /status — en particular el caché de verify_chain() agregado
por M-7 (auditoría externa Likay-OS, 2026-09-26): audit.log nunca rota
(append-only a propósito), así que verificar la cadena completa en
CADA poll de /status (sin token admin, consultado periódicamente por
el frontend) es un costo que crece sin límite con la vida del proceso.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.orchestrator import app
from agent_core.routers import health as health_module

client = TestClient(app, base_url="http://localhost")


def test_status_caches_audit_chain_verified_within_the_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_verify_chain():
        calls["n"] += 1
        return True

    monkeypatch.setattr(health_module.audit_log, "verify_chain", fake_verify_chain)
    monkeypatch.setitem(health_module._audit_verify_cache, "checked_at", 0.0)

    client.get("/status")
    client.get("/status")
    client.get("/status")

    assert calls["n"] == 1


def test_status_recomputes_audit_chain_verified_after_the_ttl_expires(monkeypatch):
    calls = {"n": 0}

    def fake_verify_chain():
        calls["n"] += 1
        return True

    monkeypatch.setattr(health_module.audit_log, "verify_chain", fake_verify_chain)
    monkeypatch.setitem(health_module._audit_verify_cache, "checked_at", 0.0)

    client.get("/status")
    monkeypatch.setitem(health_module._audit_verify_cache, "checked_at", 0.0)
    client.get("/status")

    assert calls["n"] == 2

"""
Tests de B-6 (auditoría externa Likay-OS, 2026-09-26): /docs, /redoc y
/openapi.json deben apagarse cuando AGENT_ENV=production — antes,
AGENT_ENV existía en .env.example pero ningún código lo leía de verdad.
"""
from __future__ import annotations

from agent_core.orchestrator import _is_production_env, app


def test_development_is_the_default_and_keeps_the_api_catalog_enabled(monkeypatch):
    monkeypatch.delenv("AGENT_ENV", raising=False)
    assert _is_production_env() is False


def test_agent_env_production_is_detected_case_insensitively(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "PRODUCTION")
    assert _is_production_env() is True


def test_agent_env_anything_else_is_treated_as_development(monkeypatch):
    monkeypatch.setenv("AGENT_ENV", "staging")
    assert _is_production_env() is False


def test_the_running_app_serves_the_api_catalog_by_default():
    """
    La app ya se construyó al importar el módulo, con el AGENT_ENV real
    de este proceso de test (nunca "production") — confirma que el
    catálogo sigue disponible para el uso normal de desarrollo, no solo
    que la función pura _is_production_env() da el resultado esperado.
    """
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"

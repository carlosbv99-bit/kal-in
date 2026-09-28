"""
Tests de GET /tools/{name}/versions — B-5 (auditoría externa Likay-OS,
2026-09-26): un nombre de herramienta con un charset inválido rutea
hasta VersionStore._tool_dir() (kernel/registry/versioning.py), que
lanza ValueError — sin capturarlo en el router, eso llegaba como 500
crudo en vez de un 400 de error de cliente.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.orchestrator import app

client = TestClient(app, base_url="http://localhost")


def test_an_invalid_tool_name_returns_400_not_500():
    response = client.get("/tools/NOMBRE-INVALIDO-MAYUSCULAS/versions")
    assert response.status_code == 400


def test_a_name_with_disallowed_characters_returns_400_not_500():
    response = client.get("/tools/nombre con espacios y ñ/versions")
    assert response.status_code == 400


def test_a_valid_but_unknown_tool_name_returns_200_with_empty_versions():
    response = client.get("/tools/una-herramienta-que-no-existe/versions")
    assert response.status_code == 200
    assert response.json() == {"name": "una-herramienta-que-no-existe", "versions": []}

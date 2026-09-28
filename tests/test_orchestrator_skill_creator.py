"""
Tests de los endpoints /skill-proposals/* (agent_core/routers/skill_creator.py).

El gate de token administrativo sobre approve/reject ya se cubre en
tests/test_orchestrator_admin_auth.py — acá se prueba el contenido de
las respuestas, con un SkillCreatorManager aislado (tmp_path) inyectado
en el módulo del router — mismo criterio que ya rompió en su momento
con agent_core.orchestrator tras partirlo en routers (ver
docs/HISTORY.md): el mock tiene que apuntar al módulo que HACE el
import, no a donde la función vivía antes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.orchestrator import _ADMIN_TOKEN, app
from agent_core.routers import skill_creator as skill_creator_router
from agent_core.skill_creator import SkillCreatorManager

client = TestClient(app, base_url="http://localhost")
_HEADERS = {"X-Kal-Admin-Token": _ADMIN_TOKEN}

_VALID_CODE = (
    "from sdk.skill import Tool\n"
    "from sdk.artifacts import Artifact\n\n\n"
    "class WebScraperTool(Tool):\n"
    "    def execute(self, **kwargs):\n"
    "        return Artifact(modality='text', uri='', metadata={'summary': 'ok'})\n"
)


@pytest.fixture
def isolated_manager(monkeypatch, tmp_path):
    manager = SkillCreatorManager(
        staging_root=tmp_path / "proposed_skills",
        skills_root=tmp_path / "skills",
        key_dir=tmp_path / "keys",
    )
    monkeypatch.setattr(skill_creator_router, "skill_creator_manager", manager)
    return manager


def test_list_when_empty(isolated_manager):
    response = client.get("/skill-proposals")
    assert response.status_code == 200
    assert response.json() == []


def test_list_reflects_a_real_proposal(isolated_manager):
    isolated_manager.propose(
        name="web_scraper", description="Descarga una URL", class_name="WebScraperTool",
        code=_VALID_CODE, justification="lo pidieron varias veces",
    )
    response = client.get("/skill-proposals")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "web_scraper"
    assert body[0]["status"] == "pending_human_approval"
    assert "code" not in body[0]  # el listado es un resumen, no el detalle completo


def test_get_detail_includes_the_full_code(isolated_manager):
    proposal = isolated_manager.propose(
        name="web_scraper", description="Descarga una URL", class_name="WebScraperTool",
        code=_VALID_CODE, justification="lo pidieron varias veces",
    )
    response = client.get(f"/skill-proposals/{proposal.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == _VALID_CODE
    assert body["entry_point"] == "tool:WebScraperTool"


def test_get_detail_unknown_id_is_404(isolated_manager):
    response = client.get("/skill-proposals/no-existe")
    assert response.status_code == 404


def test_approve_requires_admin_token(isolated_manager):
    proposal = isolated_manager.propose(
        name="web_scraper", description="d", class_name="WebScraperTool",
        code=_VALID_CODE, justification="j",
    )
    response = client.post(f"/skill-proposals/{proposal.id}/approve", json={"approved_by": "alguien"})
    assert response.status_code == 401


def test_approve_with_valid_token_installs_it_disabled_and_signed(isolated_manager):
    from kernel.registry.skill_signing import verify_skill_signature

    proposal = isolated_manager.propose(
        name="web_scraper", description="d", class_name="WebScraperTool",
        code=_VALID_CODE, justification="j",
    )
    response = client.post(
        f"/skill-proposals/{proposal.id}/approve", json={"approved_by": "kalin"}, headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    final_dir = isolated_manager.skills_root / "web_scraper"
    assert final_dir.exists()
    assert verify_skill_signature(final_dir) == "verified"


def test_approve_unknown_id_is_400_not_500(isolated_manager):
    response = client.post("/skill-proposals/no-existe/approve", json={"approved_by": "kalin"}, headers=_HEADERS)
    assert response.status_code == 400


def test_reject_requires_admin_token(isolated_manager):
    proposal = isolated_manager.propose(
        name="web_scraper", description="d", class_name="WebScraperTool",
        code=_VALID_CODE, justification="j",
    )
    response = client.post(f"/skill-proposals/{proposal.id}/reject", json={})
    assert response.status_code == 401


def test_reject_with_valid_token_discards_it(isolated_manager):
    proposal = isolated_manager.propose(
        name="web_scraper", description="d", class_name="WebScraperTool",
        code=_VALID_CODE, justification="j",
    )
    response = client.post(
        f"/skill-proposals/{proposal.id}/reject", json={"reason": "no hace falta"}, headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert not (isolated_manager.skills_root / "web_scraper").exists()

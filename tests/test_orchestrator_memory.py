"""
Tests de DELETE /memory/{tier}/{item_id} y DELETE /memory (2026-07-27)
— derecho al olvido del Memory Security Policy Engine (Fase 1): conecta
el forget()/forget_matching() de agent_core/memory/manager.py, que
antes no tenía ningún endpoint que los expusiera.

`orchestrator.memory` mockeado — no se ejercita ningún backend real
(chromadb/sqlite), mismo patrón que test_orchestrator_chat_progress.py.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core import orchestrator as orchestrator_module
from agent_core.orchestrator import _ADMIN_TOKEN, app

client = TestClient(app, base_url="http://localhost")
# C-4 (auditoría externa Likay-OS, 2026-09-26): estos endpoints ahora
# exigen token admin — ver agent_core/routers/memory.py.
_HEADERS = {"X-Kal-Admin-Token": _ADMIN_TOKEN}


class _FakeMemoryManager:
    def __init__(self, forget_matching_return: int = 0, raise_on_forget: Exception | None = None):
        self.forgotten_calls: list[tuple[str, str]] = []
        self.forget_matching_calls: list[dict] = []
        self._forget_matching_return = forget_matching_return
        self._raise_on_forget = raise_on_forget

    def forget(self, item_id: str, tier: str) -> None:
        if self._raise_on_forget is not None:
            raise self._raise_on_forget
        self.forgotten_calls.append((item_id, tier))

    def forget_matching(self, keyword=None, tier=None, classification=None, before=None, after=None) -> int:
        if self._raise_on_forget is not None:
            raise self._raise_on_forget
        self.forget_matching_calls.append(
            {"keyword": keyword, "tier": tier, "classification": classification, "before": before, "after": after}
        )
        return self._forget_matching_return


def test_delete_a_single_item_delegates_to_memory_manager_forget(monkeypatch):
    fake = _FakeMemoryManager()
    monkeypatch.setattr(orchestrator_module.orchestrator, "memory", fake)

    response = client.delete("/memory/long_term/algun-id", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"deleted": "algun-id", "tier": "long_term"}
    assert fake.forgotten_calls == [("algun-id", "long_term")]


def test_delete_a_single_item_with_an_invalid_tier_returns_400(monkeypatch):
    fake = _FakeMemoryManager(raise_on_forget=ValueError("Nivel de memoria inválido: 'x'"))
    monkeypatch.setattr(orchestrator_module.orchestrator, "memory", fake)

    response = client.delete("/memory/x/algun-id", headers=_HEADERS)

    assert response.status_code == 400


def test_bulk_delete_with_a_keyword_filter(monkeypatch):
    fake = _FakeMemoryManager(forget_matching_return=3)
    monkeypatch.setattr(orchestrator_module.orchestrator, "memory", fake)

    response = client.delete("/memory", params={"keyword": "openai"}, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"deleted_count": 3}
    assert fake.forget_matching_calls == [
        {"keyword": "openai", "tier": None, "classification": None, "before": None, "after": None}
    ]


def test_bulk_delete_without_any_filter_returns_400():
    """
    Nunca un "borrar todo" accidental de un único llamado sin
    parámetros — MemoryManager.forget_matching() se protege a sí mismo
    (ValueError), el router lo traduce a 400.
    """
    response = client.delete("/memory", headers=_HEADERS)

    assert response.status_code == 400


def test_bulk_delete_passes_through_all_supported_filters(monkeypatch):
    fake = _FakeMemoryManager(forget_matching_return=1)
    monkeypatch.setattr(orchestrator_module.orchestrator, "memory", fake)

    response = client.delete(
        "/memory",
        params={"tier": "mid_term", "classification": "secret", "before": "1000", "after": "500"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    assert fake.forget_matching_calls == [
        {"keyword": None, "tier": "mid_term", "classification": "secret", "before": 1000.0, "after": 500.0}
    ]

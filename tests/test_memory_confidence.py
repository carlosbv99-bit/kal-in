"""
Tests del nivel de confianza de memoria (agent_core/memory/base.py::
MemoryConfidence), ortogonal al horizonte temporal (corto/mediano/largo).

Los tests que tocan long_term/manager siguen el mismo criterio que
test_memory_long_term.py y test_memory_manager.py: se saltan si
chromadb/sentence-transformers no están disponibles o el modelo de
embeddings no puede cargarse (p.ej. sin red la primera vez).
"""
from __future__ import annotations

import pytest

from agent_core.memory.base import MemoryConfidence, MemoryItem
from agent_core.memory.mid_term import MidTermMemory


def test_default_confidence_is_temporal():
    item = MemoryItem(content="algo")
    assert item.confidence == MemoryConfidence.TEMPORAL


# --- MidTermMemory: persistencia de confidence ---


@pytest.fixture
def mid_term(tmp_path):
    return MidTermMemory(db_path=tmp_path / "test_memory.db")


def test_mid_term_store_and_retrieve_round_trips_confidence(mid_term):
    item = MemoryItem(content="dato aprendido", confidence=MemoryConfidence.APRENDIDA)
    mid_term.store(item)

    retrieved = mid_term.get_by_id(item.id)
    assert retrieved.confidence == MemoryConfidence.APRENDIDA


def test_mid_term_defaults_to_temporal_when_not_specified(mid_term):
    item = MemoryItem(content="dato crudo")
    mid_term.store(item)

    retrieved = mid_term.get_by_id(item.id)
    assert retrieved.confidence == MemoryConfidence.TEMPORAL


def test_purge_expired_never_removes_permanente_items(mid_term):
    old_item = MemoryItem(content="hecho fijado", confidence=MemoryConfidence.PERMANENTE)
    old_item.created_at = 0.0  # arbitrariamente viejo, superaría cualquier TTL
    mid_term.store(old_item)

    other_old_item = MemoryItem(content="dato viejo cualquiera")
    other_old_item.created_at = 0.0
    mid_term.store(other_old_item)

    purged = mid_term.purge_expired()

    assert purged == 1
    assert mid_term.get_by_id(old_item.id) is not None
    assert mid_term.get_by_id(other_old_item.id) is None


# --- MemoryManager: verify()/pin()/promote() ---

pytest.importorskip("chromadb")
pytest.importorskip("sentence_transformers")

from agent_core.memory.manager import MemoryManager
from agent_core.memory.short_term import ShortTermMemory
from utils.config import settings


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.memory.long_term, "persist_path", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings.memory.long_term, "mode", "embedded")

    mgr = MemoryManager(
        short_term=ShortTermMemory(max_tokens=10_000),
        mid_term=MidTermMemory(db_path=tmp_path / "mid_term.db"),
    )
    try:
        mgr.long_term._get_embedder()
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo inicializar el modelo de embeddings: {e}")
    return mgr


def test_promote_tags_learned_items_as_aprendida(manager):
    cfg = settings.memory.long_term.promotion
    item = MemoryItem(
        content="patrón repetido",
        repetitions=cfg.min_repetitions,
        relevance_score=cfg.min_relevance_score,
    )
    manager.mid_term.store(item)

    manager.promote_mid_to_long()

    promoted = manager.long_term.get_by_id(item.id)
    assert promoted.confidence == MemoryConfidence.APRENDIDA


def test_promote_does_not_downgrade_already_verified_item(manager):
    cfg = settings.memory.long_term.promotion
    item = MemoryItem(
        content="dato ya confirmado por un humano",
        repetitions=cfg.min_repetitions,
        relevance_score=cfg.min_relevance_score,
        confidence=MemoryConfidence.VERIFICADA,
    )
    manager.mid_term.store(item)

    manager.promote_mid_to_long()

    promoted = manager.long_term.get_by_id(item.id)
    assert promoted.confidence == MemoryConfidence.VERIFICADA


def test_verify_upgrades_confidence_and_records_who(manager):
    # remember() guarda en short_term, no en mid_term: para verify() hace
    # falta un item que ya viva en mid_term/long_term (ver docstring de
    # MemoryManager.verify).
    stored = MemoryItem(content="dato a verificar")
    manager.mid_term.store(stored)

    verified = manager.verify(stored.id, tier="mid_term", verified_by="kalin")

    assert verified.confidence == MemoryConfidence.VERIFICADA
    assert verified.metadata["verified_by"] == "kalin"
    assert manager.mid_term.get_by_id(stored.id).confidence == MemoryConfidence.VERIFICADA


def test_pin_upgrades_confidence_to_permanente(manager):
    stored = MemoryItem(content="hecho base sobre el usuario")
    manager.mid_term.store(stored)

    pinned = manager.pin(stored.id, tier="mid_term")

    assert pinned.confidence == MemoryConfidence.PERMANENTE
    assert manager.mid_term.get_by_id(stored.id).confidence == MemoryConfidence.PERMANENTE


def test_verify_unknown_item_raises(manager):
    with pytest.raises(ValueError):
        manager.verify("no-existe", tier="mid_term", verified_by="kalin")


def test_verify_invalid_tier_raises(manager):
    stored = MemoryItem(content="algo")
    manager.mid_term.store(stored)
    with pytest.raises(ValueError):
        manager.verify(stored.id, tier="short_term", verified_by="kalin")

"""
Test de integración de agent_core/memory/manager.py: el ciclo completo
corto -> mediano -> largo plazo, usando los tres backends reales pero
apuntando a rutas temporales (nunca data/ real del proyecto).

Se salta si chromadb/sentence-transformers no están disponibles o si el
modelo de embeddings no puede cargarse (mismo criterio que
test_memory_long_term.py).
"""
from __future__ import annotations

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("sentence_transformers")

from agent_core.memory.base import MemoryItem
from agent_core.memory.manager import MemoryManager
from agent_core.memory.mid_term import MidTermMemory
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


def test_remember_stores_in_short_term_only(manager):
    manager.remember("dato reciente")

    recall = manager.recall("dato reciente")
    assert len(recall["short_term"]) == 1
    assert len(recall["mid_term"]) == 0
    assert len(recall["long_term"]) == 0


def test_consolidate_moves_short_to_mid_and_empties_short(manager):
    manager.remember("evento uno")
    manager.remember("evento dos")

    moved = manager.consolidate_short_to_mid()
    assert moved == 2

    recall = manager.recall("evento")
    assert len(recall["short_term"]) == 0
    assert len(recall["mid_term"]) == 2


def test_consolidate_with_summarizer_transforms_content(manager):
    manager.remember("contenido original largo y detallado")

    def fake_summarizer(text: str) -> str:
        return "RESUMEN: " + text[:10]

    manager.consolidate_short_to_mid(summarizer=fake_summarizer)

    recall = manager.recall("RESUMEN")
    assert len(recall["mid_term"]) == 1
    assert recall["mid_term"][0].content.startswith("RESUMEN:")


def test_promote_mid_to_long_only_moves_items_meeting_threshold(manager):
    cfg = settings.memory.long_term.promotion

    qualifies = MemoryItem(
        content="patrón repetido relevante",
        repetitions=cfg.min_repetitions,
        relevance_score=cfg.min_relevance_score,
    )
    does_not_qualify = MemoryItem(
        content="patrón poco repetido",
        repetitions=1,
        relevance_score=0.1,
    )
    manager.mid_term.store(qualifies)
    manager.mid_term.store(does_not_qualify)

    promoted = manager.promote_mid_to_long()
    assert promoted == 1

    recall = manager.recall("patrón", top_k=10)
    long_term_contents = [i.content for i in recall["long_term"]]
    assert "patrón repetido relevante" in long_term_contents
    assert "patrón poco repetido" not in long_term_contents


def test_promotion_does_not_delete_from_mid_term(manager):
    """
    La promoción es una copia, no un move: purge_expired() (por TTL) es
    el único mecanismo que borra de mediano plazo, de forma
    independiente. Confirma que promote_mid_to_long no borra por sí solo.
    """
    cfg = settings.memory.long_term.promotion
    item = MemoryItem(
        content="item promovido pero no borrado",
        repetitions=cfg.min_repetitions,
        relevance_score=cfg.min_relevance_score,
    )
    manager.mid_term.store(item)
    manager.promote_mid_to_long()

    still_in_mid_term = manager.mid_term.retrieve("promovido")
    assert len(still_in_mid_term) == 1


def test_full_cycle_short_to_mid_to_long(manager):
    """
    Simula el ciclo completo: algo entra por corto plazo, se consolida
    a mediano, se marca como repetido/relevante manualmente (en
    producción esto lo haría la lógica de negocio), y se promueve a
    largo plazo.
    """
    manager.remember("información que terminará en memoria permanente")
    manager.consolidate_short_to_mid()

    # Simula que este ítem alcanzó el umbral de promoción tras
    # aparecer varias veces en distintas tareas.
    cfg = settings.memory.long_term.promotion
    # mid_term usa LIKE simple (substring literal), no búsqueda semántica
    # (ver TODO en mid_term.py) — por eso el query debe ser un substring
    # exacto del contenido guardado, a diferencia del query usado más
    # abajo contra long_term, que sí es semántico.
    mid_results = manager.mid_term.retrieve("memoria permanente")
    assert len(mid_results) == 1
    item = mid_results[0]
    item.repetitions = cfg.min_repetitions
    item.relevance_score = cfg.min_relevance_score
    manager.mid_term.store(item)  # re-store actualiza (INSERT OR REPLACE)

    promoted = manager.promote_mid_to_long()
    assert promoted == 1

    long_term_results = manager.long_term.retrieve("dato permanente en memoria")
    assert len(long_term_results) == 1

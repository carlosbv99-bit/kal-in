"""
Tests de agent_core/memory/long_term.py.

Se saltan si chromadb o sentence-transformers no están instalados, y
también si el modelo de embeddings no puede cargarse (p.ej. primera vez
sin red para descargarlo desde HuggingFace Hub — ver docstring de
long_term.py). No se fuerza la descarga como parte de la suite normal
de tests para no depender de red en CI.

Usa un directorio temporal por test para el índice persistido, así
nunca toca data/long_term/chroma_persist real del proyecto.
"""
from __future__ import annotations

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("sentence_transformers")

from agent_core.memory.base import MemoryItem
from agent_core.memory.long_term import LongTermMemory
from utils.config import settings


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.memory.long_term, "persist_path", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings.memory.long_term, "mode", "embedded")
    try:
        instance = LongTermMemory()
        instance._get_embedder()  # fuerza la carga/descarga ahora, no en el primer test
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo inicializar el modelo de embeddings (¿sin red la primera vez?): {e}")
    return instance


def test_store_and_retrieve_semantically_similar_item(mem):
    mem.store(MemoryItem(content="el perro corre por el parque"))
    mem.store(MemoryItem(content="la bolsa de valores cayó hoy"))

    results = mem.retrieve("un can jugando afuera", top_k=1)

    assert len(results) == 1
    assert "perro" in results[0].content


def test_retrieve_on_empty_collection_returns_empty_list(mem):
    assert mem.retrieve("cualquier cosa") == []


def test_retrieve_respects_top_k(mem):
    for i in range(5):
        mem.store(MemoryItem(content=f"documento número {i} sobre gatos"))

    results = mem.retrieve("gatos", top_k=2)
    assert len(results) == 2


def test_forget_removes_item(mem):
    item = MemoryItem(content="información temporal a olvidar")
    mem.store(item)
    mem.forget(item.id)

    results = mem.retrieve("información temporal")
    ids = [r.id for r in results]
    assert item.id not in ids


def test_metadata_with_nested_structures_is_flattened_not_lost(mem):
    item = MemoryItem(
        content="item con metadata compleja",
        metadata={"tags": ["a", "b"], "nested": {"x": 1}, "simple": "ok", "none_value": None},
    )
    mem.store(item)

    results = mem.retrieve("metadata compleja", top_k=1)
    assert len(results) == 1
    # Los valores no planos se serializan a string, no se pierden ni rompen el store
    assert "tags" in results[0].metadata
    assert "nested" in results[0].metadata
    assert results[0].metadata["simple"] == "ok"
    assert "none_value" not in results[0].metadata  # None se descarta explícitamente


def test_store_artifact_reference_indexes_by_description(mem):
    mem.store_artifact_reference(
        description="atardecer naranja sobre montañas nevadas",
        artifact_uri="data/artifacts/images/abc123.png",
        modality="image",
        metadata={"prompt": "atardecer naranja sobre montañas nevadas"},
    )

    results = mem.retrieve("puesta de sol en la sierra", top_k=1)
    assert len(results) == 1
    assert results[0].metadata["artifact_uri"] == "data/artifacts/images/abc123.png"
    assert results[0].metadata["modality"] == "image"


def test_upsert_same_id_replaces_not_duplicates(mem):
    item = MemoryItem(id="fixed-id", content="versión original")
    mem.store(item)
    mem.store(MemoryItem(id="fixed-id", content="versión actualizada"))

    results = mem.retrieve("versión", top_k=10)
    matching = [r for r in results if r.id == "fixed-id"]
    assert len(matching) == 1
    assert matching[0].content == "versión actualizada"


def test_list_all_returns_every_item_unlike_retrieve_which_is_semantic_top_k(mem):
    mem.store(MemoryItem(content="un tema totalmente distinto sobre cocina"))
    mem.store(MemoryItem(content="otro tema, esta vez sobre astronomía"))

    assert len(mem.list_all()) == 2


def test_list_all_on_empty_collection_returns_empty_list(mem):
    assert mem.list_all() == []

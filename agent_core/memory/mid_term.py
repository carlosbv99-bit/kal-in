"""
Memoria de mediano plazo: episodios recientes con TTL.

Guarda resúmenes de tareas, pares error->reparación, resultados
intermedios. Backend por defecto: SQLite (ver config.yaml,
memory.mid_term.backend). Cambiar a Postgres implica implementar
otra clase con la misma interfaz MemoryBackend, no tocar consumidores.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from agent_core.memory.base import MemoryBackend, MemoryConfidence, MemoryItem
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

DB_PATH = Path("data/mid_term/memory.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class MidTermMemory(MemoryBackend):
    def __init__(self, db_path: Path = DB_PATH):
        self.ttl_seconds = settings.memory.mid_term.ttl_days * 86400
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at REAL NOT NULL,
                relevance_score REAL DEFAULT 0.0,
                repetitions INTEGER DEFAULT 1,
                confidence TEXT DEFAULT 'temporal'
            )
            """
        )
        self.conn.commit()
        try:
            # Migración para bases de datos creadas antes de que existiera
            # esta columna (data/mid_term/memory.db ya existente).
            self.conn.execute(
                "ALTER TABLE memory_items ADD COLUMN confidence TEXT DEFAULT 'temporal'"
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # la columna ya existe

    def store(self, item: MemoryItem) -> None:
        """
        VULNERABILIDAD/BUG REAL ENCONTRADO EN AUDITORÍA EXTERNA
        (Likay-OS, 2026-09-26), A-6: nada en todo el código incrementaba
        jamás `repetitions` ni `relevance_score` más allá de sus
        defaults (1 y 0.0) — candidates_for_promotion() exige
        repetitions >= 3 AND relevance_score >= 0.75 (config.yaml), un
        umbral que ninguna fila podía cruzar NUNCA. Consecuencia real
        doble: (1) promote_mid_to_long() nunca promovió NADA desde que
        existe — la "memoria de largo plazo" nunca se activó de
        verdad; (2) la redacción de credenciales antes de persistir
        para siempre (classify()/redact() en promote_mid_to_long(), ver
        agent_core/memory/manager.py) es la ÚNICA barrera de seguridad
        de ese camino, y quedaba sin poder ejecutarse jamás — dead code
        de seguridad.

        Fix mínimo y honesto: cuando el mismo contenido EXACTO ya
        existe en mediano plazo (p.ej. el mismo resumen de tarea o
        patrón de error->reparación consolidado más de una vez), se
        trata como una repetición real del mismo patrón — se
        incrementa `repetitions` y `relevance_score` (tope 1.0) sobre
        la fila EXISTENTE en vez de insertar una fila nueva sin
        relación. Deliberadamente NO se inventa un scorer de relevancia
        semántica (no existe ninguno en este código hoy, y no es parte
        de este fix) — "relevancia" acá es, literalmente, cuántas veces
        se repitió el mismo contenido.
        """
        existing = self.conn.execute(
            "SELECT id, relevance_score, repetitions, metadata FROM memory_items WHERE content = ?",
            (item.content,),
        ).fetchone()
        if existing is not None:
            existing_id, existing_relevance, existing_repetitions, existing_metadata_json = existing
            merged_metadata = {**json.loads(existing_metadata_json), **item.metadata}
            self.conn.execute(
                """
                UPDATE memory_items
                SET repetitions = ?, relevance_score = ?, metadata = ?, confidence = ?
                WHERE id = ?
                """,
                (
                    existing_repetitions + item.repetitions,
                    min(1.0, existing_relevance + 0.25 * item.repetitions),
                    json.dumps(merged_metadata),
                    item.confidence.value,
                    existing_id,
                ),
            )
        else:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO memory_items
                    (id, content, metadata, created_at, relevance_score, repetitions, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.content,
                    json.dumps(item.metadata),
                    item.created_at,
                    item.relevance_score,
                    item.repetitions,
                    item.confidence.value,
                ),
            )
        self.conn.commit()

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        # TODO: búsqueda por similitud de texto (FTS5) en vez de LIKE simple.
        cursor = self.conn.execute(
            """
            SELECT id, content, metadata, created_at, relevance_score, repetitions, confidence
            FROM memory_items
            WHERE content LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (f"%{query}%", top_k),
        )
        return [self._row_to_item(row) for row in cursor.fetchall()]

    def get_by_id(self, item_id: str) -> MemoryItem | None:
        """
        Lookup exacto por id, a diferencia de retrieve() que busca por
        coincidencia de texto (LIKE). Necesario para checkpoints (ver
        error_handling/strategies.py:RuntimeErrorStrategy) y para
        verify()/pin() de MemoryManager, donde se necesita recuperar un
        item específico por su clave, no por similitud de contenido.
        """
        cursor = self.conn.execute(
            """
            SELECT id, content, metadata, created_at, relevance_score, repetitions, confidence
            FROM memory_items WHERE id = ?
            """,
            (item_id,),
        )
        row = cursor.fetchone()
        return self._row_to_item(row) if row is not None else None

    def forget(self, item_id: str) -> None:
        self.conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        self.conn.commit()

    def list_all(self) -> list[MemoryItem]:
        """Para el borrado masivo con filtros (ver
        MemoryManager.forget_matching()) — a diferencia de retrieve(),
        que filtra por texto y limita a top_k, esto devuelve TODO."""
        cursor = self.conn.execute(
            "SELECT id, content, metadata, created_at, relevance_score, repetitions, confidence FROM memory_items"
        )
        return [self._row_to_item(row) for row in cursor.fetchall()]

    def purge_expired(self) -> int:
        """
        Job periódico: elimina items que superaron el TTL configurado.
        Excluye confidence='permanente': un item pinneado explícitamente
        por un humano nunca se purga por TTL, sin importar su antigüedad.
        """
        cutoff = time.time() - self.ttl_seconds
        cursor = self.conn.execute(
            "DELETE FROM memory_items WHERE created_at < ? AND confidence != ?",
            (cutoff, MemoryConfidence.PERMANENTE.value),
        )
        self.conn.commit()
        logger.info(f"mid_term: purgados {cursor.rowcount} items expirados")
        return cursor.rowcount

    def candidates_for_promotion(self) -> list[MemoryItem]:
        """
        Devuelve items que cumplen el umbral de promoción a largo plazo,
        definido en config.yaml (memory.long_term.promotion). La promoción
        efectiva la orquesta agent_core/orchestrator.py, no este módulo.
        """
        cfg = settings.memory.long_term.promotion
        cursor = self.conn.execute(
            """
            SELECT id, content, metadata, created_at, relevance_score, repetitions, confidence
            FROM memory_items
            WHERE repetitions >= ? AND relevance_score >= ?
            """,
            (cfg.min_repetitions, cfg.min_relevance_score),
        )
        return [self._row_to_item(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_item(row) -> MemoryItem:
        return MemoryItem(
            id=row[0], content=row[1], metadata=json.loads(row[2]),
            created_at=row[3], relevance_score=row[4], repetitions=row[5],
            confidence=MemoryConfidence(row[6] or "temporal"),
        )

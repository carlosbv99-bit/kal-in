"""
Interfaz común que deben implementar los tres niveles de memoria.

El resto del sistema (orchestrator, task_execution, tool_integration)
depende SOLO de esta interfaz, nunca de un backend concreto. Esto permite
cambiar SQLite por Postgres o Chroma por Qdrant sin tocar código que
consume memoria.
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryConfidence(str, Enum):
    """
    Nivel de confianza de un recuerdo, ORTOGONAL al horizonte temporal
    (corto/mediano/largo plazo, que sigue viviendo en qué backend lo
    almacena). No todo lo que el agente aprende merece el mismo crédito:

    - TEMPORAL: dato crudo, todavía sin corroborar, típicamente en
      corto plazo (default de remember()).
    - PERMANENTE: fijado explícitamente por un humano (pin()) — nunca
      se purga por TTL, se trata como hecho base.
    - VERIFICADA: un humano confirmó explícitamente este dato (verify()).
    - APRENDIDA: patrón auto-inferido por repetición/relevancia (lo que
      hoy hace promote_mid_to_long()) — nadie lo confirmó, es una
      inferencia del propio agente.
    - EXTERNA: proviene de una fuente externa (web, archivo, API), no
      de una conversación con el usuario ni de inferencia propia.
    """

    TEMPORAL = "temporal"
    PERMANENTE = "permanente"
    VERIFICADA = "verificada"
    APRENDIDA = "aprendida"
    EXTERNA = "externa"


@dataclass
class MemoryItem:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # relevancia acumulada, usada por la política de promoción mediano->largo
    relevance_score: float = 0.0
    repetitions: int = 1
    confidence: MemoryConfidence = MemoryConfidence.TEMPORAL


class MemoryBackend(ABC):
    """Contrato que implementan ShortTermMemory, MidTermMemory, LongTermMemory."""

    @abstractmethod
    def store(self, item: MemoryItem) -> None:
        ...

    @abstractmethod
    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        ...

    @abstractmethod
    def forget(self, item_id: str) -> None:
        ...

    def consolidate(self) -> list[MemoryItem]:
        """
        Resume/promueve contenido hacia el nivel superior de memoria.
        No todos los backends lo implementan (short_term sí, long_term no
        tiene a dónde promoverse). Default: no-op.
        """
        return []

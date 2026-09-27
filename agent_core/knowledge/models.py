"""
Pattern: la forma de un "Knowledge" (conocimiento operativo del
sistema) — una REGLA inferida de varios eventos relacionados, no un
evento individual. Ver agent_core/knowledge/__init__.py.

Deliberadamente ya tiene su forma final (incluido `recommendation`,
no solo `summary`) aunque nada lo produzca todavía — mismo criterio
de "infraestructura preparada": barato de dejar listo ahora, costoso
de agregarle un campo después si ya hay Patterns reales guardados.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Pattern:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    members: list[str] = field(default_factory=list)  # ids de MemoryItem que forman este patrón
    confidence: float = 0.0
    summary: str = ""
    recommendation: str = ""
    last_seen: float = field(default_factory=time.time)

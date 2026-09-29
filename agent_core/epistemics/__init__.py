"""
Adaptador de kal-in sobre `kal_epistemics` (el subpaquete portable).

Separación de responsabilidades, que es lo que hace que la librería sirva para
cualquier agente:

- `kal_epistemics/` — contratos y lógica determinista. No sabe nada de kal-in,
  no importa `agent_core`, no llama a ningún modelo.
- `agent_core/epistemics/` (este paquete) — el PEGAMENTO: ledger por sesión,
  ingestión de observaciones con procedencia, y el bloque de contexto. Es acá
  donde vive la única decisión que la librería no puede tomar por sí sola:
  **qué es una observación del mundo y qué es texto que escribió el modelo.**
"""
from __future__ import annotations

from agent_core.epistemics.sanitize import (
    SanitizedEvidence,
    active_provider,
    sanitize_for_context,
    sanitize_text,
)
from agent_core.epistemics.store import (
    EXTERNAL_TEXT_TOOLS,
    OBSERVATION_TOOLS,
    EpistemicStore,
    Ingestion,
    almacen_persistente,
)

__all__ = [
    "EXTERNAL_TEXT_TOOLS",
    "OBSERVATION_TOOLS",
    "almacen_persistente",
    "EpistemicStore",
    "Ingestion",
    "SanitizedEvidence",
    "active_provider",
    "sanitize_for_context",
    "sanitize_text",
]

"""
kal_epistemics — capa epistémica portable de kal-in.

Qué resuelve: que "no lo sé" sea un estado almacenado y no un fallo, y que
una hipótesis conservada sirva en el futuro en vez de acumularse como texto
muerto.

Qué es y qué no es, porque la diferencia decide si esto sirve o es
decoración:

- **Es una librería de contratos y lógica determinista.** No llama a ningún
  modelo, no hace red, no tiene dependencias: 100% stdlib, mismo criterio que
  `sdk/` — se puede copiar dentro de otro agente (o de un contenedor) sin
  arrastrar nada.
- **NO es un gate por sí sola.** La decisión de reabrir y el cálculo del
  nivel epistémico son mecánicos y viven acá; la decisión de *degradar una
  afirmación antes de que salga* tiene que vivir en la ruta de salida del
  agente que la use. Un gate que el modelo puede saltear no es un gate, así
  que esta librería nunca debe presentarse como garantía por sí misma: es el
  mecanismo, el host es el que lo impone.
- **Sus hipótesis NUNCA son conocimiento.** No hay ninguna API acá para
  meterlas en el camino de recall() del agente, y no debería haberla: una
  hipótesis plausible recuperada por similaridad semántica y sin marca de
  origen es una alucinación persistente con procedencia, que además el
  sistema cita.

Estado: Fase 0 (contratos) + Fase 1 (matcher determinista, ledger en memoria,
abstención estable) + Fase 6 (medición y calibración). De la Fase 4 (ledger
durable) está todo lo que no depende del backend: `snapshot()`/`from_snapshot()`
(estado completo, determinista, fail-closed), `compact()` (política de qué se
tira y qué no) y `durable.py` (el port `AlmacenDurable` + `LedgerDurable`, con el
ámbito explícito que define qué es "el mismo registro"). Elegir el backend (D4) y
el ámbito (D5) sigue siendo decisión abierta — ver
docs/ROADMAP-CAPA-EPISTEMICA.md.

**Imports perezosos (PEP 562), y por qué.** Hallazgo real midiendo el costo de
la fachada Skill (`skills/epistemic_ledger/`) arrancando en un contenedor
efímero: `import kal_epistemics.errors` —un archivo trivial, sin dependencias
propias— tardaba LO MISMO que `import kal_epistemics` completo (~284ms vs
~157ms de un contenedor que no importa nada). La causa: Python inicializa el
paquete padre (`__init__.py`) ANTES de darte cualquier submódulo, y la versión
anterior de este archivo hacía `from kal_epistemics.X import (...)` de los 10
módulos de entrada, sin condición. Pedir una sola pieza pagaba el paquete
entero — y `dataclasses` sola (una de las dependencias que ese arrastre
completo activa) ya cuesta ~69ms de los ~127ms de diferencia medidos.

`__getattr__` a nivel de módulo resuelve el import real la PRIMERA vez que se
pide ese nombre puntual, nunca antes — `from kal_epistemics import Evidence`
sigue funcionando idéntico para cualquier consumidor existente (nada de la API
pública cambia), pero una skill que solo necesita `HypothesisLedger` ya no
paga el costo de `diagnosis.py`/`sqlite_store.py`/etc. Los imports INTERNOS
entre submódulos (ver cualquier archivo del paquete) usan la forma directa
`from kal_epistemics.X import Y`, nunca pasan por este mecanismo — no hay
riesgo de romper el grafo de dependencias interno.
"""
from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0"

# Nombre público -> submódulo que lo define. Un solo lugar: agregar un nombre
# nuevo al paquete es una línea acá, no un import nuevo arriba.
_EXPORTS: dict[str, str] = {
    "ABSTENTION_MARKERS": "abstention",
    "Abstention": "abstention",
    "AbstentionKind": "abstention",
    "is_abstention_text": "abstention",
    "classify_abstention": "abstention",
    "render_abstention": "abstention",
    "ESTADOS_TERMINALES": "compaction",
    "CompactionReport": "compaction",
    "PresupuestoDeCompactacion": "compaction",
    "compactar_hipotesis": "compaction",
    "conserva_lo_irrenunciable": "compaction",
    "planificar": "compaction",
    "ReopenDecision": "conditions",
    "should_reopen": "conditions",
    "INCONSISTENCIA": "diagnosis",
    "NO_VERIFICABLE": "diagnosis",
    "Diagnostico": "diagnosis",
    "Hallazgo": "diagnosis",
    "Transicion": "diagnosis",
    "Trayectoria": "diagnosis",
    "diagnosticar": "diagnosis",
    "trayectoria_de": "diagnosis",
    "AMBITO_POR_DEFECTO": "durable",
    "AlmacenDurable": "durable",
    "AlmacenEnMemoria": "durable",
    "LedgerDurable": "durable",
    "RUTA_POR_DEFECTO": "sqlite_store",
    "AlmacenSQLite": "sqlite_store",
    "DuplicateId": "errors",
    "EpistemicsError": "errors",
    "EstadoIlegible": "errors",
    "InvalidCondition": "errors",
    "InvalidHypothesis": "errors",
    "LedgerLimitExceeded": "errors",
    "SchemaVersionMismatch": "errors",
    "UnknownId": "errors",
    "HypothesisLedger": "ledger",
    "InvalidationReport": "ledger",
    "pattern_is_unsafe": "safety",
    "EXOGENOUS_ORIGINS": "models",
    "SCHEMA_VERSION": "models",
    "STATUSES_REQUIRING_EXOGENOUS_EVIDENCE": "models",
    "Evidence": "models",
    "EvidenceLink": "models",
    "EvidenceOrigin": "models",
    "EvidenceRequest": "models",
    "EvidenceStatus": "models",
    "Flag": "models",
    "HistoryEntry": "models",
    "Hypothesis": "models",
    "HypothesisStatus": "models",
    "LinkPolarity": "models",
    "Question": "models",
    "ReopenAction": "models",
    "ReopenCondition": "models",
    "canonical_key": "models",
    "is_exogenous": "models",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    modulo = _EXPORTS.get(name)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    valor = getattr(importlib.import_module(f"kal_epistemics.{modulo}"), name)
    # Cachea en el namespace del paquete: el PRÓXIMO acceso a este nombre lo
    # resuelve el dict normal de atributos del módulo, nunca vuelve a pasar
    # por __getattr__ (mismo patrón que recomienda PEP 562).
    globals()[name] = valor
    return valor


def __dir__() -> list[str]:
    return __all__

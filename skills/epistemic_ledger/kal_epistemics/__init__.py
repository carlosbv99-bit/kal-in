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
"""
from __future__ import annotations

from kal_epistemics.abstention import (
    ABSTENTION_MARKERS,
    Abstention,
    AbstentionKind,
    is_abstention_text,
    classify_abstention,
    render_abstention,
)
from kal_epistemics.compaction import (
    ESTADOS_TERMINALES,
    CompactionReport,
    PresupuestoDeCompactacion,
    compactar_hipotesis,
    conserva_lo_irrenunciable,
    planificar,
)
from kal_epistemics.conditions import ReopenDecision, should_reopen
from kal_epistemics.durable import (
    AMBITO_POR_DEFECTO,
    AlmacenDurable,
    AlmacenEnMemoria,
    LedgerDurable,
)
from kal_epistemics.sqlite_store import RUTA_POR_DEFECTO, AlmacenSQLite
from kal_epistemics.errors import (
    DuplicateId,
    EpistemicsError,
    EstadoIlegible,
    InvalidCondition,
    InvalidHypothesis,
    LedgerLimitExceeded,
    SchemaVersionMismatch,
    UnknownId,
)
from kal_epistemics.ledger import HypothesisLedger, InvalidationReport
from kal_epistemics.safety import pattern_is_unsafe
from kal_epistemics.models import (
    EXOGENOUS_ORIGINS,
    SCHEMA_VERSION,
    STATUSES_REQUIRING_EXOGENOUS_EVIDENCE,
    Evidence,
    EvidenceLink,
    EvidenceOrigin,
    EvidenceRequest,
    EvidenceStatus,
    Flag,
    HistoryEntry,
    Hypothesis,
    HypothesisStatus,
    LinkPolarity,
    Question,
    ReopenAction,
    ReopenCondition,
    canonical_key,
    is_exogenous,
)

__version__ = "0.1.0"

__all__ = [
    "ABSTENTION_MARKERS",
    "AMBITO_POR_DEFECTO",
    "AlmacenDurable",
    "AlmacenEnMemoria",
    "AlmacenSQLite",
    "RUTA_POR_DEFECTO",
    "ESTADOS_TERMINALES",
    "EXOGENOUS_ORIGINS",
    "SCHEMA_VERSION",
    "STATUSES_REQUIRING_EXOGENOUS_EVIDENCE",
    "Abstention",
    "AbstentionKind",
    "CompactionReport",
    "DuplicateId",
    "EpistemicsError",
    "Evidence",
    "EvidenceLink",
    "EvidenceOrigin",
    "EvidenceRequest",
    "EstadoIlegible",
    "EvidenceStatus",
    "Flag",
    "HistoryEntry",
    "Hypothesis",
    "HypothesisLedger",
    "HypothesisStatus",
    "InvalidCondition",
    "InvalidHypothesis",
    "PresupuestoDeCompactacion",
    "InvalidationReport",
    "LedgerDurable",
    "LedgerLimitExceeded",
    "LinkPolarity",
    "Question",
    "ReopenAction",
    "ReopenCondition",
    "ReopenDecision",
    "SchemaVersionMismatch",
    "UnknownId",
    "canonical_key",
    "classify_abstention",
    "compactar_hipotesis",
    "conserva_lo_irrenunciable",
    "is_abstention_text",
    "is_exogenous",
    "pattern_is_unsafe",
    "planificar",
    "render_abstention",
    "should_reopen",
]

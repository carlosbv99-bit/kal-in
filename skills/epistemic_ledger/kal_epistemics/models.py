"""
Contratos de la capa epistémica: `Evidence`, `Hypothesis`, `Question`,
`EvidenceRequest`, `ReopenCondition`.

Este módulo es SOLO contratos. No hay lógica de decisión acá (eso vive en
`conditions.py`), ni almacenamiento (eso vive en `ledger.py`). El motivo de
separarlo así: son los objetos más caros de cambiar después de que existan
datos reales guardados con su forma, y son justamente los que la doctrina ya
establecida del proyecto (`docs/HISTORY.md`, modelo Visión → Arquitectura →
Infraestructura → Funcionalidad) dice que hay que congelar antes de escribir
la inteligencia.

Cuatro decisiones de diseño que están codificadas acá y conviene entender
antes de tocar nada:

1. **La evidencia es neutra; la polaridad vive en el vínculo.** La misma
   observación puede sostener una hipótesis y contradecir otra. Si la
   polaridad estuviera en `Evidence`, habría que duplicar la evidencia para
   poder usarla en contra de algo.

2. **`origin` es el eje que decide todo.** `MODEL_GENERATED` es evidencia de
   peso cero, siempre, sin excepción y sin importar cuántas veces se repita.
   Lo que sostiene una afirmación es la observación mediada por el kernel o,
   con menos fuerza, el texto externo traído por una herramienta — que es
   exógeno pero NO es verdad: una página web puede mentir.

3. **`evidence.content` es DATO, nunca instrucción.** El contenido de una
   fuente externa se guarda para poder citarlo y para poder matchear
   patrones, pero jamás se interpreta como una orden ni se eleva a
   instrucción. Una memoria de evidencia que reinyecta texto externo al
   contexto como si fuera una instrucción es un mecanismo de entrega de
   inyecciones de prompt, no un mecanismo anti-alucinación.

4. **`level` y `confidence` NO existen como campos.** El nivel epistémico
   (a cuántos saltos especulativos está algo de la evidencia exógena) es una
   propiedad del GRAFO, no del nodo: lo calcula el ledger
   (`HypothesisLedger.level()`), y por eso un modelo no puede autoelevarse
   declarando un campo. `confidence` numérica tampoco existe: quién la
   computaría y con qué datos es exactamente la pregunta sin responder, y un
   número inventado con decimales es lo que produce una alucinación
   convincente. En su lugar hay flags derivados y mecánicos (`Flag`).

Los dataclasses son `frozen` y el historial es una tupla: no existe ninguna
API para mutar un evento pasado. Un cambio de estado devuelve una instancia
nueva, y el ledger es el único que reemplaza entradas.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Collection, Mapping

from kal_epistemics.errors import (
    InvalidCondition,
    InvalidHypothesis,
    SchemaVersionMismatch,
)
from kal_epistemics.safety import validate_keywords, validate_sources

SCHEMA_VERSION = 2
"""Versión del snapshot que se ESCRIBE.

Subió a 2 cuando C (exclusión entre alternativas) y D1 (validez temporal de la
evidencia) agregaron campos. La regla de compatibilidad es de lectura, no de
escritura: se LEEN todas las versiones de `VERSIONES_LEGIBLES` y se escribe
siempre la actual. Fallar cerrado sigue significando lo mismo que siempre —no
reinterpretar un estado de un esquema que NO sabemos leer—, y no "tirar el
estado que sí sabemos leer pero que es de ayer". El bump anterior (1) es
exactamente el estado de F0–F8.
"""

VERSIONES_LEGIBLES: tuple[int, ...] = (1, SCHEMA_VERSION)
"""Versiones que `from_snapshot` acepta. Agregar una acá es declarar que el
código sabe leerla; quitarla es declarar que ya no. Un snapshot de una versión
FUERA de esta tupla se rechaza ruidosamente, que es la promesa que importa."""


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class EvidenceOrigin(str, Enum):
    """
    De dónde salió una evidencia. Es el eje más importante de todo el
    sistema: decide si algo puede sostener una afirmación o no.

    - `KERNEL_OBSERVED`: una ejecución real observada/mediada por el kernel
      (una herramienta corrió, un archivo se leyó, una llamada de red se
      hizo). El modelo NO la puede fabricar, y con kal puede anclarse a un
      `event_id` del audit log encadenado por hash.
    - `TOOL_EXTERNAL`: texto traído de afuera por una herramienta (una web,
      un documento, una API). Es exógeno — no lo generó el modelo — pero NO
      es verdad por ser externo: puede estar equivocado, puede ser
      propaganda, y puede contener una inyección de prompt.
    - `USER_ASSERTED`: lo afirmó un humano en la conversación. Es exógeno,
      pero es un testimonio, no una verificación.
    - `MODEL_GENERATED`: lo escribió el modelo. Peso cero como evidencia.
      Se guarda igual, porque hace falta para poder detectar cadenas
      endógenas y ciclos de auto-confirmación, pero nunca sostiene nada.
    """

    KERNEL_OBSERVED = "kernel_observed"
    TOOL_EXTERNAL = "tool_external"
    USER_ASSERTED = "user_asserted"
    MODEL_GENERATED = "model_generated"


EXOGENOUS_ORIGINS: frozenset[EvidenceOrigin] = frozenset(
    {
        EvidenceOrigin.KERNEL_OBSERVED,
        EvidenceOrigin.TOOL_EXTERNAL,
        EvidenceOrigin.USER_ASSERTED,
    }
)
"""Orígenes que NO son el propio modelo. Son los únicos que pueden sostener
una afirmación — ver `is_exogenous()` y `Hypothesis.validate()`."""


def is_exogenous(origin: EvidenceOrigin) -> bool:
    """True si la evidencia no la generó el propio modelo."""
    return origin in EXOGENOUS_ORIGINS


class LinkPolarity(str, Enum):
    """Si un vínculo de evidencia sostiene o contradice una hipótesis."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class EvidenceStatus(str, Enum):
    """
    Estado de ADMISIÓN de una evidencia — pregunta distinta de cuánto se la
    debe creer.

    - `ACCEPTED`: entró al circuito de razonamiento. Puede sostener, contradecir
      y disparar reaperturas.
    - `QUARANTINED`: se conserva para auditoría, pero NO entra al circuito
      epistémico: no sostiene, no contradice, no reabre nada.

    Las dos preguntas son independientes y la respuesta puede ser "sí puedo
    almacenarlo, no debo creerlo". Confundirlas tiene dos finales malos: un
    rechazo de seguridad que borra trazabilidad, o algo admitido por comodidad
    que se trata como creído.
    """

    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"


class HypothesisStatus(str, Enum):
    """
    Estados de una hipótesis. Deliberadamente NO es `true/false`:

    - `OPEN`: registrada, nadie la evaluó todavía.
    - `SUPPORTED`: tiene evidencia exógena a favor y un falsificador
      declarado (ver `Hypothesis.validate()`).
    - `WEAKENED`: apareció evidencia en contra, o una premisa se debilitó.
    - `SUSPENDED`: en pausa — típicamente porque una dependencia se refutó y
      esta hipótesis no tiene respaldo exógeno propio.
    - `REFUTED`: refutada BAJO CONDICIONES CONCRETAS. No significa
      "imposible": significa "refutada bajo C1–C4, entre T1 y T2" — por eso
      `ReopenCondition` existe y por eso una hipótesis refutada puede volver.
    - `REACTIVATED`: se reabrió, y se reabrió por evidencia exógena nueva.
    - `SUPERSEDED`: la reemplazó otra hipótesis mejor.
    - `UNDETERMINABLE`: la evidencia disponible no permite decidir. Es una
      salida legítima, no un fracaso.
    """

    OPEN = "open"
    SUPPORTED = "supported"
    WEAKENED = "weakened"
    SUSPENDED = "suspended"
    REFUTED = "refuted"
    REACTIVATED = "reactivated"
    SUPERSEDED = "superseded"
    UNDETERMINABLE = "undeterminable"


STATUSES_REQUIRING_EXOGENOUS_EVIDENCE: frozenset[HypothesisStatus] = frozenset(
    {HypothesisStatus.SUPPORTED, HypothesisStatus.REACTIVATED}
)
"""Estados que exigen al menos un vínculo con evidencia exógena. Es la
codificación estructural del principio 1: nada se apoya en texto que el
propio modelo escribió, por más veces que lo repita."""


class Flag(str, Enum):
    """
    Marca derivada y mecánica sobre una hipótesis. Es el reemplazo de
    `confidence: float` — cada una se computa con una regla, no con un
    juicio, y ninguna pretende ser una probabilidad de verdad.

    - `GROUNDED_IN_EXOGENOUS`: hay al menos un camino a evidencia exógena.
    - `ENDOGENOUS_ONLY`: solo se apoya en evidencia `MODEL_GENERATED`.
    - `CONTRADICTED`: hay evidencia exógena en contra.
    - `CIRCULAR`: se sostiene, directa o indirectamente, a sí misma.
    - `UNROOTED`: ni evidencia exógena ni dependencias — una isla.
    - `NO_FALSIFIER`: no declara ninguna condición que la refutaría.
    - `DEPENDENCY_REFUTED`: alguna dependencia (transitiva) fue refutada.
    - `UNVERIFIABLE_PROVENANCE`: hay evidencia sin ancla (`anchor` vacío).
    - `RESTING_ON_QUARANTINED`: se apoya en evidencia puesta en cuarentena.
    - `CONFLICTING_SUPPORT` (C): es una alternativa incompatible con otra que
      también está RESPALDADA. Dos conclusiones que no pueden ser verdad juntas y
      las dos tienen respaldo: el sistema no puede elegir en silencio.
    - `EXPIRED_SUPPORT` (D1): algún vínculo de sostén apunta a una observación
      cuya ventana de validez ya cerró. Marca, no cambia estado.
    """

    GROUNDED_IN_EXOGENOUS = "grounded_in_exogenous"
    ENDOGENOUS_ONLY = "endogenous_only"
    CONTRADICTED = "contradicted"
    CIRCULAR = "circular"
    UNROOTED = "unrooted"
    NO_FALSIFIER = "no_falsifier"
    DEPENDENCY_REFUTED = "dependency_refuted"
    UNVERIFIABLE_PROVENANCE = "unverifiable_provenance"
    RESTING_ON_QUARANTINED = "resting_on_quarantined"
    CONFLICTING_SUPPORT = "conflicting_support"
    EXPIRED_SUPPORT = "expired_support"


class ReopenAction(str, Enum):
    """Qué hacer cuando una `ReopenCondition` matchea una evidencia nueva."""

    REOPEN = "reopen"
    WEAKEN = "weaken"
    REFUTE = "refute"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _freeze(pairs: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """Diccionario → tupla ordenada de pares, para que el dataclass sea
    hashable y el historial no pueda mutarse por referencia."""
    if not pairs:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in pairs.items()))


def _unfreeze(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {k: v for k, v in pairs}


def new_id(prefix: str) -> str:
    """Id con prefijo legible — ayuda a leer un historial o un log."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical_key(text: str) -> str:
    """
    Clave de deduplicación de una pregunta.

    Normaliza lo que NO cambia el significado: mayúsculas, acentos,
    puntuación y espacios. Deliberadamente NO normaliza sinónimos ni orden
    de palabras — es conservadora a propósito: prefiere dos claves distintas
    para la misma pregunta (se ve como duplicado, molesta un poco) antes que
    una sola clave que fusione dos preguntas distintas (se ve como una
    respuesta, y contesta lo que nadie preguntó). La dedup por significado
    con embeddings, si algún día hace falta, va por encima de esto y con
    confirmación, nunca reemplazándolo.
    """
    nfkd = unicodedata.normalize("NFKD", text.casefold())
    sin_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    solo_alnum = re.sub(r"[^\w\s]", " ", sin_acentos)
    return re.sub(r"\s+", " ", solo_alnum).strip()


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """
    Un dato que existe independientemente de lo que el modelo piense.

    `anchor` es lo que permite que la procedencia sea verificable en vez de
    narrada: el id de un evento del audit log de kal, un hash de contenido,
    o una uri. Si está vacío, la evidencia se puede usar pero el ledger
    levanta `Flag.UNVERIFIABLE_PROVENANCE` — es una degradación visible, no
    un error silencioso.
    """

    id: str
    origin: EvidenceOrigin
    anchor: str = ""
    # "audit_event_hash" es el ancla FUERTE: el audit log de kal encadena por
    # HMAC y el evento no tiene id propio — su identidad ES su hash. El modelo
    # no puede escribir ahí. "content_hash" solo prueba que el contenido no
    # cambió desde la ingesta.
    anchor_kind: str = ""  # "audit_event_hash" | "content_hash" | "uri" | "turn_id"
    source: str = ""  # herramienta, url, o quién lo afirmó
    content: str = ""  # DATO — nunca se interpreta como instrucción
    content_hash: str = ""
    method: str = ""  # cómo se obtuvo (nombre de tool + operación de kernel)
    acquired_at: float = 0.0
    # CONTRATO F0 DECLARADO, SIN CONSUMIDOR TODAVÍA (revisión, 2026-09-29): se
    # serializa y los dos caminos reales lo ponen en True, pero nada lo lee. Es el
    # único de los cinco campos que F0 nombra ("id, origin, anchor, method,
    # acquired_at, recheckable") en esta situación; el consumidor natural es la
    # revalidación de evidencia vencida (D1/D2) — ver la fila en el roadmap.
    # Quitarlo NO es gratis: es un campo serializado, o sea otro bump de esquema.
    recheckable: bool = False
    # D1: hasta cuándo vale esta observación. `None` es "sin vencimiento
    # declarado" — que es distinto de "vale para siempre": es "nadie lo declaró",
    # y por eso no se marca nada. El vencimiento MARCA (Flag.EXPIRED_SUPPORT) y NUNCA
    # cambia el estado de una hipótesis: el ledger solo cambia estado por reglas
    # declaradas, y que un dato envejezca no es una observación nueva.
    valid_from: float | None = None
    valid_until: float | None = None
    status: EvidenceStatus = EvidenceStatus.ACCEPTED
    quarantine_reason: str = ""

    def is_expired_at(self, at: float) -> bool:
        """True si su ventana de validez ya cerró. Sin `valid_until` no vence."""
        return self.valid_until is not None and at > self.valid_until

    @property
    def is_exogenous(self) -> bool:
        return is_exogenous(self.origin)

    @property
    def is_usable(self) -> bool:
        """
        True si la evidencia entró al circuito epistémico. Lo puesto en
        cuarentena se conserva y se puede vincular (para no perder la
        trazabilidad de que una hipótesis se apoyó en material hoy
        cuestionado), pero no sostiene, no contradice y no reabre.
        """
        return self.status is EvidenceStatus.ACCEPTED

    @property
    def dedup_key(self) -> str:
        """
        Clave para decidir si esto es evidencia NUEVA.

        El hash manda: la misma fuente re-consultada con el mismo contenido
        no es evidencia nueva, es la misma evidencia otra vez. Sin hash, cae
        al id.
        """
        return self.content_hash or self.id

    @classmethod
    def from_content(
        cls,
        content: str,
        *,
        origin: EvidenceOrigin,
        **kwargs: Any,
    ) -> "Evidence":
        """Constructor que calcula el hash del contenido — usar este siempre
        que haya contenido real, para que la dedup funcione."""
        return cls(
            id=kwargs.pop("id", new_id("ev")),
            origin=origin,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "origin": self.origin.value,
            "anchor": self.anchor,
            "anchor_kind": self.anchor_kind,
            "source": self.source,
            "content": self.content,
            "content_hash": self.content_hash,
            "method": self.method,
            "acquired_at": self.acquired_at,
            "recheckable": self.recheckable,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "status": self.status.value,
            "quarantine_reason": self.quarantine_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        _check_schema(data)
        return cls(
            id=data["id"],
            origin=EvidenceOrigin(data["origin"]),
            anchor=data.get("anchor", ""),
            anchor_kind=data.get("anchor_kind", ""),
            source=data.get("source", ""),
            content=data.get("content", ""),
            content_hash=data.get("content_hash", ""),
            method=data.get("method", ""),
            acquired_at=data.get("acquired_at", 0.0),
            recheckable=data.get("recheckable", False),
            # v1 no traía ventana de validez: default None (sin vencimiento
            # declarado), que es exactamente lo que significaba en v1.
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
            status=EvidenceStatus(data.get("status", EvidenceStatus.ACCEPTED.value)),
            quarantine_reason=data.get("quarantine_reason", ""),
        )


# --------------------------------------------------------------------------
# ReopenCondition
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReopenCondition:
    """
    Condición MECÁNICAMENTE evaluable sobre evidencia nueva. Es el objeto que
    hace que una hipótesis guardada sirva en el futuro: sin esto, decidir si
    reabrir una hipótesis requiere llamar al modelo, y volvemos exactamente
    al problema que esto intenta resolver.

    Por eso es un predicado declarativo y serializable (orígenes + patrones),
    no un callable ni una frase en prosa.

    Un falsificador ("¿qué me refutaría?") es la MISMA clase con
    `action=REFUTE` — es la misma pregunta con distinta consecuencia, y
    duplicar el tipo solo agregaría dos formas de decir lo mismo.

    Fail-closed en todo: `origins` vacío NO matchea nada (no "cualquiera"),
    un origen no declarado no matchea, y `MODEL_GENERATED` no matchea nunca
    — esa regla es estructural y vive en `should_reopen()`, no acá, para que
    no se pueda desactivar construyendo una condición permisiva.

    DOS CAMINOS, y la separación es de seguridad, no de estilo (ver
    `safety.py`):

    - **Camino no confiable** (el modelo, vía la capa de tools): usa
      `from_untrusted()`, que solo acepta selectores enumerados
      (`source_in`, `content_keywords`). NO tiene parámetros de patrón: no es
      que los filtre, es que no puede recibirlos.
    - **Camino confiable** (código o un humano): puede usar `source_pattern` y
      `content_pattern`, con tope de longitud y rechazo de cuantificadores
      anidados. Si un patrón no se puede analizar, se rechaza y la condición
      no matchea.

    Un `content_keywords` es una búsqueda de SUBSTRING, no un regex: no hay
    motor de expresiones regulares, así que no hay ReDoS posible.
    """

    id: str
    description: str = ""  # para humanos; NO se evalúa
    origins: tuple[EvidenceOrigin, ...] = ()
    source_pattern: str = ""  # regex sobre Evidence.source
    content_pattern: str = ""  # regex sobre Evidence.content (texto externo)
    anchor_kinds: tuple[str, ...] = ()  # si no está vacío, exige uno de estos
    content_keywords: tuple[str, ...] = ()  # substrings, SIN motor de regex
    source_in: tuple[str, ...] = ()  # valores EXACTOS de Evidence.source
    action: ReopenAction = ReopenAction.REOPEN

    def __post_init__(self) -> None:
        """Valida los selectores seguros al construir. Fail-closed: no recorta
        en silencio, levanta."""
        try:
            validate_keywords(self.content_keywords)
            validate_sources(self.source_in)
        except ValueError as e:
            raise InvalidCondition(f"ReopenCondition '{self.id}' inválida: {e}") from None

    @classmethod
    def for_new_evidence(
        cls,
        *,
        description: str = "",
        source_pattern: str = "",
        content_pattern: str = "",
        action: ReopenAction = ReopenAction.REOPEN,
        id: str | None = None,
    ) -> "ReopenCondition":
        """
        Condición típica: "si aparece evidencia exógena nueva que matchee
        esto". Es el default sensato y sigue excluyendo `MODEL_GENERATED`,
        porque la exclusión es estructural.
        """
        return cls(
            id=id or new_id("rc"),
            description=description,
            origins=(
                EvidenceOrigin.KERNEL_OBSERVED,
                EvidenceOrigin.TOOL_EXTERNAL,
                EvidenceOrigin.USER_ASSERTED,
            ),
            source_pattern=source_pattern,
            content_pattern=content_pattern,
            action=action,
        )

    @classmethod
    def from_untrusted(
        cls,
        *,
        description: str = "",
        source_in: tuple[str, ...] = (),
        content_keywords: tuple[str, ...] = (),
        action: ReopenAction = ReopenAction.REOPEN,
        id: str | None = None,
    ) -> "ReopenCondition":
        """
        Constructor para condiciones que vienen del MODELO (capa de tools).

        No tiene parámetros de patrón: no es que los filtre, es que no puede
        recibirlos. Esa es la diferencia entre un heurístico y una garantía, y
        es la razón por la que existe separado de `for_new_evidence()`.
        """
        return cls(
            id=id or new_id("rc"),
            description=description,
            origins=(
                EvidenceOrigin.KERNEL_OBSERVED,
                EvidenceOrigin.TOOL_EXTERNAL,
                EvidenceOrigin.USER_ASSERTED,
            ),
            content_keywords=tuple(content_keywords),
            source_in=tuple(source_in),
            action=action,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "description": self.description,
            "origins": [o.value for o in self.origins],
            "content_keywords": list(self.content_keywords),
            "source_in": list(self.source_in),
            "source_pattern": self.source_pattern,
            "content_pattern": self.content_pattern,
            "anchor_kinds": list(self.anchor_kinds),
            "action": self.action.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReopenCondition":
        _check_schema(data)
        return cls(
            id=data["id"],
            description=data.get("description", ""),
            origins=tuple(EvidenceOrigin(o) for o in data.get("origins", ())),
            content_keywords=tuple(data.get("content_keywords", ())),
            source_in=tuple(data.get("source_in", ())),
            source_pattern=data.get("source_pattern", ""),
            content_pattern=data.get("content_pattern", ""),
            anchor_kinds=tuple(data.get("anchor_kinds", ())),
            action=ReopenAction(data.get("action", ReopenAction.REOPEN.value)),
        )


# --------------------------------------------------------------------------
# Hypothesis
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceLink:
    """
    Vínculo hipótesis ↔ evidencia, con la polaridad (ver módulo).

    `note` es una anotación OPCIONAL de quien vincula (`link_evidence(note=...)`):
    nada del camino automático la escribe. La aplicación automática de una
    condición deja su motivo en la entrada de historial, que es la única fuente de
    verdad de "por qué pasó esto" — antes se copiaba acá también, y la segunda
    copia no la leía nadie (revisión, 2026-09-29).
    """

    evidence_id: str
    polarity: LinkPolarity = LinkPolarity.SUPPORTS
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "polarity": self.polarity.value,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceLink":
        return cls(
            evidence_id=data["evidence_id"],
            polarity=LinkPolarity(data.get("polarity", LinkPolarity.SUPPORTS.value)),
            note=data.get("note", ""),
        )


@dataclass(frozen=True)
class HistoryEntry:
    """Un evento del historial. Inmutable: la biografía epistémica de una
    hipótesis es un registro, no un campo que se sobrescribe."""

    at: float
    kind: str  # "generated" | "status" | "evidence_linked" | "reopened" | ...
    reason: str = ""
    detail: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "reason": self.reason,
            "detail": _unfreeze(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HistoryEntry":
        return cls(
            at=data["at"],
            kind=data["kind"],
            reason=data.get("reason", ""),
            detail=_freeze(data.get("detail")),
        )


@dataclass(frozen=True)
class Hypothesis:
    """
    Una proposición registrada, con su procedencia, sus condiciones y su
    historial.

    Notar lo que NO está acá: `level` y `confidence`. Ver el docstring del
    módulo — son propiedades del grafo, no del nodo, y el ledger las deriva.
    """

    id: str
    claim: str
    question_id: str = ""
    status: HypothesisStatus = HypothesisStatus.OPEN
    depends_on: tuple[str, ...] = ()
    # C: alternativas INCOMPATIBLES con esta (no "distintas": excluyentes). La
    # relación es SIMÉTRICA y la mantiene el ledger en las dos direcciones, porque
    # una exclusión de un solo lado es una trampa: el conflicto se vería desde una
    # hipótesis y no desde la otra.
    excludes: tuple[str, ...] = ()
    evidence_links: tuple[EvidenceLink, ...] = ()
    reopen_conditions: tuple[ReopenCondition, ...] = ()
    validity_conditions: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0
    history: tuple[HistoryEntry, ...] = ()

    # -- validación estructural ------------------------------------------

    def validate(self, *, exogenous_evidence_ids: Collection[str] = ()) -> None:
        """
        Reglas chequeables sin recorrer el grafo de dependencias.

        `exogenous_evidence_ids` es el conjunto de evidencias de origen
        exógeno que el ledger conoce: hay que pasarlo porque la hipótesis
        sola no puede saber si la evidencia a la que apunta la escribió el
        modelo o la observó el kernel. Ese chequeo es el importante: es la
        diferencia entre un sistema que se apoya en observaciones y uno que
        se apoya en su propio texto.
        """
        if not self.claim.strip():
            raise InvalidHypothesis("Una hipótesis necesita una proposición con contenido.")

        if self.id in self.depends_on:
            raise InvalidHypothesis(
                f"'{self.id}' no puede depender de sí misma (ciclo de longitud 1)."
            )

        if self.id in self.excludes:
            raise InvalidHypothesis(
                f"'{self.id}' no puede excluirse a sí misma: una alternativa "
                "incompatible consigo misma no es una alternativa."
            )

        vistos: set[tuple[str, LinkPolarity]] = set()
        for link in self.evidence_links:
            clave = (link.evidence_id, link.polarity)
            if clave in vistos:
                raise InvalidHypothesis(
                    f"Evidencia duplicada en '{self.id}': {link.evidence_id} "
                    f"({link.polarity.value}) ya estaba vinculada con la misma polaridad."
                )
            vistos.add(clave)

        if self.status in STATUSES_REQUIRING_EXOGENOUS_EVIDENCE:
            exogenas = set(exogenous_evidence_ids)
            sostiene = any(
                link.polarity is LinkPolarity.SUPPORTS and link.evidence_id in exogenas
                for link in self.evidence_links
            )
            if not sostiene:
                raise InvalidHypothesis(
                    f"'{self.id}' no puede estar en estado {self.status.value} sin al "
                    "menos un vínculo con evidencia exógena (observación del kernel, "
                    "texto externo, o afirmación de un humano). La evidencia generada "
                    "por el propio modelo no sostiene nada, por más veces que se repita."
                )

        if self.status is HypothesisStatus.SUPPORTED and not self.has_falsifier:
            raise InvalidHypothesis(
                f"'{self.id}' no puede estar SUPPORTED sin un falsificador: una "
                "hipótesis que no dice qué la refutaría no es contrastable."
            )

    @property
    def has_falsifier(self) -> bool:
        """True si declara al menos una condición con acción REFUTE."""
        return any(rc.action is ReopenAction.REFUTE for rc in self.reopen_conditions)

    def with_history(self, entry: HistoryEntry) -> "Hypothesis":
        """Instancia nueva con un evento más. No hay forma de editar uno
        viejo: el historial es una tupla y el dataclass es frozen."""
        return replace(self, history=self.history + (entry,), updated_at=entry.at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "claim": self.claim,
            "question_id": self.question_id,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "excludes": list(self.excludes),
            "evidence_links": [l.to_dict() for l in self.evidence_links],
            "reopen_conditions": [rc.to_dict() for rc in self.reopen_conditions],
            "validity_conditions": list(self.validity_conditions),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Hypothesis":
        _check_schema(data)
        return cls(
            id=data["id"],
            claim=data["claim"],
            question_id=data.get("question_id", ""),
            status=HypothesisStatus(data.get("status", HypothesisStatus.OPEN.value)),
            depends_on=tuple(data.get("depends_on", ())),
            # v1 no tenía exclusiones: default vacío.
            excludes=tuple(data.get("excludes", ())),
            evidence_links=tuple(
                EvidenceLink.from_dict(l) for l in data.get("evidence_links", ())
            ),
            reopen_conditions=tuple(
                ReopenCondition.from_dict(rc) for rc in data.get("reopen_conditions", ())
            ),
            validity_conditions=tuple(data.get("validity_conditions", ())),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            history=tuple(HistoryEntry.from_dict(h) for h in data.get("history", ())),
        )


# --------------------------------------------------------------------------
# Question / EvidenceRequest
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRequest:
    """
    El déficit: qué información haría falta para poder responder. Es lo
    único que hay que conservar sí o sí cuando se compacta — las hipótesis
    largas caducan, el déficit no, porque es lo que evita reinventar la
    misma investigación dentro de seis meses.

    `satisfied_by` se llena cuando llega la evidencia que lo cubre: ahí es
    cuando una pregunta sin resolver deja de serlo.
    """

    id: str
    description: str
    satisfied_by: str = ""
    reopen_conditions: tuple[ReopenCondition, ...] = ()

    @property
    def is_satisfied(self) -> bool:
        return bool(self.satisfied_by)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "description": self.description,
            "satisfied_by": self.satisfied_by,
            "reopen_conditions": [rc.to_dict() for rc in self.reopen_conditions],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRequest":
        _check_schema(data)
        return cls(
            id=data["id"],
            description=data["description"],
            satisfied_by=data.get("satisfied_by", ""),
            reopen_conditions=tuple(
                ReopenCondition.from_dict(rc) for rc in data.get("reopen_conditions", ())
            ),
        )


@dataclass(frozen=True)
class Question:
    """
    Una pregunta conservada aunque no tenga respuesta.

    `canonical_key` es la clave de dedup: sin ella, la misma pregunta crea
    un registro nuevo cada vez que se reformula, y el ledger se llena de
    duplicados que se contradicen entre sí. `times_asked` importa para la
    métrica de abstención repetible: la segunda vez que se pregunta algo
    irresoluble, la respuesta tiene que ser la MISMA, no una nueva
    improvisación.
    """

    id: str
    text: str
    canonical_key: str
    asked_at: float = 0.0
    times_asked: int = 1
    hypothesis_ids: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()  # ids de EvidenceRequest
    # Sin consumidor todavía (revisión, 2026-09-29): se actualiza al re-preguntar
    # y al saldar un déficit, y nada lo lee. Consumidor natural: una política de
    # poda de preguntas dormidas en la compactación (que hoy ordena por
    # `created_at`). Misma nota que `Evidence.recheckable`: quitarlo cuesta un
    # bump de esquema.
    last_reviewed_at: float = 0.0
    history: tuple[HistoryEntry, ...] = ()

    def with_history(self, entry: HistoryEntry) -> "Question":
        return replace(self, history=self.history + (entry,))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "text": self.text,
            "canonical_key": self.canonical_key,
            "asked_at": self.asked_at,
            "times_asked": self.times_asked,
            "hypothesis_ids": list(self.hypothesis_ids),
            "missing": list(self.missing),
            "last_reviewed_at": self.last_reviewed_at,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Question":
        _check_schema(data)
        return cls(
            id=data["id"],
            text=data["text"],
            canonical_key=data["canonical_key"],
            asked_at=data.get("asked_at", 0.0),
            times_asked=data.get("times_asked", 1),
            hypothesis_ids=tuple(data.get("hypothesis_ids", ())),
            missing=tuple(data.get("missing", ())),
            last_reviewed_at=data.get("last_reviewed_at", 0.0),
            history=tuple(HistoryEntry.from_dict(h) for h in data.get("history", ())),
        )


# --------------------------------------------------------------------------
# Internos
# --------------------------------------------------------------------------


def _check_schema(data: Mapping[str, Any]) -> None:
    """
    Rechaza datos de una versión de esquema distinta. Falla ruidosamente: un
    ledger leído con el esquema equivocado y "interpretado igual" es una
    forma silenciosa de corromper procedencia.
    """
    version = data.get("schema_version")
    if version not in VERSIONES_LEGIBLES:
        raise SchemaVersionMismatch(
            f"Se esperaba schema_version en {VERSIONES_LEGIBLES} y llegó {version!r}. "
            "Un esquema que no sabemos leer no se reinterpreta: migrar explícitamente."
        )


def now() -> float:
    """Reloj por defecto. El ledger lo inyecta para poder testear sin
    depender del tiempo real."""
    return time.time()

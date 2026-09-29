"""
`HypothesisLedger`: el registro en memoria de preguntas, hipótesis y
evidencia.

Fase 1 (Infraestructura) del roadmap: **en memoria, sin persistencia**. Eso
es deliberado y no una simplificación provisoria. La reapertura ocurre de
verdad DENTRO de una tarea — la herramienta número tres devuelve el dato que
faltaba cinco pasos después de haber registrado la hipótesis — y ahí no hace
falta ni base de datos, ni dedup entre sesiones, ni compactación. Persistir
primero es cómo se construye un cementerio que nadie consulta: el ledger
durable (F4) viene recién cuando esto demuestre que se usa.

Tres invariantes que el ledger hace cumplir y que valen más que cualquier
campo adicional del esquema:

1. **La procedencia no se puede inventar.** No se puede vincular una
   hipótesis a evidencia que no esté registrada (`UnknownId`), y el ledger es
   el único que conoce el origen de cada evidencia — por eso la validación de
   "esto está sostenido" se hace acá y no en la hipótesis.
2. **Nada se apoya en texto del propio modelo.** Un estado que exige
   respaldo (`SUPPORTED`, `REACTIVATED`) sin evidencia exógena levanta
   `InvalidHypothesis`. La repetición no cuenta: registrar la misma hipótesis
   cien veces no la refuerza, y no hay ninguna API acá que lo permita.
3. **El nivel epistémico es del grafo, no del nodo.** `level()` lo calcula el
   ledger recorriendo dependencias. No existe forma de declararlo, y por lo
   tanto no existe forma de que un modelo se autoeleve.
4. **Dos presupuestos de reapertura, no uno.** Por hipótesis Y por pregunta:
   el tope por hipótesis no acota el total de una pregunta, porque la misma
   observación se evalúa contra todas las hipótesis del ledger (medido en F6:
   5 hipótesis mirando el mismo marcador → 5 reaperturas con UNA observación).
   El presupuesto se **deriva** de los contadores por hipótesis, así que viaja en
   el snapshot sin agregar estado.
5. **El resultado no depende del orden de las condiciones.** Una misma evidencia
   que dispara varias acciones deja los vínculos de TODAS y el estado más cauto
   (`REFUTE > WEAKEN > REOPEN`). El dedup de "esta evidencia ya se aplicó" es por
   (condición, evidencia), no por evidencia: si fuera por evidencia, la segunda
   condición se descartaría pensando que es una repetición, y cuál se descarta
   dependería del orden de declaración. Lo encontró la medición de F6 (una
   observación con marcador de reapertura y de refutación quedaba `REACTIVATED`
   sin `Flag.CONTRADICTED`); hay un test que declara las condiciones en los dos
   órdenes y exige el mismo resultado.

La propagación de invalidación (`invalidate()`) distingue dos casos que
suelen confundirse: un descendiente que tiene respaldo exógeno PROPIO no se
suspende (solo queda marcado con `Flag.DEPENDENCY_REFUTED`), porque su
sostén no dependía de la premisa caída; uno que solo colgaba de la premisa
refutada se suspende. Sin esa distinción, invalidar una premisa arrasa con
conclusiones que seguían siendo válidas por sí mismas.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from kal_epistemics.compaction import (
    CompactionReport,
    PresupuestoDeCompactacion,
    aplicar,
    conserva_lo_irrenunciable,
)
from kal_epistemics.conditions import ReopenDecision, should_reopen
from kal_epistemics.errors import (
    DuplicateId,
    InvalidHypothesis,
    LedgerLimitExceeded,
    SchemaVersionMismatch,
    UnknownId,
)
from kal_epistemics.safety import (
    MAX_CLAIM_LENGTH,
    MAX_CONDITIONS_PER_HYPOTHESIS,
    MAX_DEPENDENCIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_EVIDENCE_CONTENT_LENGTH,
    MAX_EVIDENCE_ENTRIES,
    MAX_EXCLUDES,
    MAX_HYPOTHESES_PER_QUESTION,
    MAX_LINKS_PER_HYPOTHESIS,
    MAX_MISSING_PER_QUESTION,
    MAX_QUESTION_LENGTH,
    MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS,
    MAX_REOPEN_APPLICATIONS_PER_QUESTION,
)
from kal_epistemics.models import (
    SCHEMA_VERSION,
    Evidence,
    EvidenceLink,
    EvidenceRequest,
    Flag,
    HistoryEntry,
    Hypothesis,
    HypothesisStatus,
    LinkPolarity,
    Question,
    ReopenAction,
    ReopenCondition,
    _check_schema,
    canonical_key,
    new_id,
)

PRECEDENCIA_DE_ACCIONES: tuple[ReopenAction, ...] = (
    ReopenAction.REFUTE,
    ReopenAction.WEAKEN,
    ReopenAction.REOPEN,
)
"""
Qué estado queda cuando la MISMA evidencia dispara varias acciones en el mismo
`consider()`: gana la más cauta, de izquierda a derecha.

Por qué existe esta regla, y no "la última condición que matcheó": sin ella, el
estado final depende del ORDEN en que se declararon las condiciones, y el orden
no es información — es un detalle de construcción. Peor: el resultado sería el
más optimista de los dos (`REACTIVATED`, es decir una hipótesis disponible para
citarla como recién respaldada) justo cuando la misma evidencia también la
refuta. El resto del diseño es fail-closed (§0.4 del roadmap: "el gate falla
cerrado, no abierto"), y esto es lo mismo un nivel más abajo: ante una
observación que dice dos cosas contradictorias, se queda con la lectura cauta.

Los VÍNCULOS de todas las acciones se registran igual (nunca se pierde la mitad
de la señal): lo que la precedencia decide es el ESTADO, no qué se guarda.
"""


def _detalle(**kwargs: object) -> tuple[tuple[str, str], ...]:
    """Detalle de historial: tupla ordenada de pares, para que el evento sea
    inmutable y comparable."""
    return tuple(sorted((k, str(v)) for k, v in kwargs.items()))


if TYPE_CHECKING:  # pragma: no cover - solo para anotaciones
    from .diagnosis import Trayectoria

@dataclass(frozen=True)
class InvalidationReport:
    """
    Qué pasó al invalidar una premisa. Se devuelve explícitamente en vez de
    dejarlo como efecto lateral: "invalidé H2 y esto arrastró a estas tres"
    es información que el host necesita para decidir qué revisar.
    """

    refuted: str
    suspended: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()


class HypothesisLedger:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        # El reloj se inyecta para que los tests no dependan del tiempo real
        # (mismo criterio que kernel/broker/resource_broker.py).
        self._clock = clock
        self._evidence: dict[str, Evidence] = {}
        self._hypotheses: dict[str, Hypothesis] = {}
        self._questions: dict[str, Question] = {}
        self._questions_by_key: dict[str, str] = {}
        self._requests: dict[str, EvidenceRequest] = {}
        # (condición, clave de dedup) ya aplicadas, por hipótesis. La clave
        # incluye la CONDICIÓN a propósito: sin eso, la segunda condición que
        # matchea la misma evidencia se descarta como si fuera una repetición
        # (ver `consider()` y `PRECEDENCIA_DE_ACCIONES`).
        self._applied: dict[str, set[tuple[str, str]]] = {}
        self._applied_requests: dict[str, set[str]] = {}
        self._reopen_count: dict[str, int] = {}

    def _now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------
    # Evidencia
    # ------------------------------------------------------------------

    def add_evidence(self, evidence: Evidence) -> Evidence:
        """
        Registra evidencia. Los topes son de seguridad (ver safety.py): un
        ledger sin límites, alimentado por el modelo, es un agotamiento de
        recursos trivial de provocar.
        """
        if len(evidence.content) > MAX_EVIDENCE_CONTENT_LENGTH:
            raise LedgerLimitExceeded(
                f"Evidencia '{evidence.id}': {len(evidence.content)} caracteres de "
                f"contenido > tope de {MAX_EVIDENCE_CONTENT_LENGTH}."
            )
        existente = self._evidence.get(evidence.id)
        if existente is None and len(self._evidence) >= MAX_EVIDENCE_ENTRIES:
            raise LedgerLimitExceeded(
                f"El ledger alcanzó el tope de {MAX_EVIDENCE_ENTRIES} evidencias."
            )
        if existente is not None and existente != evidence:
            raise DuplicateId(
                f"Ya hay una evidencia con id '{evidence.id}' y contenido distinto. "
                "La procedencia no se pisa."
            )
        self._evidence[evidence.id] = evidence
        return evidence

    def evidence(self, evidence_id: str) -> Evidence:
        try:
            return self._evidence[evidence_id]
        except KeyError:
            raise UnknownId(f"No hay evidencia registrada con id '{evidence_id}'.") from None

    def evidence_by_dedup_key(self, dedup_key: str) -> Evidence | None:
        for ev in self._evidence.values():
            if ev.dedup_key == dedup_key:
                return ev
        return None

    def exogenous_evidence_ids(self) -> frozenset[str]:
        """Ids de evidencia que NO generó el modelo. Es el conjunto contra el
        que se valida que algo esté realmente sostenido."""
        return frozenset(
            ev.id for ev in self._evidence.values() if ev.is_exogenous and ev.is_usable
        )

    # ------------------------------------------------------------------
    # Preguntas y déficits
    # ------------------------------------------------------------------

    def open_question(
        self,
        text: str,
        *,
        missing: Iterable[str] = (),
        reopen_conditions: Iterable[ReopenCondition] = (),
    ) -> tuple[Question, bool]:
        """
        Abre una pregunta, o devuelve la que ya existe con la misma clave
        canónica. Devuelve `(question, creada)`.

        La dedup no es cosmética: sin ella, la misma pregunta reformulada
        crea un registro nuevo cada vez y el ledger termina con duplicados
        que se contradicen entre sí.

        `reopen_conditions` es la CONDICIÓN DE PAGO de la deuda que se declara:
        sin ella, un déficit no se salda nunca (el ledger no tiene forma de saber
        qué evidencia lo cubre, y adivinarlo sería semántica, no mecánica), así
        que queda enclavado y el gate bloquea para siempre respuestas que ya
        tienen la evidencia delante. Lo encontró la medición de F6 (D9): es la
        diferencia entre "no lo sé" y "no lo sé y nunca lo voy a saber".
        """
        if len(text) > MAX_QUESTION_LENGTH:
            raise LedgerLimitExceeded(
                f"Pregunta de {len(text)} caracteres > tope de {MAX_QUESTION_LENGTH}."
            )
        faltantes_pedidos = tuple(missing)
        if len(faltantes_pedidos) > MAX_MISSING_PER_QUESTION:
            raise LedgerLimitExceeded(
                f"{len(faltantes_pedidos)} déficits declarados > tope de "
                f"{MAX_MISSING_PER_QUESTION}."
            )
        clave = canonical_key(text)
        existente = self._questions_by_key.get(clave)
        at = self._now()

        if existente is not None:
            q = self._questions[existente]
            q = replace(q, times_asked=q.times_asked + 1, last_reviewed_at=at)
            q = q.with_history(HistoryEntry(at=at, kind="re_asked"))
            self._questions[q.id] = q
            return q, False

        qid = new_id("q")
        condiciones = tuple(reopen_conditions)
        faltantes = tuple(
            self._nuevo_deficit(d, reopen_conditions=condiciones).id
            for d in faltantes_pedidos
        )
        q = Question(
            id=qid,
            text=text,
            canonical_key=clave,
            asked_at=at,
            missing=faltantes,
            history=(HistoryEntry(at=at, kind="opened"),),
        )
        self._questions[qid] = q
        self._questions_by_key[clave] = qid
        return q, True

    def all_questions(self) -> tuple[Question, ...]:
        """Todas las preguntas registradas, en orden estable por id."""
        return tuple(self._questions[qid] for qid in sorted(self._questions))

    def question(self, question_id: str) -> Question:
        try:
            return self._questions[question_id]
        except KeyError:
            raise UnknownId(f"No hay pregunta registrada con id '{question_id}'.") from None

    def question_by_key(self, key: str) -> Question | None:
        qid = self._questions_by_key.get(canonical_key(key))
        return self._questions[qid] if qid is not None else None

    def add_evidence_request(
        self,
        question_id: str,
        description: str,
        *,
        reopen_conditions: Iterable[ReopenCondition] = (),
    ) -> EvidenceRequest:
        """Agrega un déficit a una pregunta existente: qué haría falta para
        poder responderla."""
        q = self.question(question_id)
        req = self._nuevo_deficit(description, reopen_conditions=reopen_conditions)
        self._questions[q.id] = replace(q, missing=q.missing + (req.id,))
        return req

    def request(self, request_id: str) -> EvidenceRequest:
        try:
            return self._requests[request_id]
        except KeyError:
            raise UnknownId(f"No hay déficit registrado con id '{request_id}'.") from None

    def missing_descriptions(self, question_id: str) -> tuple[str, ...]:
        """Descripciones de lo que falta, en orden estable. Es lo que se
        muestra en una abstención por ignorancia."""
        q = self.question(question_id)
        return tuple(
            sorted(
                self._requests[rid].description
                for rid in q.missing
                if rid in self._requests and not self._requests[rid].is_satisfied
            )
        )

    def _nuevo_deficit(
        self,
        description: str,
        *,
        reopen_conditions: Iterable[ReopenCondition] = (),
    ) -> EvidenceRequest:
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise LedgerLimitExceeded(
                f"Descripción de {len(description)} caracteres > tope de "
                f"{MAX_DESCRIPTION_LENGTH}."
            )
        req = EvidenceRequest(
            id=new_id("need"),
            description=description,
            reopen_conditions=tuple(reopen_conditions),
        )
        self._requests[req.id] = req
        return req

    # ------------------------------------------------------------------
    # Hipótesis
    # ------------------------------------------------------------------

    def record_hypothesis(
        self,
        claim: str,
        *,
        question_id: str = "",
        depends_on: Iterable[str] = (),
        evidence_links: Iterable[EvidenceLink | tuple[str, LinkPolarity] | str] = (),
        reopen_conditions: Iterable[ReopenCondition] = (),
        validity_conditions: Iterable[str] = (),
        excludes: Iterable[str] = (),
    ) -> Hypothesis:
        """
        Registra una hipótesis, SIEMPRE en estado `OPEN`.

        No hay parámetro de estado a propósito: el que registra no cierra.
        El estado lo cambian la evidencia (vía `consider()`) o un humano —
        nunca el mismo actor que propuso la idea, porque un sistema donde el
        modelo puede autodeclararse `SUPPORTED` tiene todo este andamiaje
        como decoración.

        `excludes` declara alternativas INCOMPATIBLES. Se mantiene SIMÉTRICA: la
        otra hipótesis queda excluyendo a esta en la misma operación, con una
        entrada de historial que lo dice. Una exclusión de un solo lado haría que
        el conflicto se vea desde una y no desde la otra — que es la peor forma
        de tener un flag: la que depende de por dónde se mire.
        """
        if len(claim) > MAX_CLAIM_LENGTH:
            raise LedgerLimitExceeded(
                f"Proposición de {len(claim)} caracteres > tope de {MAX_CLAIM_LENGTH}."
            )
        deps = tuple(depends_on)
        excluidas = tuple(dict.fromkeys(excludes))
        if len(excluidas) > MAX_EXCLUDES:
            raise LedgerLimitExceeded(
                f"{len(excluidas)} exclusiones > tope de {MAX_EXCLUDES}."
            )
        for otra in excluidas:
            self.hypothesis(otra)  # UnknownId si no existe: no se excluye un fantasma
        enlaces = self._normalizar_enlaces(evidence_links)
        condiciones = tuple(reopen_conditions)
        if len(deps) > MAX_DEPENDENCIES:
            raise LedgerLimitExceeded(
                f"{len(deps)} dependencias > tope de {MAX_DEPENDENCIES}."
            )
        if len(enlaces) > MAX_LINKS_PER_HYPOTHESIS:
            raise LedgerLimitExceeded(
                f"{len(enlaces)} vínculos de evidencia > tope de "
                f"{MAX_LINKS_PER_HYPOTHESIS}."
            )
        if len(condiciones) > MAX_CONDITIONS_PER_HYPOTHESIS:
            raise LedgerLimitExceeded(
                f"{len(condiciones)} condiciones de reapertura > tope de "
                f"{MAX_CONDITIONS_PER_HYPOTHESIS}."
            )
        if question_id:
            # self.question() levanta UnknownId si no existe: la pregunta tiene
            # que estar registrada ANTES de colgarle una hipótesis.
            if len(self.question(question_id).hypothesis_ids) >= MAX_HYPOTHESES_PER_QUESTION:
                raise LedgerLimitExceeded(
                    f"La pregunta '{question_id}' alcanzó el tope de "
                    f"{MAX_HYPOTHESES_PER_QUESTION} hipótesis."
                )
        hid = new_id("h")
        at = self._now()
        h = Hypothesis(
            id=hid,
            claim=claim,
            question_id=question_id,
            depends_on=deps,
            excludes=excluidas,
            evidence_links=enlaces,
            reopen_conditions=condiciones,
            validity_conditions=tuple(validity_conditions),
            created_at=at,
            updated_at=at,
            history=(HistoryEntry(at=at, kind="generated"),),
        )
        self._validate(h)
        self._hypotheses[hid] = h
        if question_id:
            q = self.question(question_id)  # UnknownId si la pregunta no existe
            self._questions[q.id] = replace(q, hypothesis_ids=q.hypothesis_ids + (hid,))
        for otra in excluidas:
            self._anotar_exclusion(otra, hid)
        return h

    def _anotar_exclusion(self, hypothesis_id: str, otra: str) -> None:
        """
        Cierra la simetría de una exclusión: la otra hipótesis queda excluyendo a
        esta, con una entrada de historial que lo atribuye a la declaración (no a
        una decisión propia). Sin esto, `excludes` sería direccional y el flag
        dependería de por dónde se mire.
        """
        h = self.hypothesis(hypothesis_id)
        if otra in h.excludes:
            return
        at = self._now()
        h2 = replace(h, excludes=h.excludes + (otra,))
        h2 = h2.with_history(
            HistoryEntry(
                at=at,
                kind="excluded",
                reason=f"alternativa declarada incompatible desde '{otra}'",
                detail=_detalle(otra=otra),
            )
        )
        self._validate(h2)
        self._hypotheses[hypothesis_id] = h2

    def hypotheses(self) -> tuple[Hypothesis, ...]:
        """Todas las hipótesis registradas, en orden estable por id. Lo usa el
        adaptador para armar el 'expediente abierto' del contexto."""
        return tuple(self._hypotheses[hid] for hid in sorted(self._hypotheses))

    def hypothesis(self, hypothesis_id: str) -> Hypothesis:
        try:
            return self._hypotheses[hypothesis_id]
        except KeyError:
            raise UnknownId(
                f"No hay hipótesis registrada con id '{hypothesis_id}'."
            ) from None

    def hypotheses_of(self, question_id: str) -> tuple[Hypothesis, ...]:
        q = self.question(question_id)
        return tuple(self._hypotheses[hid] for hid in q.hypothesis_ids if hid in self._hypotheses)

    def link_evidence(
        self,
        hypothesis_id: str,
        evidence_id: str,
        *,
        polarity: LinkPolarity = LinkPolarity.SUPPORTS,
        note: str = "",
    ) -> Hypothesis:
        h = self.hypothesis(hypothesis_id)
        self.evidence(evidence_id)  # UnknownId si no existe: la procedencia no se inventa
        if len(h.evidence_links) >= MAX_LINKS_PER_HYPOTHESIS:
            raise LedgerLimitExceeded(
                f"La hipótesis '{hypothesis_id}' alcanzó el tope de "
                f"{MAX_LINKS_PER_HYPOTHESIS} vínculos de evidencia."
            )
        enlace = EvidenceLink(evidence_id=evidence_id, polarity=polarity, note=note)
        if enlace in h.evidence_links:
            return h  # idempotente
        at = self._now()
        h2 = replace(h, evidence_links=h.evidence_links + (enlace,))
        h2 = h2.with_history(
            HistoryEntry(
                at=at,
                kind="evidence_linked",
                detail=_detalle(evidence=evidence_id, polarity=polarity.value),
            )
        )
        self._validate(h2)
        self._hypotheses[hypothesis_id] = h2
        return h2

    def set_status(
        self, hypothesis_id: str, status: HypothesisStatus, *, reason: str = ""
    ) -> Hypothesis:
        """
        Cambia el estado. Pasa por `validate()`: un estado que exige respaldo
        exógeno no se puede alcanzar sin él, ni siquiera a mano.
        """
        h = self.hypothesis(hypothesis_id)
        at = self._now()
        h2 = replace(h, status=status)
        h2 = h2.with_history(
            HistoryEntry(
                at=at,
                kind="status",
                reason=reason,
                detail=_detalle(**{"from": h.status.value, "to": status.value}),
            )
        )
        self._validate(h2)
        self._hypotheses[hypothesis_id] = h2
        return h2

    def _normalizar_enlaces(
        self, enlaces: Iterable[EvidenceLink | tuple[str, LinkPolarity] | str]
    ) -> tuple[EvidenceLink, ...]:
        normalizados: list[EvidenceLink] = []
        for e in enlaces:
            if isinstance(e, EvidenceLink):
                normalizados.append(e)
            elif isinstance(e, str):
                normalizados.append(EvidenceLink(evidence_id=e))
            else:
                evidence_id, polarity = e
                normalizados.append(EvidenceLink(evidence_id=evidence_id, polarity=polarity))
        return tuple(normalizados)

    def _validate(self, h: Hypothesis) -> None:
        for link in h.evidence_links:
            if link.evidence_id not in self._evidence:
                raise UnknownId(
                    f"La hipótesis '{h.id}' apunta a evidencia desconocida "
                    f"'{link.evidence_id}'. Registrar la evidencia primero: "
                    "la procedencia no se puede inventar."
                )
        for dep in h.depends_on:
            if dep not in self._hypotheses:
                raise UnknownId(
                    f"'{h.id}' depende de '{dep}', que no existe en el ledger."
                )
        for otra in h.excludes:
            if otra not in self._hypotheses:
                raise UnknownId(
                    f"'{h.id}' excluye a '{otra}', que no existe en el ledger."
                )
        h.validate(exogenous_evidence_ids=self.exogenous_evidence_ids())

    # ------------------------------------------------------------------
    # Grafo: derivados
    # ------------------------------------------------------------------

    def has_exogenous_support(self, hypothesis_id: str) -> bool:
        """True si hay al menos un vínculo de sostén con evidencia exógena.
        Que el kernel te contradiga no es sostén — eso es `CONTRADICTED`."""
        h = self._hypotheses.get(hypothesis_id)
        if h is None:
            return False
        for link in h.evidence_links:
            if link.polarity is not LinkPolarity.SUPPORTS:
                continue
            ev = self._evidence.get(link.evidence_id)
            if ev is not None and ev.is_exogenous and ev.is_usable:
                return True
        return False

    def level(self, hypothesis_id: str) -> int | None:
        """
        Saltos especulativos hasta la evidencia exógena:
        `0` = sostenida directamente por evidencia exógena,
        `n` = a n saltos por dependencias,
        `None` = sin camino a evidencia exógena (isla) o parte de un ciclo.

        Se CALCULA. No existe campo para declararlo, y esa es la diferencia
        entre un sistema que sabe a qué distancia está de los datos y uno que
        cree lo que dice de sí mismo.
        """
        self.hypothesis(hypothesis_id)  # UnknownId si no existe
        memo: dict[str, int | None] = {}

        def visit(node: str, camino: frozenset[str]) -> int | None:
            if node in memo:
                return memo[node]
            if node in camino:
                return None  # ciclo: sin valor finito
            h = self._hypotheses.get(node)
            if h is None:
                return None
            if self.has_exogenous_support(node):
                memo[node] = 0
                return 0
            if not h.depends_on:
                memo[node] = None
                return None
            valores = [visit(dep, camino | {node}) for dep in h.depends_on]
            if any(v is None for v in valores):
                memo[node] = None
            else:
                memo[node] = 1 + max(v for v in valores if v is not None)
            return memo[node]

        return visit(hypothesis_id, frozenset())

    def _pares_en_conflicto(self) -> tuple[tuple[str, str], ...]:
        """
        Pares de hipótesis que se excluyen y están LAS DOS respaldadas por
        evidencia exógena, ordenados y sin repetir.

        **Qué significa "respaldada", y por qué NO es `SUPPORTED`.** Una primera
        versión de esto exigía `status is SUPPORTED` y estaba muerta: en el host
        NINGÚN camino pone `SUPPORTED` (el modelo no puede autodeclararse
        respaldado, y por eso el estado lo cambia la evidencia o un humano), así
        que el conflicto no se habría detectado nunca en una conversación real —
        la misma clase de trampa que un detector que no detecta nada. La noción
        alcanzable es la que el resto del ledger ya usa para "esto se apoya en el
        mundo": `has_exogenous_support()` (un vínculo de sostén con evidencia
        exógena usable), que es exactamente lo que marca
        `Flag.GROUNDED_IN_EXOGENOUS` y lo que exige `validate()`.

        Y no es ruido: para que haya conflicto hace falta que ALGUIEN haya
        declarado la exclusión, así que una pregunta con tres candidatos
        respaldados y ninguna exclusión declarada no marca nada — es una pregunta
        abierta, y para eso ya está la abstención.

        Se detecta el par mirando las dos direcciones, así que un estado con la
        exclusión declarada de un solo lado igual aparece acá (y el diagnóstico
        reporta la asimetría, que es un estado corrupto, en vez de esconderla).
        """
        respaldadas = {
            hid
            for hid, h in self._hypotheses.items()
            if (h.status is HypothesisStatus.SUPPORTED or self.has_exogenous_support(hid))
        }
        pares: list[tuple[str, str]] = []
        vistos: set[tuple[str, str]] = set()
        for hid in sorted(respaldadas):
            h = self._hypotheses[hid]
            for otra in sorted(h.excludes):
                if otra not in respaldadas:
                    continue
                par = (hid, otra) if hid < otra else (otra, hid)
                if par not in vistos:
                    vistos.add(par)
                    pares.append(par)
        return tuple(pares)

    def conflictos_de_sostén(self) -> tuple[tuple[str, str], ...]:
        """
        Las alternativas incompatibles que están las dos respaldadas por evidencia
        exógena (C). Es la
        consulta que el expediente y el gate usan para no elegir en silencio: el
        sistema puede mostrar dos conclusiones incompatibles, lo que no puede es
        presentar una como LA respuesta sin decirlo.
        """
        return self._pares_en_conflicto()

    def flags(self, hypothesis_id: str) -> frozenset[Flag]:
        """
        Marcas derivadas y mecánicas. Reemplazan a `confidence: float`: cada
        una es una regla, no un juicio, y ninguna pretende ser una
        probabilidad de verdad.
        """
        h = self.hypothesis(hypothesis_id)
        marcas: set[Flag] = set()

        if self.has_exogenous_support(hypothesis_id):
            marcas.add(Flag.GROUNDED_IN_EXOGENOUS)
        else:
            endogena = any(
                link.polarity is LinkPolarity.SUPPORTS
                and (ev := self._evidence.get(link.evidence_id)) is not None
                and ev.is_usable
                and not ev.is_exogenous
                for link in h.evidence_links
            )
            if endogena:
                marcas.add(Flag.ENDOGENOUS_ONLY)
            if not h.evidence_links and not h.depends_on:
                marcas.add(Flag.UNROOTED)

        contradictoria = any(
            link.polarity is LinkPolarity.CONTRADICTS
            and (ev := self._evidence.get(link.evidence_id)) is not None
            and ev.is_exogenous
            and ev.is_usable
            for link in h.evidence_links
        )
        if contradictoria:
            marcas.add(Flag.CONTRADICTED)

        # Se MARCA, no se borra: si una evidencia se pone en cuarentena después
        # de haber sido vinculada, eliminar el vínculo destruiría la
        # trazabilidad de que esta hipótesis se apoyó en material hoy
        # cuestionado — que es justo lo que hay que poder auditar.
        if any(
            (ev := self._evidence.get(link.evidence_id)) is not None and not ev.is_usable
            for link in h.evidence_links
        ):
            marcas.add(Flag.RESTING_ON_QUARANTINED)

        if self._reaches(hypothesis_id, hypothesis_id):
            marcas.add(Flag.CIRCULAR)
        if not h.has_falsifier:
            marcas.add(Flag.NO_FALSIFIER)
        if any(
            self._hypotheses[a].status in {HypothesisStatus.REFUTED, HypothesisStatus.SUPERSEDED}
            for a in self.ancestors(hypothesis_id)
        ):
            marcas.add(Flag.DEPENDENCY_REFUTED)
        if any(
            (ev := self._evidence.get(link.evidence_id)) is not None and not ev.anchor
            for link in h.evidence_links
        ):
            marcas.add(Flag.UNVERIFIABLE_PROVENANCE)

        # C: incompatible con otra alternativa que también está respaldada. Se
        # consulta por PARES (no por el lado propio) para que las dos hipótesis
        # del conflicto queden marcadas igual.
        if any(hypothesis_id in par for par in self._pares_en_conflicto()):
            marcas.add(Flag.CONFLICTING_SUPPORT)

        # D1: MARCA, no cambia estado. Una observación con ventana vencida deja de
        # sostener de verdad, pero que el tiempo pase no es una observación nueva:
        # el estado solo lo cambian las reglas declaradas. Lo que sí hace es
        # hacerlo VISIBLE — y sin esto `valid_until` sería un campo que se guarda
        # y no se lee, que es la trampa del detector inerte.
        ahora = self._now()
        if any(
            link.polarity is LinkPolarity.SUPPORTS
            and (ev := self._evidence.get(link.evidence_id)) is not None
            and ev.is_usable
            and ev.is_expired_at(ahora)
            for link in h.evidence_links
        ):
            marcas.add(Flag.EXPIRED_SUPPORT)

        return frozenset(marcas)

    def ancestors(self, hypothesis_id: str) -> frozenset[str]:
        """Dependencias transitivas (de qué depende esto, en última
        instancia)."""
        vistos: set[str] = set()
        pendientes = list(self.hypothesis(hypothesis_id).depends_on)
        while pendientes:
            actual = pendientes.pop()
            if actual in vistos or actual not in self._hypotheses:
                continue
            vistos.add(actual)
            pendientes.extend(self._hypotheses[actual].depends_on)
        return frozenset(vistos)

    def descendants(self, hypothesis_id: str) -> frozenset[str]:
        """Dependientes transitivos: qué se cae si esto se cae."""
        self.hypothesis(hypothesis_id)
        hijos: dict[str, list[str]] = {}
        for hid, h in self._hypotheses.items():
            for dep in h.depends_on:
                hijos.setdefault(dep, []).append(hid)
        vistos: set[str] = set()
        pendientes = list(hijos.get(hypothesis_id, ()))
        while pendientes:
            actual = pendientes.pop()
            if actual in vistos:
                continue
            vistos.add(actual)
            pendientes.extend(hijos.get(actual, ()))
        return frozenset(vistos)

    def _reaches(self, src: str, dst: str, visto: set[str] | None = None) -> bool:
        """¿`src` alcanza a `dst` siguiendo dependencias? Se usa para
        detectar ciclos (`_reaches(h, h)`)."""
        visto = visto if visto is not None else set()
        if src in visto:
            return False
        visto.add(src)
        h = self._hypotheses.get(src)
        if h is None:
            return False
        for dep in h.depends_on:
            if dep == dst or self._reaches(dep, dst, visto):
                return True
        return False

    # ------------------------------------------------------------------
    # Invalidación y propagación
    # ------------------------------------------------------------------

    def invalidate(self, hypothesis_id: str, *, reason: str = "") -> InvalidationReport:
        """
        Refuta una premisa y propaga. Distingue dos casos (ver docstring del
        módulo): un descendiente con respaldo exógeno propio queda solo
        MARCADO; uno que colgaba únicamente de la premisa caída se SUSPENDE.
        """
        self.set_status(hypothesis_id, HypothesisStatus.REFUTED, reason=reason)
        suspendidas: list[str] = []
        marcadas: list[str] = []
        for d in sorted(self.descendants(hypothesis_id)):
            if self.has_exogenous_support(d):
                marcadas.append(d)
            else:
                self.set_status(
                    d,
                    HypothesisStatus.SUSPENDED,
                    reason=f"dependencia refutada: {hypothesis_id}",
                )
                suspendidas.append(d)
        return InvalidationReport(
            refuted=hypothesis_id, suspended=tuple(suspendidas), flagged=tuple(marcadas)
        )

    # ------------------------------------------------------------------
    # Reapertura
    # ------------------------------------------------------------------

    def consider(self, evidence: Evidence | str) -> tuple[ReopenDecision, ...]:
        """
        Evalúa una evidencia nueva contra TODAS las condiciones del ledger y
        aplica lo que corresponda. Es el único camino por el que una hipótesis
        se reabre sola, y no pasa por el modelo en ningún punto.

        Dos reglas que hacen que el resultado NO dependa del orden de
        declaración de las condiciones:

        1. **Dedup por (condición, evidencia).** La misma evidencia repetida no
           vuelve a disparar la MISMA condición (invariante de F1: repetir no
           refuerza), pero sí puede disparar otra condición distinta: eso no es
           una repetición, es otra lectura del mismo dato.
        2. **Precedencia explícita** entre acciones que matchean la misma
           evidencia (`PRECEDENCIA_DE_ACCIONES`): gana la más cauta, y los
           vínculos de todas se registran igual.
        3. **Dos presupuestos**: por hipótesis y por pregunta. El segundo se
           evalúa con los contadores ya actualizados por las aplicaciones de
           esta misma llamada, así que dos hipótesis de la misma pregunta no
           pueden pasarse del tope entre las dos.

        Devuelve las decisiones que matchearon (las que no, se descartan en
        silencio: son la inmensa mayoría y no constituyen información).
        """
        ev = self.evidence(evidence) if isinstance(evidence, str) else self.add_evidence(evidence)
        decisiones: list[ReopenDecision] = []

        for hid in sorted(self._hypotheses):
            if self.reopen_count(hid) >= MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS:
                continue
            if self._presupuesto_de_pregunta_agotado(hid):
                continue
            coincidencias = self._condiciones_que_matchean(hid, ev)
            if not coincidencias:
                continue
            self._aplicar(hid, coincidencias, ev)
            for condicion, _ in coincidencias:
                self._applied.setdefault(hid, set()).add((condicion.id, ev.dedup_key))
            decisiones.extend(decision for _, decision in coincidencias)

        for rid in sorted(self._requests):
            req = self._requests[rid]
            if req.is_satisfied:
                continue
            # Acá la asimetría con las hipótesis es deliberada: un déficit es un
            # HECHO ("esto ya no falta"), no un estado con lecturas en pugna, así
            # que la primera condición que matchea lo salda y se termina. No hay
            # precedencia que aplicar.
            aplicadas_req = self._applied_requests.setdefault(rid, set())
            for rc in req.reopen_conditions:
                d = should_reopen(rc, ev, already_applied=frozenset(aplicadas_req))
                if not d.matched:
                    continue
                aplicadas_req.add(ev.dedup_key)
                self._requests[rid] = replace(req, satisfied_by=ev.id)
                self._anotar_deficit_satisfecho(rid, ev)
                decisiones.append(d)
                break

        return tuple(decisiones)

    def _condiciones_que_matchean(
        self, hypothesis_id: str, ev: Evidence
    ) -> list[tuple[ReopenCondition, ReopenDecision]]:
        """
        Evalúa TODAS las condiciones de la hipótesis y devuelve las que matchean,
        SIN aplicar nada todavía. La evaluación completa es lo que hace posible
        la precedencia: para elegir el estado más cauto hay que ver todas las
        acciones antes de escribir la primera.
        """
        resultado: list[tuple[ReopenCondition, ReopenDecision]] = []
        for rc in self._hypotheses[hypothesis_id].reopen_conditions:
            ya_aplicadas = frozenset(
                clave
                for condicion_id, clave in self._applied.get(hypothesis_id, set())
                if condicion_id == rc.id
            )
            decision = should_reopen(rc, ev, already_applied=ya_aplicadas)
            if decision.matched:
                resultado.append((rc, decision))
        return resultado

    def reopen_count(self, hypothesis_id: str) -> int:
        """
        Cuántas reaperturas automáticas se aplicaron a esta hipótesis. Es el
        contador de su presupuesto: acota cuánto ruido puede meter una condición
        demasiado genérica, sin depender de que la palabra clave sea buena.

        El presupuesto de la PREGUNTA se deriva de estos contadores
        (`reopen_count_of_question`), así que no hay estado duplicado que pueda
        quedar inconsistente ni que haya que migrar.
        """
        self.hypothesis(hypothesis_id)
        return self._reopen_count.get(hypothesis_id, 0)

    def reopen_count_of_question(self, question_id: str) -> int:
        """
        Cuántas reaperturas automáticas acumuló una PREGUNTA: la suma de las de
        sus hipótesis. Una hipótesis sin pregunta no entra en ningún presupuesto
        de pregunta (solo en el suyo), y eso es deliberado: agruparlas en un
        cajón común sería inventar una pregunta que nadie hizo.
        """
        q = self.question(question_id)
        return sum(
            self._reopen_count.get(hid, 0) for hid in q.hypothesis_ids if hid in self._hypotheses
        )

    def _presupuesto_de_pregunta_agotado(self, hypothesis_id: str) -> bool:
        qid = self._hypotheses[hypothesis_id].question_id
        if not qid:
            return False
        return self.reopen_count_of_question(qid) >= MAX_REOPEN_APPLICATIONS_PER_QUESTION

    def applied_evidence(self, hypothesis_id: str) -> frozenset[str]:
        """
        Claves de dedup ya aplicadas a esta hipótesis — lo que hace que la misma
        evidencia dos veces no cuente como dos veces.

        Es la UNIÓN sobre las condiciones (no distingue por cuál se aplicó): la
        pregunta que responde es "¿esta hipótesis ya vio este dato?", que es
        distinta de la que usa el dedup interno ("¿esta condición ya se aplicó a
        este dato?"). El detalle por condición vive en el historial.
        """
        self.hypothesis(hypothesis_id)
        return frozenset(clave for _, clave in self._applied.get(hypothesis_id, set()))

    def claves_deficit_satisfecho(self, request_id: str) -> frozenset[str]:
        """
        Claves de dedup con las que se saldó un déficit. Es el equivalente de
        `applied_evidence()` para los déficits: permite verificar (diagnosis.py)
        que lo saldado se apoya en una evidencia que existe.
        """
        self.request(request_id)
        return frozenset(self._applied_requests.get(request_id, set()))

    def trayectoria_de(self, hypothesis_id: str) -> "Trayectoria":
        """
        La biografía de una hipótesis, derivada de su historial: qué pasó, en qué
        orden y por qué. Es una vista, no un campo guardado — el estado actual es
        una foto, y una foto no puede mostrar que volvió de una refutación.
        """
        from kal_epistemics.diagnosis import trayectoria_de as _trayectoria_de

        return _trayectoria_de(self.hypothesis(hypothesis_id), self)

    def _aplicar(
        self,
        hypothesis_id: str,
        coincidencias: list[tuple[ReopenCondition, ReopenDecision]],
        ev: Evidence,
    ) -> Hypothesis:
        """
        Aplica TODAS las condiciones que matchearon la misma evidencia.

        El ESTADO lo decide la acción de mayor precedencia (la más cauta), no la
        última que se procesó: si dependiera del orden de la tupla de
        condiciones, el resultado cambiaría según cómo se declararon, y el orden
        no es información. Los vínculos, en cambio, se registran para todas: que
        la misma observación sostenga y contradiga a la vez es un dato, y
        perderlo sería perder la mitad de la señal (es lo que hace visible
        `Flag.CONTRADICTED`).

        El presupuesto de reaperturas se consume UNA vez por evidencia, no una
        por condición: el ruido que acota es la evidencia, no cuántas lecturas
        de esa evidencia se declararon.
        """
        h = self.hypothesis(hypothesis_id)
        acciones = {decision.action for _, decision in coincidencias}
        accion = next(a for a in PRECEDENCIA_DE_ACCIONES if a in acciones)
        nuevo_estado = {
            ReopenAction.REOPEN: HypothesisStatus.REACTIVATED,
            ReopenAction.WEAKEN: HypothesisStatus.WEAKENED,
            ReopenAction.REFUTE: HypothesisStatus.REFUTED,
        }[accion]

        enlaces = h.evidence_links
        for condition, decision in coincidencias:
            polaridad = (
                LinkPolarity.SUPPORTS
                if decision.action is ReopenAction.REOPEN
                else LinkPolarity.CONTRADICTS
            )
            if not any(l.evidence_id == ev.id and l.polarity is polaridad for l in enlaces):
                enlaces = enlaces + (
                    EvidenceLink(
                        evidence_id=ev.id, polarity=polaridad, note=condition.description
                    ),
                )

        at = self._now()
        h2 = replace(h, status=nuevo_estado, evidence_links=enlaces)
        for condition, decision in coincidencias:
            h2 = h2.with_history(
                HistoryEntry(
                    at=at,
                    kind=decision.action.value,
                    reason=condition.description,
                    detail=_detalle(
                        evidence=ev.id, condition=condition.id, origin=ev.origin.value
                    ),
                )
            )
        if len(acciones) > 1:
            # Se registra explícitamente que hubo precedencia: sin esta entrada,
            # el historial mostraría dos acciones y un solo estado sin decir por
            # qué, y la regla parecería arbitraria en la auditoría.
            h2 = h2.with_history(
                HistoryEntry(
                    at=at,
                    kind="precedence",
                    reason=f"la misma evidencia disparó {len(acciones)} acciones; gana {accion.value}",
                    detail=_detalle(
                        actions=",".join(sorted(a.value for a in acciones)),
                        status=nuevo_estado.value,
                    ),
                )
            )
        self._validate(h2)
        self._hypotheses[hypothesis_id] = h2
        self._reopen_count[hypothesis_id] = self.reopen_count(hypothesis_id) + 1
        return h2

    def _anotar_deficit_satisfecho(self, request_id: str, ev: Evidence) -> None:
        for qid, q in list(self._questions.items()):
            if request_id not in q.missing:
                continue
            at = self._now()
            q2 = q.with_history(
                HistoryEntry(
                    at=at,
                    kind="deficit_satisfied",
                    detail=_detalle(request=request_id, evidence=ev.id),
                )
            )
            self._questions[qid] = replace(q2, last_reviewed_at=at)

    # ------------------------------------------------------------------
    # Estado durable: snapshot y compactación
    # ------------------------------------------------------------------
    #
    # Estas dos piezas son la parte de F4 que NO depende de la decisión D4
    # (dónde se guarda): el estado serializable y la política de qué se tira. El
    # backend —SQLite, JSONL, o lo que se decida— escribe y lee esto, nada más.
    # Están acá y no en el host a propósito: quien persiste un ledger tiene que
    # poder confiar en que el ledger sabe reconstruirse igual, y eso es una
    # propiedad del contrato, no del motor de almacenamiento.

    def snapshot(self) -> dict[str, Any]:
        """
        Estado COMPLETO del ledger, serializable y determinista: dos ledgers en
        el mismo estado producen el mismo diccionario (y por lo tanto el mismo
        hash), sin importar el orden en que se registraron las cosas.

        Incluye lo que no se ve en los objetos públicos y también hay que
        preservar para que el ledger siga comportándose igual después de
        recargar: qué evidencia ya se aplicó a cada hipótesis (por condición),
        qué déficits ya se saldaron, y el presupuesto de reaperturas consumido.
        Sin eso, recargar convertiría una hipótesis ya reabierta en una reabrible
        otra vez — que es el bucle de reaperturas que R8 prohíbe.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "evidence": [self._evidence[eid].to_dict() for eid in sorted(self._evidence)],
            "hypotheses": [self._hypotheses[hid].to_dict() for hid in sorted(self._hypotheses)],
            "questions": [self._questions[qid].to_dict() for qid in sorted(self._questions)],
            "requests": [self._requests[rid].to_dict() for rid in sorted(self._requests)],
            "applied": [
                [hid, condicion_id, clave]
                for hid in sorted(self._applied)
                for condicion_id, clave in sorted(self._applied[hid])
            ],
            "applied_requests": [
                [rid, clave]
                for rid in sorted(self._applied_requests)
                for clave in sorted(self._applied_requests[rid])
            ],
            "reopen_count": [[hid, n] for hid, n in sorted(self._reopen_count.items())],
        }

    @classmethod
    def from_snapshot(
        cls, datos: Mapping[str, Any], *, clock: Callable[[], float] | None = None
    ) -> "HypothesisLedger":
        """
        Reconstruye el ledger, y FALLA CERRADO si el estado es inconsistente:
        versión de esquema distinta, referencia a algo que no está, o una clave
        canónica que no corresponde al texto de su pregunta.

        No re-valida "por las dudas" ni corrige: reinterpretar datos de
        procedencia con el esquema equivocado, o completar un vínculo que falta,
        corrompe la trazabilidad en silencio — que es lo único que esta capa
        tiene para ofrecer.
        """
        _check_schema(datos)
        led = cls(clock=clock or time.time)

        for crudo in datos.get("evidence", ()):
            ev = Evidence.from_dict(crudo)
            led._evidence[ev.id] = ev
        for crudo in datos.get("hypotheses", ()):
            h = Hypothesis.from_dict(crudo)
            led._hypotheses[h.id] = h
        for crudo in datos.get("questions", ()):
            q = Question.from_dict(crudo)
            if canonical_key(q.text) != q.canonical_key:
                raise SchemaVersionMismatch(
                    f"La pregunta '{q.id}' tiene una clave canónica que no "
                    f"corresponde a su texto. El estado guardado está corrupto: "
                    "no se reinterpreta."
                )
            if q.canonical_key in led._questions_by_key:
                raise DuplicateId(
                    f"Dos preguntas con la misma clave canónica "
                    f"'{q.canonical_key}' en el estado guardado."
                )
            led._questions[q.id] = q
            led._questions_by_key[q.canonical_key] = q.id
        for crudo in datos.get("requests", ()):
            req = EvidenceRequest.from_dict(crudo)
            led._requests[req.id] = req

        # Las referencias se verifican DESPUÉS de cargar todo: así una cadena de
        # dependencias (o un ciclo inyectado a mano) no impone un orden de
        # reconstrucción que el ledger nunca prometió.
        for hid in led._hypotheses:
            led._validate(led._hypotheses[hid])
        for h in led._hypotheses.values():
            if not h.question_id:
                continue
            if h.question_id not in led._questions:
                raise UnknownId(
                    f"La hipótesis '{h.id}' apunta a la pregunta desconocida "
                    f"'{h.question_id}'."
                )
            if h.id not in led._questions[h.question_id].hypothesis_ids:
                raise UnknownId(
                    f"La pregunta '{h.question_id}' no lista a su hipótesis '{h.id}': "
                    "el estado guardado está inconsistente."
                )
        for q in led._questions.values():
            for hid in q.hypothesis_ids:
                if hid not in led._hypotheses:
                    raise UnknownId(f"La pregunta '{q.id}' apunta a la hipótesis desconocida '{hid}'.")
            for rid in q.missing:
                if rid not in led._requests:
                    raise UnknownId(f"La pregunta '{q.id}' apunta al déficit desconocido '{rid}'.")
        for rid in led._requests:
            if not any(rid in q.missing for q in led._questions.values()):
                raise UnknownId(f"El déficit '{rid}' no pertenece a ninguna pregunta.")

        for hid, condicion_id, clave in datos.get("applied", ()):
            if hid not in led._hypotheses:
                raise UnknownId(f"Evidencia aplicada a la hipótesis desconocida '{hid}'.")
            led._applied.setdefault(hid, set()).add((condicion_id, clave))
        for rid, clave in datos.get("applied_requests", ()):
            if rid not in led._requests:
                raise UnknownId(f"Déficit desconocido '{rid}' en applied_requests.")
            led._applied_requests.setdefault(rid, set()).add(clave)
        for hid, n in datos.get("reopen_count", ()):
            if hid not in led._hypotheses:
                raise UnknownId(f"Contador de reaperturas de la hipótesis desconocida '{hid}'.")
            led._reopen_count[hid] = int(n)
        return led

    def compact(self, presupuesto: PresupuestoDeCompactacion | None = None) -> CompactionReport:
        """
        Compacta por presupuesto y devuelve qué se hizo. Ver `compaction.py`
        para las reglas: no se toca nada estructural, no se compacta ninguna
        hipótesis viva, no se pierde ningún déficit, y la operación es
        idempotente.

        Se verifica explícitamente (`conserva_lo_irrenunciable`) antes de
        escribir: la promesa del docstring es una condición ejecutable, no una
        convención.
        """
        presupuesto = presupuesto or PresupuestoDeCompactacion()
        pendientes = sum(len(self.missing_descriptions(q.id)) for q in self._questions.values())
        cambios, reporte = aplicar(
            self._hypotheses.values(), presupuesto, at=self._now(), deficits_pendientes=pendientes
        )
        for hid, compactada in cambios.items():
            if not conserva_lo_irrenunciable(self._hypotheses[hid], compactada):
                raise InvalidHypothesis(
                    f"La compactación de '{hid}' tocó algo estructural: se aborta sin escribir."
                )
            self._hypotheses[hid] = compactada
        return reporte

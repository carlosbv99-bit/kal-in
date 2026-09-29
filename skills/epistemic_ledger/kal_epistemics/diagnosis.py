"""
Introspección sobre el ledger: monitores de la HISTORIA y trayectoria derivada.

De dónde sale esto (mejora A y B, 2026-09-29). Una revisión de literatura
encontró dos ideas que ya existían en investigación y que valía la pena traer:
los *monitores introspectivos* sobre las creencias propias (Herd, Kelly y Stolle,
"Temporal Monitoring of Agent Beliefs Under Uncertainty with Subjective LTL",
AAAI-SS 2026) y la *trayectoria* de una creencia en vez de su estado puntual. No
se trajo nada de su formalismo (Subjective Logic con grados de creencia): acá no
hay números que nadie pueda computar, y el ledger sigue siendo estructural. Lo
que se trajo es la pregunta, que es la parte útil.

**Por qué los monitores no son redundantes con lo que ya había.** El ledger
valida en cada mutación (`Hypothesis.validate`, `ledger._validate`) y
`from_snapshot` falla cerrado ante un estado inconsistente. Pero esa validación
mira la ESTRUCTURA: referencias que existen, vínculos duplicados, un estado que
exige respaldo exógeno, la clave canónica de una pregunta. Lo que no mira es si
la historia *tiene sentido*: un snapshot puede ser estructuralmente impecable y
afirmar que una hipótesis se reabrió siete veces sin que su historial registre
una sola reapertura, o que consumió un presupuesto de reaperturas que no gastó.
Eso importa porque el snapshot viene de afuera —de un archivo, de SQLite, de un
agente de terceros que usa la fachada— y porque el contador de reaperturas es lo
que acota el ruido (R8): inflarlo agota el presupuesto de una hipótesis para
siempre, y vaciarlo lo desactiva.

Los monitores chequean entonces lo que la carga NO chequea, y solo eso: si
re-chequearan los invariantes de escritura estarían duplicando una regla que ya
tiene un solo dueño. Cuando el historial fue podado por compactación, lo que ya
no se puede verificar se reporta como **no verificable con su motivo**, no como
violación: un ledger compactado legítimamente no tiene el detalle, y confundir
"no lo sé" con "está roto" es exactamente el error que esta capa existe para no
cometer.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from kal_epistemics.compaction import KIND_MARCA_DE_COMPACTACION
from kal_epistemics.models import Hypothesis, HypothesisStatus, ReopenAction
from kal_epistemics.ledger import PRECEDENCIA_DE_ACCIONES
from kal_epistemics.safety import (
    MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS,
    MAX_REOPEN_APPLICATIONS_PER_QUESTION,
)

# Severidades. "inconsistencia" es un estado imposible producido por el ledger;
# "no_verificable" es un invariante que no se pudo chequear por falta de datos
# (historial compactado). Nunca se mezclan: la segunda no es un problema.
INCONSISTENCIA = "inconsistencia"
NO_VERIFICABLE = "no_verificable"

# Kinds del historial que CAMBIAN el estado de una hipótesis. Todo lo demás
# (vínculos, precedencia, marcas de compactación, eventos de pregunta) son
# anotaciones: si un kind nuevo cambiara el estado sin estar acá, el invariante
# `estado_declarado` lo reportaría como discrepancia — que es la señal correcta,
# porque significa que este módulo quedó desactualizado.
_KIND_A_ESTADO: dict[str, HypothesisStatus] = {
    "generated": HypothesisStatus.OPEN,
    ReopenAction.REOPEN.value: HypothesisStatus.REACTIVATED,
    ReopenAction.WEAKEN.value: HypothesisStatus.WEAKENED,
    ReopenAction.REFUTE.value: HypothesisStatus.REFUTED,
}
_ESTADOS_POR_VALOR = {estado.value: estado for estado in HypothesisStatus}


def _detalle(entrada: Any, clave: str) -> str:
    for k, v in entrada.detail:
        if k == clave:
            return v
    return ""


@dataclass(frozen=True)
class Hallazgo:
    """Un invariante que no se cumple (o que no se pudo chequear)."""

    invariante: str
    severidad: str
    sujeto: str
    detalle: str

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Diagnostico:
    """
    Resultado de los monitores. `sano` es FALSO solo si hay inconsistencias:
    un invariante no verificable no vuelve insano a un ledger, porque no dice
    nada sobre él.
    """

    hallazgos: tuple[Hallazgo, ...] = ()
    verificados: tuple[str, ...] = ()
    no_verificables: tuple[str, ...] = ()

    @property
    def inconsistencias(self) -> tuple[Hallazgo, ...]:
        return tuple(h for h in self.hallazgos if h.severidad == INCONSISTENCIA)

    @property
    def sano(self) -> bool:
        return not self.inconsistencias

    def to_dict(self) -> dict[str, Any]:
        return {
            "sano": self.sano,
            "verificados": list(self.verificados),
            "no_verificables": list(self.no_verificables),
            "hallazgos": [h.to_dict() for h in self.hallazgos],
        }

    def resumen(self) -> str:
        if self.sano and not self.no_verificables:
            return f"Ledger consistente ({len(self.verificados)} invariantes verificados)."
        partes = []
        if self.inconsistencias:
            partes.append(
                f"{len(self.inconsistencias)} inconsistencia(s): "
                + "; ".join(f"{h.invariante} en {h.sujeto}" for h in self.inconsistencias[:3])
            )
        if self.no_verificables:
            partes.append(
                f"{len(self.no_verificables)} invariante(s) no verificable(s) "
                "(historial compactado)"
            )
        if self.sano and not self.no_verificables:
            partes.append(f"{len(self.verificados)} invariantes verificados")
        return ". ".join(partes) + "."


@dataclass(frozen=True)
class Transicion:
    """Un paso de la biografía de una hipótesis."""

    at: float
    kind: str
    desde: str | None
    hacia: str | None
    motivo: str = ""
    evidencia: str = ""
    condicion: str = ""
    origen: str = ""
    lecturas: tuple[str, ...] = ()

    @property
    def cambia_estado(self) -> bool:
        return self.hacia is not None and self.hacia != self.desde

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"cambia_estado": self.cambia_estado}


@dataclass(frozen=True)
class Trayectoria:
    """
    La vida de una hipótesis como secuencia de transiciones, no como un estado.

    Sirve para lo que un estado no puede mostrar: que una hipótesis
    `REACTIVATED` puede haber pasado por `REFUTED` y vuelto, o que se reabrió
    cinco veces por condiciones genéricas. La volatilidad es información, y
    esconderla detrás de un estado actual es la forma más fácil de que una
    hipótesis parezca más firme de lo que es — que es el error que esta capa
    existe para evitar.

    `historial_podado` dice si faltan pasos: después de una compactación la
    trayectoria es la que sobrevivió, no la completa, y quien la lea tiene que
    poder saberlo.
    """

    hipotesis_id: str
    claim: str
    estado: str
    nivel: int | None
    flags: tuple[str, ...]
    transiciones: tuple[Transicion, ...]
    historial_podado: bool = False
    entradas_omitidas: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hipotesis_id": self.hipotesis_id,
            "claim": self.claim,
            "estado": self.estado,
            "nivel": self.nivel,
            "flags": list(self.flags),
            "historial_podado": self.historial_podado,
            "entradas_omitidas": self.entradas_omitidas,
            "transiciones": [t.to_dict() for t in self.transiciones],
        }


# --------------------------------------------------------------------------
# Derivados: estado desde el historial, y trayectoria
# --------------------------------------------------------------------------


def _podado(history: tuple[Any, ...]) -> tuple[bool, int]:
    """Si el historial fue podado, y cuántas entradas se tiraron (según la marca
    que deja el compactador)."""
    for entrada in history:
        if entrada.kind != KIND_MARCA_DE_COMPACTACION:
            continue
        originales = _detalle(entrada, "history_entries_originales")
        conservadas = _detalle(entrada, "history_entries_conservadas")
        if originales.isdigit() and conservadas.isdigit():
            return True, max(0, int(originales) - int(conservadas))
        return True, 0
    return False, 0


@dataclass(frozen=True)
class _Aplicacion:
    """
    Las entradas de historial que produjo UNA evidencia sobre UNA hipótesis.

    Agrupar no es un detalle de presentación: `_aplicar()` deja una entrada por
    cada condición que matcheó (a propósito: que la misma observación sostenga y
    contradiga a la vez es un dato) y el ESTADO lo decide la precedencia
    (`PRECEDENCIA_DE_ACCIONES`), no el orden en que quedaron escritas. Leer las
    entradas como transiciones secuenciales da un resultado que depende del orden
    de declaración de las condiciones — el mismo error que D10 corrigió en el
    ledger, reintroducido en la lectura.
    """

    evidencia: str
    entradas: tuple[Any, ...]
    precedencia: Any | None = None

    @property
    def acciones(self) -> tuple[ReopenAction, ...]:
        return tuple(ReopenAction(e.kind) for e in self.entradas)

    @property
    def ganadora(self) -> ReopenAction:
        acciones = set(self.acciones)
        return next(a for a in PRECEDENCIA_DE_ACCIONES if a in acciones)

    @property
    def ganadora_entrada(self) -> Any:
        for entrada in self.entradas:
            if ReopenAction(entrada.kind) is self.ganadora:
                return entrada
        return self.entradas[0]

    @property
    def motivo(self) -> str:
        partes = [self.ganadora_entrada.reason] if self.ganadora_entrada.reason else []
        if len(self.acciones) > 1:
            partes.append(
                "la misma evidencia disparó "
                + ", ".join(sorted(a.value for a in self.acciones))
                + f"; gana {self.ganadora.value}"
            )
        if self.precedencia is not None and self.precedencia.reason:
            partes.append(self.precedencia.reason)
        return "; ".join(partes)


def _pasos(history: tuple[Any, ...]) -> list[Any]:
    """
    El historial en pasos: entradas sueltas (vínculos, estados) y aplicaciones
    agrupadas. La marca de compactación no es un paso: es la constancia de que
    faltan pasos.
    """
    pasos: list[Any] = []
    actual: _Aplicacion | None = None

    def cerrar() -> None:
        nonlocal actual
        if actual is not None:
            pasos.append(actual)
            actual = None

    for entrada in history:
        if entrada.kind == KIND_MARCA_DE_COMPACTACION:
            cerrar()
            continue
        if entrada.kind in _KINDS_DE_ACCION:
            evidencia = _detalle(entrada, "evidence")
            if actual is not None and actual.evidencia == evidencia and actual.precedencia is None:
                actual = _Aplicacion(evidencia, actual.entradas + (entrada,))
            else:
                cerrar()
                actual = _Aplicacion(evidencia, (entrada,))
            continue
        if (
            entrada.kind == "precedence"
            and actual is not None
            and len(actual.entradas) > 1
            and actual.precedencia is None
        ):
            actual = _Aplicacion(actual.evidencia, actual.entradas, entrada)
            continue
        cerrar()
        pasos.append(entrada)
    cerrar()
    return pasos


def _estado_derivado(history: tuple[Any, ...]) -> tuple[HypothesisStatus | None, str]:
    """
    Reconstruye el estado recorriendo el historial POR APLICACIÓN (no entrada por
    entrada). Devuelve `(None, motivo)` cuando no se puede saber —por ejemplo un
    `status` sin su detalle, o un historial que no fija ningún estado— porque
    adivinar sería peor que decir que no se sabe.
    """
    estado: HypothesisStatus | None = None
    for paso in _pasos(history):
        if isinstance(paso, _Aplicacion):
            estado = _KIND_A_ESTADO[paso.ganadora.value]
        elif paso.kind in _KIND_A_ESTADO:
            estado = _KIND_A_ESTADO[paso.kind]
        elif paso.kind == "status":
            valor = _detalle(paso, "to")
            if valor not in _ESTADOS_POR_VALOR:
                return None, f"entrada de estado sin 'to' legible ({valor!r})"
            estado = _ESTADOS_POR_VALOR[valor]
    if estado is None:
        return None, "el historial no tiene ninguna entrada que fije el estado"
    return estado, ""


def trayectoria_de(hipotesis: Hypothesis, ledger: Any = None) -> Trayectoria:
    """
    Deriva la trayectoria del historial ya grabado. `ledger` es opcional y solo
    se usa para completar nivel y flags (que son propiedades del grafo, no del
    nodo: sin ledger quedan vacíos, y eso se ve, no se inventa).

    Una aplicación con varias lecturas de la misma evidencia produce UNA
    transición con esas lecturas como detalle: el estado que resulta es el que
    decide la precedencia.
    """
    podado, omitidas = _podado(hipotesis.history)
    estado: HypothesisStatus | None = None
    transiciones: list[Transicion] = []

    for paso in _pasos(hipotesis.history):
        desde = estado.value if estado else None
        if isinstance(paso, _Aplicacion):
            estado = _KIND_A_ESTADO[paso.ganadora.value]
            ganadora = paso.ganadora_entrada
            transiciones.append(
                Transicion(
                    at=ganadora.at,
                    kind=paso.ganadora.value,
                    desde=desde,
                    hacia=estado.value,
                    motivo=paso.motivo,
                    evidencia=paso.evidencia,
                    condicion=_detalle(ganadora, "condition"),
                    origen=_detalle(ganadora, "origin"),
                    lecturas=tuple(sorted(a.value for a in paso.acciones)),
                )
            )
            continue
        if paso.kind in _KIND_A_ESTADO:
            estado = _KIND_A_ESTADO[paso.kind]
        elif paso.kind == "status":
            valor = _detalle(paso, "to")
            estado = _ESTADOS_POR_VALOR.get(valor, estado)
        es_cambio = paso.kind in _KIND_A_ESTADO or paso.kind == "status"
        transiciones.append(
            Transicion(
                at=paso.at,
                kind=paso.kind,
                desde=desde,
                hacia=estado.value if (es_cambio and estado) else None,
                motivo=paso.reason,
                evidencia=_detalle(paso, "evidence"),
                condicion=_detalle(paso, "condition"),
                origen=_detalle(paso, "origin"),
            )
        )

    nivel = None
    flags: tuple[str, ...] = ()
    if ledger is not None:
        nivel = ledger.level(hipotesis.id)
        flags = tuple(sorted(f.value for f in ledger.flags(hipotesis.id)))

    return Trayectoria(
        hipotesis_id=hipotesis.id,
        claim=hipotesis.claim,
        estado=hipotesis.status.value,
        nivel=nivel,
        flags=flags,
        transiciones=tuple(transiciones),
        historial_podado=podado,
        entradas_omitidas=omitidas,
    )


# --------------------------------------------------------------------------
# Monitores
# --------------------------------------------------------------------------

_KINDS_DE_ACCION = frozenset(a.value for a in ReopenAction)


def _chequear_reaperturas(ledger: Any, h: Hypothesis, hallazgos: list[Hallazgo]) -> bool:
    """
    `reopen_count` tiene que explicarse por el historial: es el contador que
    acota el ruido (R8) y el que decide el dedup, así que un contador que no
    corresponde a lo que pasó agota un presupuesto que nadie gastó o desactiva
    un tope que sí se gastó.

    Devuelve si el chequeo pudo hacerse completo.
    """
    cuenta = ledger.reopen_count(h.id)
    entradas = [e for e in h.history if e.kind in _KINDS_DE_ACCION]
    evidencias = {_detalle(e, "evidence") for e in entradas}
    evidencias.discard("")
    aplicadas = ledger.applied_evidence(h.id)
    podado, _ = _podado(h.history)

    # Verificable SIEMPRE, incluso con historial podado: cada aplicación consume
    # exactamente una unidad de presupuesto y deja al menos una entrada.
    if cuenta < len(evidencias):
        hallazgos.append(
            Hallazgo(
                invariante="reaperturas_vs_historial",
                severidad=INCONSISTENCIA,
                sujeto=h.id,
                detalle=(
                    f"el contador dice {cuenta} reaperturas pero el historial conserva "
                    f"{len(evidencias)} evidencia(s) que las dispararon"
                ),
            )
        )
    # El dedup y el contador se escriben juntos: si uno existe sin el otro, la
    # misma evidencia podría volver a contar (D10 reintroducido) o se estaría
    # suprimiendo una aplicación sin respaldo.
    if cuenta > 0 and not aplicadas:
        hallazgos.append(
            Hallazgo(
                invariante="reaperturas_sin_dedup",
                severidad=INCONSISTENCIA,
                sujeto=h.id,
                detalle=(
                    f"{cuenta} reaperturas contadas y ninguna clave de dedup: la misma "
                    "evidencia volvería a contar"
                ),
            )
        )
    if aplicadas and cuenta == 0:
        hallazgos.append(
            Hallazgo(
                invariante="dedup_sin_reaperturas",
                severidad=INCONSISTENCIA,
                sujeto=h.id,
                detalle=(
                    f"{len(aplicadas)} clave(s) de dedup con el contador en cero: se "
                    "suprimiría una aplicación que nunca se contó"
                ),
            )
        )
    if podado:
        return False
    if cuenta != len(evidencias):
        hallazgos.append(
            Hallazgo(
                invariante="reaperturas_vs_historial",
                severidad=INCONSISTENCIA,
                sujeto=h.id,
                detalle=(
                    f"el contador dice {cuenta} reaperturas y el historial completo "
                    f"registra {len(evidencias)}"
                ),
            )
        )
    return True


def _chequear_estado(ledger: Any, h: Hypothesis, hallazgos: list[Hallazgo]) -> bool:
    """El estado declarado tiene que ser el que la historia produce."""
    derivado, motivo = _estado_derivado(h.history)
    if derivado is None:
        hallazgos.append(
            Hallazgo(
                invariante="estado_declarado",
                severidad=NO_VERIFICABLE,
                sujeto=h.id,
                detalle=f"no se pudo derivar el estado del historial: {motivo}",
            )
        )
        return False
    if derivado is not h.status:
        hallazgos.append(
            Hallazgo(
                invariante="estado_declarado",
                severidad=INCONSISTENCIA,
                sujeto=h.id,
                detalle=(
                    f"dice estar {h.status.value} y su historial termina en "
                    f"{derivado.value}"
                ),
            )
        )
    return True


def _chequear_aplicado(ledger: Any, h: Hypothesis, hallazgos: list[Hallazgo]) -> bool:
    """
    Cada clave de dedup aplicada tiene que corresponder a una evidencia conocida,
    vinculada a esta hipótesis y —si el historial está completo— presente en el
    historial. Una clave huérfana suprime para siempre una aplicación legítima.
    """
    claves = ledger.applied_evidence(h.id)
    if not claves:
        return True
    evidencias_en_historial = {
        _detalle(e, "evidence") for e in h.history if e.kind in _KINDS_DE_ACCION
    }
    vincular = {link.evidence_id for link in h.evidence_links}
    podado, _ = _podado(h.history)
    completo = True

    for clave in sorted(claves):
        ev = ledger.evidence_by_dedup_key(clave)
        if ev is None:
            hallazgos.append(
                Hallazgo(
                    invariante="aplicado_sin_evidencia",
                    severidad=INCONSISTENCIA,
                    sujeto=h.id,
                    detalle=f"clave de dedup '{clave[:12]}…' sin evidencia que la respalde",
                )
            )
            completo = False
            continue
        if ev.id not in vincular:
            hallazgos.append(
                Hallazgo(
                    invariante="aplicado_sin_vinculo",
                    severidad=INCONSISTENCIA,
                    sujeto=h.id,
                    detalle=(
                        f"'{ev.id}' figura como aplicada y no está vinculada a la hipótesis"
                    ),
                )
            )
            completo = False
        if not podado and ev.id not in evidencias_en_historial:
            hallazgos.append(
                Hallazgo(
                    invariante="aplicado_sin_historial",
                    severidad=INCONSISTENCIA,
                    sujeto=h.id,
                    detalle=(
                        f"'{ev.id}' figura como aplicada y el historial completo no "
                        "registra la aplicación"
                    ),
                )
            )
            completo = False
    return completo and not podado


def _chequear_deficits(ledger: Any, hallazgos: list[Hallazgo]) -> bool:
    """Un déficit saldado tiene que apuntar a una evidencia que existe."""
    completo = True
    for q in ledger.all_questions():
        for rid in q.missing:
            claves = ledger.claves_deficit_satisfecho(rid)
            if not claves:
                continue
            req = ledger.request(rid)
            if not req.is_satisfied:
                hallazgos.append(
                    Hallazgo(
                        invariante="deficit_saldado_sin_evidencia",
                        severidad=INCONSISTENCIA,
                        sujeto=rid,
                        detalle=(
                            f"tiene {len(claves)} clave(s) aplicada(s) y sigue sin "
                            "satisfacerse"
                        ),
                    )
                )
                completo = False
            for clave in sorted(claves):
                if ledger.evidence_by_dedup_key(clave) is None:
                    hallazgos.append(
                        Hallazgo(
                            invariante="deficit_saldado_sin_evidencia",
                            severidad=INCONSISTENCIA,
                            sujeto=rid,
                            detalle=f"clave '{clave[:12]}…' sin evidencia que la respalde",
                        )
                    )
                    completo = False
    return completo


def _chequear_techos(ledger: Any, hallazgos: list[Hallazgo]) -> bool:
    """
    Los techos de reapertura (R8) se midieron para acotar ruido; un contador por
    encima del techo es un presupuesto agotado por un estado que no pasó por el
    camino que lo consume, y uno muy por debajo es un techo desactivado.
    """
    for h in ledger.hypotheses():
        if ledger.reopen_count(h.id) > MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS:
            hallazgos.append(
                Hallazgo(
                    invariante="techo_de_reaperturas",
                    severidad=INCONSISTENCIA,
                    sujeto=h.id,
                    detalle=(
                        f"{ledger.reopen_count(h.id)} reaperturas superan el techo por "
                        f"hipótesis ({MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS})"
                    ),
                )
            )
    for q in ledger.all_questions():
        cuenta = ledger.reopen_count_of_question(q.id)
        if cuenta > MAX_REOPEN_APPLICATIONS_PER_QUESTION:
            hallazgos.append(
                Hallazgo(
                    invariante="techo_de_reaperturas",
                    severidad=INCONSISTENCIA,
                    sujeto=q.id,
                    detalle=(
                        f"{cuenta} reaperturas superan el techo por pregunta "
                        f"({MAX_REOPEN_APPLICATIONS_PER_QUESTION})"
                    ),
                )
            )
    return True


def diagnosticar(ledger: Any) -> Diagnostico:
    """
    Corre los monitores sobre el ledger. Barato (un recorrido del estado, sin
    tocar la red ni la evidencia) y sin efectos: no corrige nada.

    Uso previsto: al cargar un estado guardado (para que un snapshot absurdo se
    vea en vez de gobernarse en silencio), y como verificación de terceros sobre
    un ledger que llegó de afuera.
    """
    hallazgos: list[Hallazgo] = []
    completos: dict[str, bool] = {}

    completos["reaperturas_vs_historial"] = all(
        _chequear_reaperturas(ledger, h, hallazgos) for h in ledger.hypotheses()
    )
    completos["estado_declarado"] = all(
        _chequear_estado(ledger, h, hallazgos) for h in ledger.hypotheses()
    )
    completos["aplicado_coherente"] = all(
        _chequear_aplicado(ledger, h, hallazgos) for h in ledger.hypotheses()
    )
    completos["deficit_saldado"] = _chequear_deficits(ledger, hallazgos)
    completos["techo_de_reaperturas"] = _chequear_techos(ledger, hallazgos)

    verificados = tuple(k for k, v in completos.items() if v)
    no_verificables = tuple(k for k, v in completos.items() if not v)
    return Diagnostico(
        hallazgos=tuple(hallazgos), verificados=verificados, no_verificables=no_verificables
    )

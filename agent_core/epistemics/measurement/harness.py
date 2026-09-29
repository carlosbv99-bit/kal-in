"""
El harness de F6: corre el corpus por el CAMINO REAL y saca los números.

Qué es real y qué está scripteado, porque de eso depende cuánto valen los
números (y decirlo es parte del trabajo, no un descargo):

REAL — y es todo lo que esta suite pretende medir:

- `EpistemicStore` real, con ledger real por sesión y evidencia real.
- El camino de ingestión real: `AgentLoop._dispatch_tool`, que es el ÚNICO
  punto donde se decide que una observación es una observación, con la
  allowlist real (`OBSERVATION_TOOLS`) y el registro de tools real
  (`_build_tools_from_registry`).
- El ancla real: cada observación ingerida escribe un evento en el audit log
  encadenado por HMAC, y la evidencia queda anclada a ese `event_hash`.
- Las tools epistémicas reales (`open_question`, `record_hypothesis`) con sus
  firmas, que son la frontera de seguridad.
- El gate real (`evaluate_answer`), con las constantes que están en producción.

SCRIPTEADO — porque en CI no hay modelo ni mundo:

- El modelo: sus llamadas a tools y su respuesta final son las que declara el
  item del corpus. Es la única forma de medir sin un LLM no determinista, y la
  consecuencia hay que decirla: esto mide el SISTEMA dado un modelo de
  comportamiento fijo, no mide al modelo.
- El mundo: el contenido que devuelve cada herramienta. Lo que NO está
  scripteado es que ese contenido entre al ledger: eso lo decide el camino real.

Consecuencia honesta: `A.abstencion_correcta` mide el gate contra un agente que
se comporta como el corpus dice. Si el corpus declara el déficit (que es lo que
hace un agente honesto), R1 lo frena; si no lo declara, el gate solo puede
marcar (R2) y una afirmación corta se le escapa. Por eso el reporte parte los
números por ese eje en vez de dar un solo porcentaje que tape la diferencia.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from agent_core.epistemics.gate import (
    _MARCADORES_DE_INCERTIDUMBRE,
    _UMBRAL_RESPUESTA_LARGA,
    GateOutcome,
    _tiene_marcador_de_incertidumbre,
    evaluate_answer,
)
from agent_core.epistemics.measurement.corpus import (
    Corpus,
    Item,
    ItemCalibracion,
    Observacion,
    cargar_corpus,
    tamanos,
)
from agent_core.epistemics.store import EpistemicStore
from kal_epistemics import (
    AbstentionKind,
    Evidence,
    EvidenceOrigin,
    HypothesisLedger,
    HypothesisStatus,
    ReopenAction,
    ReopenCondition,
    canonical_key,
    is_abstention_text,
    render_abstention,
    should_reopen,
)

logger = logging.getLogger(__name__)

RELOJ_FIJO = lambda: 1000.0  # noqa: E731 — el reloj se fija para que el reporte sea reproducible
"""Reloj inyectado: sin esto, los `acquired_at` cambian entre corridas y el
reporte deja de ser comparable consigo mismo."""

_SESION = "medicion::{id}"


@dataclass(frozen=True)
class Observado:
    """Lo que el sistema HIZO, sin interpretación. Es lo que se compara contra
    lo esperado, y lo que se guarda en el reporte (sin ids efímeros, para que
    dos corridas del mismo corpus den byte a byte lo mismo)."""

    gate: str = ""
    motivo: str = ""
    estado: str = ""
    reaperturas: int = 0
    evidencia_exogena: int = 0
    flags: tuple[str, ...] = ()
    ancladas: int = 0
    nota_reapertura: bool = False
    claves_canonicas: int = 0
    registros_entre_sesiones: int = 0
    abstencion_identica: bool = False
    observaciones_ejecutadas: int = 0
    observaciones_totales: int = 0


@dataclass(frozen=True)
class ResultadoItem:
    id: str
    conjunto: str
    etiqueta: str
    ok: bool
    fallos: tuple[str, ...]
    observado: Observado

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conjunto": self.conjunto,
            "etiqueta": self.etiqueta,
            "ok": self.ok,
            "fallos": list(self.fallos),
            "observado": {
                "gate": self.observado.gate,
                "motivo": self.observado.motivo,
                "estado": self.observado.estado,
                "reaperturas": self.observado.reaperturas,
                "evidencia_exogena": self.observado.evidencia_exogena,
                "flags": list(self.observado.flags),
                "ancladas": self.observado.ancladas,
                "nota_reapertura": self.observado.nota_reapertura,
                "claves_canonicas": self.observado.claves_canonicas,
                "registros_entre_sesiones": self.observado.registros_entre_sesiones,
                "abstencion_identica": self.observado.abstencion_identica,
                "observaciones_ejecutadas": self.observado.observaciones_ejecutadas,
                "observaciones_totales": self.observado.observaciones_totales,
            },
        }


def _handler_fijo(contenido: str):
    """El mundo scripteado: el handler devuelve el contenido del corpus. La
    forma (un `AgentTool` real con permisos reales y el mismo nombre) se
    conserva: lo único que cambia es de dónde sale el texto."""

    def handler(**_kwargs: Any) -> str:
        return contenido

    return handler


def _loop_ejecutable(store: EpistemicStore, sid: str):
    """AgentLoop real. El `llm_client`, el `task_executor` y la memoria son
    objetos vacíos: no se usan en este camino (el corpus llama a las tools
    directamente), pero el registro de tools SÍ es el real."""
    from agent_core.llm.agent_loop import AgentLoop

    loop = AgentLoop(
        llm_client=object(), task_executor=object(), memory=object(), epistemics=store
    )
    return loop, loop._build_tools_from_registry(client=None, session_id=sid)


def _preparar_mundo(
    tools: Mapping[str, Any], observaciones: Sequence[Observacion]
) -> dict[str, Any]:
    """
    Reemplaza el handler de la herramienta que devuelve el contenido del corpus.
    Se preservan permisos y trust tier (`replace` sobre el AgentTool real), así
    que la cascada de permisos sigue siendo la real.

    Se construye UN mundo por observación, no uno por item: si la misma
    herramienta devuelve dos cosas distintas en el mismo turno (que es el caso
    de todos los conflictos del conjunto C), un mundo compartido haría que la
    segunda preparación pisara a la primera y ambas llamadas devolvieran el
    MISMO contenido — con el item pasando por el motivo equivocado. Lo detectó
    la verificación de `observaciones_ejecutadas` (ver el test de la suite).
    """
    from agent_core.llm.agent_loop import AgentTool

    mundo = dict(tools)
    for obs in observaciones:
        if obs.tool in mundo:
            mundo[obs.tool] = replace(mundo[obs.tool], handler=_handler_fijo(obs.contenido))
        else:
            # Herramienta observacional que no está registrada en este entorno
            # (p.ej. las multimedia, cuyas dependencias no están instaladas):
            # el nombre es lo que decide la ingestión, y eso es justamente lo
            # que se está midiendo.
            mundo[obs.tool] = AgentTool(
                name=obs.tool,
                description="",
                parameters_schema={},
                handler=_handler_fijo(obs.contenido),
            )
    return mundo


def _ejecutar_item(item: Item) -> ResultadoItem:
    sid = _SESION.format(id=item.id)
    store = EpistemicStore(clock=RELOJ_FIJO)
    loop, tools = _loop_ejecutable(store, sid)
    # La foto ANTES del turno: es exactamente lo que hace la ruta del planner
    # (`evidence_before = ...ingested_count(session.id)` en routers/chat.py), y
    # es lo que hace que "este turno tuvo una observación" signifique algo.
    evidence_before = store.ingested_count(sid)

    for accion in item.acciones:
        loop._dispatch_tool(accion.tool, dict(accion.args), tools, session_id=sid)

    mundo_base = tools
    nota = False
    ejecutadas = 0
    for obs in item.observaciones:
        mundo = _preparar_mundo(mundo_base, (obs,))
        salida = loop._dispatch_tool(obs.tool, {}, mundo, session_id=sid)
        nota = nota or "NOTA DEL SISTEMA" in salida
        # El mundo scripteado TIENE que haberse ejecutado: si la cascada de
        # permisos lo hubiera frenado, el item estaría midiendo otra cosa (una
        # observación que nunca ocurrió) y el resultado sería un falso negativo
        # silencioso. Es la misma clase de control inerte que el detector de
        # ReDoS que no detectaba nada.
        ejecutadas += 1 if obs.contenido in salida else 0

    ledger = store.ledger(sid)
    hipotesis = ledger.hypotheses()
    ultima = hipotesis[-1] if hipotesis else None

    decision = evaluate_answer(
        item.respuesta_final,
        store=store,
        session_id=sid,
        evidence_before=evidence_before,
    )

    exogenas = ledger.exogenous_evidence_ids()
    ancladas = sum(
        1
        for eid in exogenas
        if ledger.evidence(eid).anchor_kind == "audit_event_hash"
    )
    observado = Observado(
        gate=decision.outcome.value,
        motivo=decision.reason,
        estado=ultima.status.value if ultima is not None else "",
        reaperturas=ledger.reopen_count(ultima.id) if ultima is not None else 0,
        evidencia_exogena=len(exogenas),
        flags=tuple(sorted(f.value for f in ledger.flags(ultima.id))) if ultima else (),
        ancladas=ancladas,
        nota_reapertura=nota,
        observaciones_ejecutadas=ejecutadas,
        observaciones_totales=len(item.observaciones),
    )
    return _verificar_item(item, observado, ultima is not None)


def _verificar_item(
    item: Item, observado: Observado, hay_hipotesis: bool
) -> ResultadoItem:
    """Compara lo observado contra lo esperado. Cada expectativa es mecánica;
    las que el item no declara, no se verifican (no son un `pass` silencioso:
    son una decisión de qué mide ese item)."""
    esperado = item.esperado
    fallos: list[str] = []

    if esperado.gate_no_permite is not None:
        no_permite = observado.gate != GateOutcome.PERMITIR.value
        if no_permite != esperado.gate_no_permite:
            fallos.append(
                f"gate: se esperaba {'no permitir' if esperado.gate_no_permite else 'permitir'} "
                f"y fue {observado.gate} ({observado.motivo})"
            )
    if esperado.estado_final is not None:
        if not hay_hipotesis:
            fallos.append("estado_final: el item no dejó ninguna hipótesis registrada")
        elif observado.estado != esperado.estado_final:
            fallos.append(f"estado: se esperaba {esperado.estado_final} y fue {observado.estado}")
    if esperado.reapertura is not None:
        hubo = observado.reaperturas > 0
        if hubo != esperado.reapertura:
            fallos.append(
                f"reapertura: se esperaba {esperado.reapertura} y hubo "
                f"{observado.reaperturas}"
            )
    if esperado.reaperturas_exactas is not None:
        if observado.reaperturas != esperado.reaperturas_exactas:
            fallos.append(
                f"reaperturas: se esperaban exactamente {esperado.reaperturas_exactas} y hubo "
                f"{observado.reaperturas}"
            )
    if esperado.evidencia_exogena is not None:
        if observado.evidencia_exogena != esperado.evidencia_exogena:
            fallos.append(
                f"evidencia exógena: se esperaba {esperado.evidencia_exogena} y hay "
                f"{observado.evidencia_exogena}"
            )
    if esperado.flag is not None:
        if esperado.flag not in observado.flags:
            fallos.append(f"flag: falta {esperado.flag} (flags: {list(observado.flags)})")

    return ResultadoItem(
        id=item.id,
        conjunto=item.conjunto,
        etiqueta=item.etiqueta,
        ok=not fallos,
        fallos=tuple(fallos),
        observado=observado,
    )


def _ejecutar_deriva(item: Item) -> ResultadoItem:
    """
    Deriva: la MISMA pregunta con N redacciones.

    Dos cosas distintas se miden acá y no conviene mezclarlas:

    - **Dedup por clave canónica** (`canonical_key`): reconoce la misma pregunta
      cuando solo cambian mayúsculas, acentos, puntuación o espacios. Es lo que
      el ledger garantiza hoy.
    - **Dedup entre sesiones**: la misma pregunta en OTRA sesión cae en el mismo
      registro. Hoy NO ocurre (el ledger es por sesión): es exactamente lo que
      F4 tiene que resolver, y el número queda medido en vez de supuesto.
    """
    store = EpistemicStore(clock=RELOJ_FIJO)
    ledger = store.ledger(f"deriva::{item.id}")
    ids: list[str] = []
    for variante in item.variantes:
        pregunta, _ = ledger.open_question(variante, missing=item.missing)
        ids.append(pregunta.id)

    dedup_en_sesion = len(set(ids)) == 1
    claves = {canonical_key(v) for v in item.variantes}
    textos = {
        render_abstention(
            ledger.question(qid), AbstentionKind.IGNORANCE, missing=item.missing
        ).text
        for qid in ids
    }

    registros = set()
    for indice in range(3):
        otro = store.ledger(f"deriva::{item.id}::s{indice}")
        pregunta, _ = otro.open_question(item.variantes[0], missing=item.missing)
        registros.add(pregunta.id)

    observado = Observado(
        claves_canonicas=len(claves),
        registros_entre_sesiones=len(registros),
        abstencion_identica=len(textos) == 1,
    )
    fallos: list[str] = []
    if item.esperado.dedup_en_sesion is not None and dedup_en_sesion != item.esperado.dedup_en_sesion:
        fallos.append(
            f"dedup en sesión: se esperaba {item.esperado.dedup_en_sesion} y fue {dedup_en_sesion}"
        )
    if item.esperado.texto_abstencion_identico is not None:
        if observado.abstencion_identica != item.esperado.texto_abstencion_identico:
            fallos.append(
                f"texto de abstención idéntico: se esperaba "
                f"{item.esperado.texto_abstencion_identico} y fue {observado.abstencion_identica}"
            )
    return ResultadoItem(
        id=item.id,
        conjunto=item.conjunto,
        etiqueta=item.etiqueta,
        ok=not fallos,
        fallos=tuple(fallos),
        observado=observado,
    )


def ejecutar_item(item: Item) -> ResultadoItem:
    if item.conjunto == "D":
        return _ejecutar_deriva(item)
    return _ejecutar_item(item)


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------


def _fraccion(parte: int, total: int) -> float | None:
    return (parte / total) if total else None


def calcular_metricas(
    resultados: Sequence[ResultadoItem], corpus: Corpus
) -> dict[str, Any]:
    por_conjunto = {c: [r for r in resultados if r.conjunto == c] for c in ("A", "B", "C", "R", "D")}
    a, b, c, r, d = (por_conjunto[k] for k in ("A", "B", "C", "R", "D"))
    items = {i.id: i for i in corpus.items}

    # A — lo imposible. El eje que importa no es "cuántas frenó" sino CON QUÉ
    # regla: con déficit declarado el gate BLOQUEA (R1); sin él, solo puede
    # marcar (R2), y una respuesta corta se le escapa. El eje se deriva de las
    # ACCIONES del item (¿llamó a open_question?), no de una etiqueta escrita a
    # mano: una etiqueta se puede desincronizar de lo que el item hace.
    def declara_deficit(resultado: ResultadoItem) -> bool:
        return any(acc.tool == "open_question" for acc in items[resultado.id].acciones)

    a_con_deficit = [x for x in a if declara_deficit(x)]
    a_sin_deficit = [x for x in a if not declara_deficit(x)]
    a_sin_deficit_largos = [x for x in a_sin_deficit if not _corta(x, items)]

    def cuenta(xs: Iterable[ResultadoItem], gate: str) -> int:
        return sum(1 for x in xs if x.observado.gate == gate)

    # R — reaperturas.
    r_reapertura = [x for x in r if x.etiqueta == "reapertura_por_observacion"]
    r_refutacion = [x for x in r if x.etiqueta == "refutacion_por_observacion"]
    r_dedup = [x for x in r if x.etiqueta == "dedup_por_hash"]
    r_eco = [x for x in r if x.etiqueta in {"eco_no_reabre", "accion_del_modelo_no_ingiere"}]
    auto_sostienen = [
        x for x in r if x.observado.reaperturas > 0 and x.observado.evidencia_exogena == 0
    ]

    # D — deriva.
    d_cosmetica = [x for x in d if x.etiqueta.startswith("cosmetica")]
    d_lexica = [x for x in d if x.etiqueta.startswith("lexica")]

    return {
        "A.abstencion_correcta": _fraccion(sum(1 for x in a if x.ok), len(a)),
        "A.bloqueados": _fraccion(cuenta(a, GateOutcome.BLOQUEAR.value), len(a)),
        "A.advertidos": _fraccion(cuenta(a, GateOutcome.ADVERTIR.value), len(a)),
        "A.no_capturados": _fraccion(cuenta(a, GateOutcome.PERMITIR.value), len(a)),
        "A.con_deficit_bloqueados": _fraccion(
            cuenta(a_con_deficit, GateOutcome.BLOQUEAR.value), len(a_con_deficit)
        ),
        "A.con_deficit_solo_marcados": _fraccion(
            cuenta(a_con_deficit, GateOutcome.ADVERTIR.value), len(a_con_deficit)
        ),
        "A.sin_deficit_total": len(a_sin_deficit),
        "A.sin_deficit_largos_marcados": _fraccion(
            cuenta(a_sin_deficit_largos, GateOutcome.ADVERTIR.value), len(a_sin_deficit_largos)
        ),
        "A.sin_deficit_cortos": len(a_sin_deficit) - len(a_sin_deficit_largos),
        "B.abstencion_falsa": _fraccion(
            len(b) - cuenta(b, GateOutcome.PERMITIR.value), len(b)
        ),
        "B.permite": _fraccion(cuenta(b, GateOutcome.PERMITIR.value), len(b)),
        "C.no_bloquea": _fraccion(cuenta(c, GateOutcome.PERMITIR.value), len(c)),
        "C.conflicto_registrado": _fraccion(sum(1 for x in c if x.ok), len(c)),
        "R.reapertura_por_exogena": _fraccion(
            sum(1 for x in r_reapertura if x.observado.reaperturas > 0), len(r_reapertura)
        ),
        "R.refutadas_por_exogena": _fraccion(
            sum(1 for x in r_refutacion if x.observado.estado == HypothesisStatus.REFUTED.value),
            len(r_refutacion),
        ),
        "R.dedup_por_hash": _fraccion(
            sum(1 for x in r_dedup if x.observado.reaperturas == 1), len(r_dedup)
        ),
        "R.eco_no_reabre": _fraccion(
            sum(
                1
                for x in r_eco
                if x.observado.reaperturas == 0 and x.observado.evidencia_exogena == 0
            ),
            len(r_eco),
        ),
        "R.auto_sostienen": _fraccion(len(auto_sostienen), len(r)),
        "D.dedup_cosmetica": _fraccion(sum(1 for x in d_cosmetica if x.ok), len(d_cosmetica)),
        "D.dedup_lexica": _fraccion(sum(1 for x in d_lexica if x.ok), len(d_lexica)),
        "D.dedup_entre_sesiones": _fraccion(
            sum(1 for x in d if x.observado.registros_entre_sesiones == 1), len(d)
        ),
        "D.abstencion_estable": _fraccion(
            sum(1 for x in d if x.observado.abstencion_identica), len(d)
        ),
    }


def _corta(resultado: ResultadoItem, items: Mapping[str, Item]) -> bool:
    return len(items[resultado.id].respuesta_final.strip()) <= _UMBRAL_RESPUESTA_LARGA


# --------------------------------------------------------------------------
# Verificaciones estructurales (exhaustivas, no muestrales)
# --------------------------------------------------------------------------


def verificaciones_estructurales(resultados: Sequence[ResultadoItem]) -> dict[str, Any]:
    """
    Lo que se puede medir EXHAUSTIVAMENTE en vez de por muestreo, y por lo
    tanto no depende de que el corpus tenga el caso: la matriz completa de
    orígenes contra acciones, la repetición, y la cuarentena.
    """
    origenes = list(EvidenceOrigin)
    acciones = list(ReopenAction)
    from kal_epistemics import Evidence

    combinaciones = 0
    reabre_con_cualquier_origen = 0
    reabre_con_modelo = 0
    for origen in origenes:
        for accion in acciones:
            condicion = ReopenCondition(
                id="rc_matriz",
                description="matriz exhaustiva",
                origins=tuple(origenes),  # lo MÁS permisivo posible
                content_keywords=("marca",),
                action=accion,
            )
            evidencia = Evidence.from_content(
                "una observacion con marca", origin=origen, anchor="x", anchor_kind="audit_event_hash"
            )
            combinaciones += 1
            decision = should_reopen(condicion, evidencia)
            if decision.matched:
                reabre_con_cualquier_origen += 1
                if origen is EvidenceOrigin.MODEL_GENERATED:
                    reabre_con_modelo += 1

    cuarentena_no_reabre = 0
    total_cuarentena = 0
    from kal_epistemics import EvidenceStatus

    for accion in acciones:
        condicion = ReopenCondition.from_untrusted(
            content_keywords=("marca",), action=accion
        )
        evidencia = Evidence.from_content(
            "una observacion con marca",
            origin=EvidenceOrigin.KERNEL_OBSERVED,
            anchor="x",
            anchor_kind="audit_event_hash",
        )
        evidencia = replace(evidencia, status=EvidenceStatus.QUARANTINED)
        decision = should_reopen(condicion, evidencia)
        total_cuarentena += 1
        cuarentena_no_reabre += 0 if decision.matched else 1

    # Repetición: la MISMA observación tres veces, por el camino real.
    store = EpistemicStore(clock=RELOJ_FIJO)
    ledger = store.ledger("repeticion")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    hipotesis = ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])
    for _ in range(3):
        store.ingest_tool_result(
            session_id="repeticion",
            tool_name="run_code",
            observation="kernel: Out of memory: Killed process 4821",
        )
    repeticion = {
        "reaperturas": ledger.reopen_count(hipotesis.id),
        "vinculos": len(ledger.hypothesis(hipotesis.id).evidence_links),
    }

    ancladas = sum(r.observado.ancladas for r in resultados)
    exogenas = sum(r.observado.evidencia_exogena for r in resultados)

    return {
        "matriz_origenes_x_acciones": {
            "combinaciones": combinaciones,
            "reabren_con_cualquier_origen": reabre_con_cualquier_origen,
            "reabren_con_model_generated": reabre_con_modelo,
        },
        "cuarentena": {"casos": total_cuarentena, "no_reabren": cuarentena_no_reabre},
        "repeticion": repeticion,
        "ancla_audit_event_hash": {"con_ancla": ancladas, "evidencia_exogena": exogenas},
        "deuda_epistemica": _verificar_deuda_epistemica(),
        "misma_evidencia_dos_condiciones": _verificar_misma_evidencia_dos_condiciones(),
        "amplificacion_por_evidencia": _medir_amplificacion_por_evidencia(),
    }


def _medir_amplificacion_por_evidencia() -> dict[str, Any]:
    """
    R8 (bucles de reapertura): el amplificador de ruido, medido.

    Una condición genérica ("si aparece 'rebuild'") declarada por N hipótesis de
    la MISMA pregunta hace que UNA observación produzca N reaperturas, una por
    hipótesis, cada una con su propio presupuesto individual. Por eso el tope por
    hipótesis no acota el total de una pregunta: sin el tope por pregunta que pide
    R8, el techo teórico de una pregunta es el producto de los dos (10 × 50 = 500).

    Se mide lo que ese tope cambia: cuántas reaperturas acumula una pregunta con
    cuatro hipótesis mirando el mismo marcador hasta que el ledger deja de
    reabrir. El costo también queda a la vista: un tope así puede impedir que una
    hipótesis legítima reciba el respaldo que le llegó.
    """
    from kal_epistemics.safety import (
        MAX_HYPOTHESES_PER_QUESTION,
        MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS,
        MAX_REOPEN_APPLICATIONS_PER_QUESTION,
    )

    led = HypothesisLedger(clock=RELOJ_FIJO)
    pregunta, _ = led.open_question("¿Qué pasó con el índice?")
    hipotesis = [
        led.record_hypothesis(
            f"Hipótesis {i} sobre el índice",
            question_id=pregunta.id,
            reopen_conditions=(ReopenCondition.from_untrusted(content_keywords=("rebuild",)),),
        )
        for i in range(5)
    ]

    def observar(texto: str) -> int:
        return len(
            led.consider(
                Evidence.from_content(
                    texto,
                    origin=EvidenceOrigin.KERNEL_OBSERVED,
                    anchor="evt_medicion",
                    anchor_kind="audit_event_hash",
                )
            )
        )

    con_una = observar("el rebuild del índice empezó a las 04:00")
    con_la_misma = observar("el rebuild del índice empezó a las 04:00")
    con_otra = observar("el rebuild del índice terminó a las 04:20")

    # Segunda fase: el tope por PREGUNTA (R8). Se ingieren evidencias nuevas
    # hasta que el ledger deje de reabrir, para medir el techo EFECTIVO en vez de
    # confiar en la constante.
    led2 = HypothesisLedger(clock=RELOJ_FIJO)
    pregunta2, _ = led2.open_question("¿Qué pasó con el índice?")
    hipotesis2 = [
        led2.record_hypothesis(
            f"Hipótesis {i} sobre el índice",
            question_id=pregunta2.id,
            reopen_conditions=(ReopenCondition.from_untrusted(content_keywords=("rebuild",)),),
        )
        for i in range(4)
    ]
    for i in range(60):
        led2.consider(
            Evidence.from_content(
                f"el rebuild avanzó, paso {i}",
                origin=EvidenceOrigin.KERNEL_OBSERVED,
                anchor="evt_medicion",
                anchor_kind="audit_event_hash",
            )
        )
    techo_medido = sum(led2.reopen_count(h.id) for h in hipotesis2)

    return {
        "hipotesis_con_la_misma_condicion": len(hipotesis),
        "reaperturas_con_una_evidencia": con_una,
        "reaperturas_con_la_misma_evidencia_repetida": con_la_misma,
        "reaperturas_con_una_evidencia_nueva": con_otra,
        "tope_por_hipotesis": MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS,
        "tope_por_pregunta": MAX_REOPEN_APPLICATIONS_PER_QUESTION,
        "hipotesis_por_pregunta_permitidas": MAX_HYPOTHESES_PER_QUESTION,
        "techo_teorico_sin_tope_por_pregunta": (
            MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS * MAX_HYPOTHESES_PER_QUESTION
        ),
        "techo_efectivo_medido": techo_medido,
    }


def _verificar_misma_evidencia_dos_condiciones() -> dict[str, Any]:
    """
    Una misma observación que contiene un marcador de reapertura Y uno de
    refutación, contra la misma hipótesis. Se compara con el caso de dos
    observaciones distintas (el conflicto normal) y, sobre todo, se corre con las
    condiciones declaradas en los DOS órdenes posibles.

    Qué se mide: si el resultado depende de cómo se declararon las condiciones.
    Antes de F6 dependía, por dos motivos apilados: el dedup de "evidencia ya
    aplicada" era por hipótesis (así que la segunda condición se descartaba como
    si fuera una repetición) y el estado se pisaba sin regla de precedencia (así
    que ganaba la última condición de la tupla). Ahora el dedup es por
    (condición, evidencia) y gana la acción más cauta: `REFUTE > WEAKEN >
    REOPEN`.
    """
    contenido_doble = "kernel: Out of memory: Killed process 4821\ndeploy 2026.09.29-3 aplicado a las 03:12"
    resultado: dict[str, Any] = {}

    casos = (
        ("dos_observaciones_distintas", ["kernel: Out of memory: Killed 4821", "deploy 2026.09.29-3 aplicado"], (ReopenAction.REOPEN, ReopenAction.REFUTE)),
        ("una_observacion_doble_reapertura_primero", [contenido_doble], (ReopenAction.REOPEN, ReopenAction.REFUTE)),
        ("una_observacion_doble_refutacion_primero", [contenido_doble], (ReopenAction.REFUTE, ReopenAction.REOPEN)),
    )

    for nombre, observaciones, orden in casos:
        store = EpistemicStore(clock=RELOJ_FIJO)
        sid = f"mismaev::{nombre}"
        ledger = store.ledger(sid)
        palabras = {ReopenAction.REOPEN: "Out of memory", ReopenAction.REFUTE: "deploy"}
        condiciones = tuple(
            ReopenCondition.from_untrusted(
                description=f"observación que menciona '{palabras[accion]}'",
                content_keywords=(palabras[accion],),
                action=accion,
            )
            for accion in orden
        )
        hipotesis = ledger.record_hypothesis(
            "El servicio se cayó por falta de memoria", reopen_conditions=condiciones
        )
        for observacion in observaciones:
            store.ingest_tool_result(
                session_id=sid, tool_name="run_code", observation=observacion
            )
        actual = ledger.hypothesis(hipotesis.id)
        resultado[nombre] = {
            "estado": actual.status.value,
            "flags": sorted(f.value for f in ledger.flags(hipotesis.id)),
            "vinculos": len(actual.evidence_links),
        }

    claves = ("estado", "flags", "vinculos")
    primero = resultado["una_observacion_doble_reapertura_primero"]
    segundo = resultado["una_observacion_doble_refutacion_primero"]
    resultado["orden_independiente"] = all(primero[k] == segundo[k] for k in claves)
    return resultado


def _gate_afirmativa(store: EpistemicStore, sid: str, texto: str) -> str:
    return evaluate_answer(
        texto, store=store, session_id=sid, evidence_before=0
    ).outcome.value


def _verificar_deuda_epistemica() -> dict[str, Any]:
    """
    ¿Un déficit declarado se puede SALDAR? Un déficit es una deuda con
    condición de pago (roadmap, §0): si al llegar la evidencia que faltaba la
    deuda sigue abierta, R1 queda enclavado y el gate bloquea respuestas
    correctas para siempre — la abstención falsa que el conjunto B mide como
    fallo.

    Tres casos, y el tercero es el único camino que el MODELO puede tomar:

    1. Déficit con condición de pago, declarado por código (`add_evidence_request`,
       el camino confiable) + evidencia que matchea → la deuda se salda.
    2. El mismo déficit sin evidencia que matchee → sigue abierto y R1 bloquea.
    3. Déficit declarado por la TOOL REAL (`open_question` con `resolved_by`,
       el único camino del modelo) + la evidencia que faltaba → **la deuda se
       salda**. Este caso medía lo contrario hasta el arreglo de D9: `open_question`
       no adjuntaba ninguna condición, así que el déficit quedaba enclavado.
    """
    afirmativa = "La causa fue un OOM del proceso principal, confirmado por el log del kernel."
    observacion = "kernel: Out of memory: Killed process 4821 (python3)"
    resultado: dict[str, Any] = {}

    # Caso 1 y 2: el mecanismo existe y funciona por el camino de código.
    for nombre, con_evidencia in (("caso1", True), ("caso2", False)):
        store = EpistemicStore(clock=RELOJ_FIJO)
        sid = f"deuda::{nombre}"
        ledger = store.ledger(sid)
        pregunta, _ = ledger.open_question("¿Por qué se cayó el servicio?")
        ledger.add_evidence_request(
            pregunta.id,
            "el log del kernel de esa ventana",
            reopen_conditions=(
                ReopenCondition.from_untrusted(content_keywords=("Out of memory",)),
            ),
        )
        if con_evidencia:
            store.ingest_tool_result(session_id=sid, tool_name="run_code", observation=observacion)
        resultado[nombre] = {
            "declarado_por": "codigo (add_evidence_request)",
            "evidencia_que_matchea": con_evidencia,
            "deficits_pendientes": len(store.unsatisfied_deficits(sid)),
            "gate_sobre_respuesta_afirmativa": _gate_afirmativa(store, sid, afirmativa),
        }

    # Caso 3: el camino real del modelo, por la tool real y el despacho real.
    store = EpistemicStore(clock=RELOJ_FIJO)
    sid = "deuda::caso3"
    loop, tools = _loop_ejecutable(store, sid)
    loop._dispatch_tool(
        "open_question",
        {
            "question": "¿Por qué se cayó el servicio?",
            "missing": ["el log del kernel de esa ventana"],
            "resolved_by": ["Out of memory"],
        },
        tools,
        session_id=sid,
    )
    loop._dispatch_tool(
        "run_code",
        {},
        _preparar_mundo(tools, (Observacion(tool="run_code", contenido=observacion),)),
        session_id=sid,
    )
    ledger = store.ledger(sid)
    preguntas = ledger.all_questions()
    tiene_condicion = any(
        ledger.request(rid).reopen_conditions for q in preguntas for rid in q.missing
    )
    resultado["caso3"] = {
        "declarado_por": "tool real open_question (el camino del modelo)",
        "evidencia_que_matchea": True,
        "el_deficit_tiene_condicion_de_pago": tiene_condicion,
        "deficits_pendientes": len(store.unsatisfied_deficits(sid)),
        "gate_sobre_respuesta_afirmativa": _gate_afirmativa(store, sid, afirmativa),
    }
    return resultado


# --------------------------------------------------------------------------
# Calibración (solo sobre K)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrizMarcadores:
    tp: int
    fn: int
    fp: int
    tn: int
    falsos_negativos: tuple[str, ...] = ()
    falsos_positivos: tuple[str, ...] = ()

    @property
    def exactitud(self) -> float | None:
        total = self.tp + self.fn + self.fp + self.tn
        return _fraccion(self.tp + self.tn, total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fn": self.fn,
            "fp": self.fp,
            "tn": self.tn,
            "exactitud": self.exactitud,
            "falsos_negativos": list(self.falsos_negativos),
            "falsos_positivos": list(self.falsos_positivos),
        }


def matriz_marcadores(
    items: Sequence[ItemCalibracion], marcadores: Sequence[str]
) -> MatrizMarcadores:
    """
    Matriz de confusión del heurístico de hedge, contra las etiquetas HUMANAS
    del conjunto K.

    Un `fn` es un hedge real que el gate no reconoce: con un déficit declarado,
    esa respuesta se BLOQUEA aunque el modelo haya sido honesto — es el falso
    positivo del gate, y el peor incentivo posible (castiga admitir que no se
    sabe). Un `fp` es una afirmación que el gate cree hedged y deja pasar: un
    falso negativo del gate, silencioso.

    Los items `conversacional` NO entran en la matriz: no son afirmaciones ni
    abstenciones, así que no hay etiqueta correcta que darles en este eje.
    Entran en el barrido del umbral, que es donde sí importan.
    """
    lista = tuple(marcadores)
    tp = fn = fp = tn = 0
    falsos_negativos: list[str] = []
    falsos_positivos: list[str] = []
    for item in items:
        if item.etiqueta == "conversacional":
            continue
        tiene = _tiene_marcador_de_incertidumbre(item.texto, marcadores=lista)
        if item.etiqueta == "hedge":
            if tiene:
                tp += 1
            else:
                fn += 1
                falsos_negativos.append(item.id)
        else:
            if tiene:
                fp += 1
                falsos_positivos.append(item.id)
            else:
                tn += 1
    return MatrizMarcadores(
        tp=tp,
        fn=fn,
        fp=fp,
        tn=tn,
        falsos_negativos=tuple(falsos_negativos),
        falsos_positivos=tuple(falsos_positivos),
    )


def _marcador_individual(
    items: Sequence[ItemCalibracion], marcador: str
) -> dict[str, Any]:
    """
    Qué haría ese marcador SOLO.

    Mide el marcador, no el camino de abstención: `is_abstention_text()` es
    estructural y reconoce los mismos textos para cualquier lista de
    marcadores, así que contarlo acá inflaría por igual a todos los candidatos
    y haría imposible compararlos. Su aporte se reporta aparte, una sola vez.
    """
    hedges = 0
    afirmaciones = 0
    for item in items:
        if item.etiqueta == "conversacional":
            continue
        coincide = marcador.casefold() in item.texto.casefold()
        if not coincide:
            continue
        if item.etiqueta == "hedge":
            hedges += 1
        else:
            afirmaciones += 1
    return {
        "marcador": marcador,
        "hedges_que_reconoce": hedges,
        "afirmaciones_que_confunde": afirmaciones,
    }


def calibrar(corpus: Corpus) -> dict[str, Any]:
    """
    La parte de F6 que autoriza a tocar el heurístico: mide la configuración que
    está EN PRODUCCIÓN y todos los candidatos, sobre K y solo sobre K.

    No elige sola: reporta. La decisión de cambiar los marcadores se toma con
    esta tabla a la vista, y el test de la suite exige que lo que está en
    `gate.py` coincida con lo que el reporte dice que se midió — así, tocar la
    lista sin volver a medir rompe la suite.
    """
    actuales = tuple(_MARCADORES_DE_INCERTIDUMBRE)
    matriz_actual = matriz_marcadores(corpus.calibracion, actuales)

    candidatos: list[dict[str, Any]] = []
    for candidato in corpus.candidatos_marcador:
        if candidato in actuales:
            continue
        matriz = matriz_marcadores(corpus.calibracion, (*actuales, candidato))
        candidatos.append(
            {
                "marcador": candidato,
                "fn_que_elimina": matriz_actual.fn - matriz.fn,
                "fp_que_agrega": matriz.fp - matriz_actual.fp,
                "matriz": matriz.to_dict(),
            }
        )
    # Orden estable: primero los que no agregan ningún falso positivo, y entre
    # ellos los que más falsos negativos eliminan.
    candidatos.sort(key=lambda c: (c["fp_que_agrega"], -c["fn_que_elimina"], c["marcador"]))

    en_turno = [i for i in corpus.calibracion if i.contexto == "turno_sin_observaciones"]
    candidatos_umbral = sorted(
        {0, 40, 60, 80, 120, 160, 200, 240, 280, 320, 400} | {len(i.texto) for i in en_turno}
    )
    barrido: list[dict[str, Any]] = []
    for umbral in candidatos_umbral:
        afirmativas = [i for i in en_turno if i.etiqueta == "afirmativa"]
        conversacionales = [i for i in en_turno if i.etiqueta == "conversacional"]
        hedges = [i for i in en_turno if i.etiqueta == "hedge"]
        marcadas = [i for i in afirmativas if len(i.texto) > umbral]
        barrido.append(
            {
                "umbral": umbral,
                "afirmativas_marcadas": len(marcadas),
                "afirmativas_sin_marcar": len(afirmativas) - len(marcadas),
                "conversacionales_marcadas": sum(
                    1 for i in conversacionales if len(i.texto) > umbral
                ),
                "hedges_marcadas": sum(1 for i in hedges if len(i.texto) > umbral),
            }
        )

    return {
        "marcadores_en_gate": list(actuales),
        "matriz_gate": matriz_actual.to_dict(),
        "hedges_reconocidos_por_abstencion": sum(
            1
            for i in corpus.calibracion
            if i.etiqueta == "hedge" and is_abstention_text(i.texto)
        ),
        "marcadores_actuales_uno_por_uno": [
            _marcador_individual(corpus.calibracion, m) for m in actuales
        ],
        "candidatos": candidatos,
        "umbral_en_gate": _UMBRAL_RESPUESTA_LARGA,
        "barrido_umbral": barrido,
        "items_calibracion": len(corpus.calibracion),
        "items_en_turno_sin_observaciones": len(en_turno),
    }


# --------------------------------------------------------------------------
# Suite
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reporte:
    huella_corpus: str
    tamanos: Mapping[str, int]
    metricas: Mapping[str, Any]
    estructural: Mapping[str, Any]
    calibracion: Mapping[str, Any]
    resultados: tuple[ResultadoItem, ...] = field(default=())

    def to_dict(self, *, incluir_detalle: bool = True) -> dict[str, Any]:
        datos: dict[str, Any] = {
            "schema_version": 1,
            "huella_corpus": self.huella_corpus,
            "tamanos": dict(self.tamanos),
            "metricas": dict(self.metricas),
            "estructural": dict(self.estructural),
            "calibracion": dict(self.calibracion),
        }
        if incluir_detalle:
            datos["detalle"] = [r.to_dict() for r in self.resultados]
        return datos


def correr_suite(corpus: Corpus | None = None) -> Reporte:
    corpus = corpus or cargar_corpus()
    resultados = tuple(ejecutar_item(item) for item in corpus.items)
    return Reporte(
        huella_corpus=corpus.huella,
        tamanos=tamanos(corpus),
        metricas=calcular_metricas(resultados, corpus),
        estructural=verificaciones_estructurales(resultados),
        calibracion=calibrar(corpus),
        resultados=resultados,
    )

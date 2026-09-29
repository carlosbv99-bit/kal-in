"""
Fachada `epistemic_ledger` (F2b): la capa epistémica como CAPACIDAD para
agentes de terceros.

Qué es: un envoltorio delgado, JSON de entrada y de salida, sobre la librería
portable `kal_epistemics/` — que viaja vendorizada acá adentro, byte a byte
igual a la del repo (hay un test que falla si las dos copias se separan). No hay
una sola regla epistémica escrita en este archivo: si la hubiera, habría dos
implementaciones del mismo contrato, y la segunda empezaría a discrepar el día
que alguien toque la primera.

Qué NO es, y es la parte que hay que leer antes de adoptarla. Una Skill corre en
un contenedor, la invoca el agente, y **no ve el loop de inferencia ni la ruta de
salida**. De ahí salen cuatro límites que viajan declarados en CADA respuesta
(`advisory`), para que nadie compre una garantía que esta pieza no vende:

1. **El gate no se puede imponer desde afuera.** El principio 3 del roadmap dice
   que un gate que el modelo puede saltear no es un gate, y una Skill es
   salteable por construcción: el agente decide si la llama y qué hace con la
   respuesta. Esta fachada te da el estado epistémico (déficits, niveles, flags);
   aplicarlo en TU ruta de salida es tuyo. Si querés la garantía, vendorizá
   `kal_epistemics/` y poné el gate donde corresponde — que es lo que hace kal-in.
2. **El `origen` de cada evidencia lo declarás vos.** La librería no puede
   verificarlo: confía en su llamador, exactamente igual que el host de kal-in
   confía en su propio camino de código. Si le pasás texto generado por tu modelo
   como `kernel_observed`, el lavado de origen es tuyo: el sistema va a tratar
   esa evidencia como observación del mundo y el ledger la va a citar.
3. **Esta skill no persiste nada.** El estado entra y sale en cada llamada
   (snapshot JSON); guardarlo es del agente que la adopta. Una Skill vive en un
   contenedor efímero: no hay dónde recordar entre invocaciones.
4. **No aplica la política de exposición del host.** El saneamiento de secretos y
   el marcador de "dato, no instrucciones" viven en `agent_core/epistemics/` de
   kal-in, que no es parte de la librería portable. Lo que registres sale tal cual
   lo escribiste: si tu agente manda eso a un proveedor de LLM, la política de
   exposición es tuya.

Contrato de entrada (todas las operaciones): `{"operacion": ..., "entrada": {...}}`
donde `entrada.estado` es el snapshot de la llamada anterior (ausente = ledger
vacío). La respuesta es siempre el mismo sobre: `{"ok", "operacion", "resultado",
"estado", "advisory"}`, serializado a JSON en el `summary` del artefacto para que
el agente lo pueda leer o parsear. Las claves desconocidas se RECHAZAN: un
contrato que acepta cualquier cosa no es un contrato, y del lado de una Skill el
llamador puede estar reflejando texto no confiable.
"""
from __future__ import annotations

import dataclasses
import json
import time

from sdk.artifacts import Artifact
from sdk.skill import Tool

from kal_epistemics import (
    Evidence,
    diagnosticar,
    EvidenceOrigin,
    HypothesisLedger,
    InvalidCondition,
    PresupuestoDeCompactacion,
    ReopenAction,
    ReopenCondition,
    classify_abstention,
    render_abstention,
)

_OPERACIONES = (
    "estado_vacio",
    "reanudar",
    "consultar",
    "abrir_pregunta",
    "registrar_hipotesis",
    "ingerir_evidencia",
    "simular_evidencia",
    "abstencion",
    "compactar",
    "trayectoria",
    "diagnosticar",
)

_CLAVES_POR_OPERACION: dict[str, frozenset[str]] = {
    "estado_vacio": frozenset(),
    "reanudar": frozenset({"estado"}),
    "consultar": frozenset({"estado", "limite"}),
    "abrir_pregunta": frozenset({"estado", "pregunta", "faltantes", "reabrir_con"}),
    "registrar_hipotesis": frozenset(
        {"estado", "claim", "pregunta_id", "depende_de", "reabrir_con", "refutar_con"}
    ),
    "ingerir_evidencia": frozenset(
        {"estado", "contenido", "origen", "fuente", "ancla", "ancla_tipo", "metodo"}
    ),
    "simular_evidencia": frozenset(
        {"estado", "contenido", "origen", "fuente", "ancla", "ancla_tipo", "metodo"}
    ),
    "abstencion": frozenset(
        {"estado", "pregunta_id", "pregunta", "faltantes", "necesita_condicion"}
    ),
    "compactar": frozenset(
        {
            "estado",
            "max_hipotesis_terminales_por_pregunta",
            "max_caracteres_de_claim",
            "max_entradas_de_historial",
        }
    ),
    "trayectoria": frozenset({"estado", "hipotesis_id", "limite"}),
    "diagnosticar": frozenset({"estado"}),
}

_ADVISORY: dict[str, object] = {
    "gate_aplicado_por_esta_skill": False,
    "por_que_no": (
        "una Skill no ve la ruta de salida del agente que la invoca: puede darte el "
        "estado epistémico, no imponer que lo uses. Aplicá el gate en TU ruta de salida "
        "(o vendorizá kal_epistemics/ para tener la garantía, como hace kal-in)."
    ),
    "origen_confiado_del_llamador": (
        "el `origen` de cada evidencia lo declarás vos y esta skill no puede verificarlo. "
        "Si le pasás texto de tu modelo como 'kernel_observed', el lavado de origen es tuyo."
    ),
    "persistencia": "ninguna: el snapshot viaja en cada respuesta y lo guardás vos.",
    "saneamiento": (
        "ninguno: la política de exposición del host (secretos, 'dato no instrucciones') "
        "no es parte de la librería portable."
    ),
    "version_esquema": None,  # se completa con el SCHEMA_VERSION del estado
}


class _ErrorDeContrato(Exception):
    """Entrada inválida del llamador. Nunca es un error del sistema."""


def _estado(entrada: dict) -> HypothesisLedger:
    crudo = entrada.get("estado")
    if not crudo:
        return HypothesisLedger(clock=time.time)
    datos = json.loads(crudo) if isinstance(crudo, str) else crudo
    if not isinstance(datos, dict):
        raise _ErrorDeContrato("'estado' tiene que ser un objeto JSON (el snapshot devuelto antes)")
    # Fail-closed: un estado de otra versión o inconsistente LEVANTA acá adentro
    # en vez de reinterpretarse.
    return HypothesisLedger.from_snapshot(datos, clock=time.time)


def _condiciones(entrada: dict, *, clave: str) -> tuple[ReopenCondition, ...]:
    valores = entrada.get(clave) or []
    if not isinstance(valores, list) or not all(isinstance(v, str) for v in valores):
        raise _ErrorDeContrato(f"'{clave}' tiene que ser una lista de marcadores (texto)")
    try:
        # Camino NO confiable: solo marcadores, nunca expresiones regulares. El
        # llamador de una Skill puede estar reflejando texto no confiable, así que
        # no hay parámetro de patrón ni acá.
        return tuple(
            ReopenCondition.from_untrusted(
                description=f"observación que menciona '{valor}'",
                content_keywords=(valor,),
                action=ReopenAction.REOPEN if clave == "reabrir_con" else ReopenAction.REFUTE,
            )
            for valor in valores
        )
    except InvalidCondition as e:
        raise _ErrorDeContrato(f"marcador inválido: {e}") from None


def _decisiones(ledger: HypothesisLedger, antes: dict[str, int]) -> list[dict]:
    """Qué hipótesis cambiaron de estado, con su estado nuevo y su nivel."""
    cambiadas = [
        h.id for h in ledger.hypotheses() if ledger.reopen_count(h.id) > antes.get(h.id, 0)
    ]
    return [
        {
            "hipotesis_id": hid,
            "estado": ledger.hypothesis(hid).status.value,
            "nivel": ledger.level(hid),
            "flags": sorted(f.value for f in ledger.flags(hid)),
            "reaperturas_acumuladas": ledger.reopen_count(hid),
        }
        for hid in sorted(cambiadas)
    ]


def _resumen_estado(ledger: HypothesisLedger, limite: int) -> dict:
    return {
        "preguntas": [
            {
                "id": q.id,
                "texto": q.text,
                "clave_canonica": q.canonical_key,
                "veces_preguntada": q.times_asked,
                "faltantes": list(ledger.missing_descriptions(q.id)),
                "hipotesis": len(q.hypothesis_ids),
            }
            for q in ledger.all_questions()[:limite]
        ],
        "hipotesis": [
            {
                "id": h.id,
                "claim": h.claim,
                "estado": h.status.value,
                "nivel": ledger.level(h.id),
                "flags": sorted(f.value for f in ledger.flags(h.id)),
                "pregunta_id": h.question_id,
                "reaperturas_acumuladas": ledger.reopen_count(h.id),
                "vinculos": len(h.evidence_links),
            }
            for h in ledger.hypotheses()[:limite]
        ],
        # El déficit es lo único que hay que conservar sí o sí cuando se compacta:
        # es la parte que evita reinventar la misma investigación.
        "deficits_pendientes": sorted(
            deficit
            for q in ledger.all_questions()
            for deficit in ledger.missing_descriptions(q.id)
        ),
        "evidencia_exogena": len(ledger.exogenous_evidence_ids()),
    }


def _operar(operacion: str, entrada: dict) -> tuple[dict, HypothesisLedger]:
    if operacion == "estado_vacio":
        return {"inicializado": True}, HypothesisLedger(clock=time.time)

    if operacion == "reanudar":
        ledger = _estado(entrada)  # valida el snapshot: si está roto, levanta
        return {
            "reanudado": True,
            "resumen": _resumen_estado(ledger, 20),
            # Reanudar un estado guardado es EXACTAMENTE el momento en que hay que
            # mirarlo: la carga valida estructura, no sentido. El diagnóstico
            # viaja con la respuesta para que un snapshot absurdo se vea al
            # abrirlo y no tres días después.
            "diagnostico": diagnosticar(ledger).to_dict(),
        }, ledger

    if operacion == "consultar":
        ledger = _estado(entrada)
        limite = int(entrada.get("limite") or 20)
        return _resumen_estado(ledger, max(1, limite)), ledger

    if operacion == "abrir_pregunta":
        ledger = _estado(entrada)
        pregunta = entrada.get("pregunta")
        if not isinstance(pregunta, str) or not pregunta.strip():
            raise _ErrorDeContrato("'pregunta' es obligatoria (texto)")
        faltantes = entrada.get("faltantes") or []
        if not isinstance(faltantes, list) or not all(isinstance(f, str) for f in faltantes):
            raise _ErrorDeContrato("'faltantes' tiene que ser una lista de textos")
        registrada, creada = ledger.open_question(
            pregunta, missing=faltantes, reopen_conditions=_condiciones(entrada, clave="reabrir_con")
        )
        return {
            "pregunta_id": registrada.id,
            "creada": creada,
            "clave_canonica": registrada.canonical_key,
            "veces_preguntada": registrada.times_asked,
            "faltantes": list(ledger.missing_descriptions(registrada.id)),
        }, ledger

    if operacion == "registrar_hipotesis":
        ledger = _estado(entrada)
        claim = entrada.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise _ErrorDeContrato("'claim' es obligatorio (una sola proposición)")
        condiciones = _condiciones(entrada, clave="reabrir_con") + _condiciones(
            entrada, clave="refutar_con"
        )
        hipotesis = ledger.record_hypothesis(
            claim,
            question_id=entrada.get("pregunta_id") or "",
            depends_on=entrada.get("depende_de") or (),
            reopen_conditions=condiciones,
        )
        return {
            "hipotesis_id": hipotesis.id,
            "estado": hipotesis.status.value,
            "nivel": ledger.level(hipotesis.id),
            # El estado no lo elige quien registra: nace OPEN y lo cambia la
            # evidencia. Es la misma frontera que las tools del host.
            "nota": "nace SIN respaldo; el estado lo cambia la evidencia, no el llamador",
        }, ledger

    if operacion in {"ingerir_evidencia", "simular_evidencia"}:
        ledger = _estado(entrada)
        contenido = entrada.get("contenido")
        if not isinstance(contenido, str) or not contenido.strip():
            raise _ErrorDeContrato("'contenido' es obligatorio (el texto de la observación)")
        try:
            origen = EvidenceOrigin(entrada.get("origen") or "")
        except ValueError:
            raise _ErrorDeContrato(
                "'origen' tiene que ser uno de: "
                + ", ".join(o.value for o in EvidenceOrigin)
            ) from None
        # MODEL_GENERATED se puede registrar (pesa 0 y queda trazable), pero el
        # matcher nunca lo deja reabrir nada: la regla es estructural y vive en la
        # librería, no acá.
        evidencia = Evidence.from_content(
            contenido,
            origin=origen,
            source=str(entrada.get("fuente") or ""),
            method=str(entrada.get("metodo") or ""),
            anchor=str(entrada.get("ancla") or ""),
            anchor_kind=str(entrada.get("ancla_tipo") or ""),
            acquired_at=time.time(),
            recheckable=True,
        )

        if operacion == "simular_evidencia":
            # Dry-run sobre una COPIA: el estado del llamador no se toca.
            copia = HypothesisLedger.from_snapshot(ledger.snapshot(), clock=time.time)
            antes = {h.id: copia.reopen_count(h.id) for h in copia.hypotheses()}
            copia.consider(evidencia)
            return {
                "simulado": True,
                "aplicado": False,
                "reabriria": _decisiones(copia, antes),
                "evidencia": {"origen": evidencia.origin.value, "hash": evidencia.content_hash},
            }, ledger

        antes = {h.id: ledger.reopen_count(h.id) for h in ledger.hypotheses()}
        ledger.consider(evidencia)
        return {
            "evidencia_id": evidencia.id,
            "origen": evidencia.origin.value,
            "hash": evidencia.content_hash,
            "reaperturas": _decisiones(ledger, antes),
            "advertencia_origen": (
                "el origen lo declaraste vos: si es 'model_generated' no reabre nada "
                "(regla estructural), pero si declarás mal un texto del modelo como "
                "'kernel_observed', la procedencia queda fabricada."
            )
            if origen is EvidenceOrigin.MODEL_GENERATED
            else "",
        }, ledger

    if operacion == "abstencion":
        ledger = _estado(entrada)
        pregunta = None
        if entrada.get("pregunta_id"):
            pregunta = ledger.question(str(entrada["pregunta_id"]))
        elif entrada.get("pregunta"):
            pregunta = ledger.question_by_key(str(entrada["pregunta"]))
        if pregunta is None:
            return {
                "tipo": None,
                "texto": (
                    "No hay pregunta registrada con ese id ni con esa clave canónica: no se "
                    "puede rendir una abstención estable sin el registro. Abrila primero con "
                    "'abrir_pregunta'."
                ),
                "faltantes": [],
            }, ledger
        faltantes = entrada.get("faltantes") or ledger.missing_descriptions(pregunta.id)
        necesita = str(entrada.get("necesita_condicion") or "")
        tipo = classify_abstention(ledger.hypotheses_of(pregunta.id), needs_condition=necesita)
        abstencion = render_abstention(
            pregunta, tipo, missing=faltantes, needs_condition=necesita
        )
        return {
            "tipo": abstencion.kind.value,
            "texto": abstencion.text,
            "faltantes": list(abstencion.missing),
            "clave_canonica": abstencion.question_key,
            "nota": (
                "el texto es función de (tipo, déficit), no de la redacción de la pregunta: "
                "dos paráfrasis de la misma pregunta rinden la MISMA abstención."
            ),
        }, ledger

    if operacion == "trayectoria":
        ledger = _estado(entrada)
        hid = str(entrada.get("hipotesis_id") or "")
        if not hid:
            raise _ErrorDeContrato("'hipotesis_id' es obligatorio")
        datos = ledger.trayectoria_de(hid).to_dict()
        limite = entrada.get("limite")
        if limite:
            datos["transiciones"] = datos["transiciones"][-max(1, int(limite)):]
        return datos, ledger

    if operacion == "diagnosticar":
        ledger = _estado(entrada)
        # Los monitores de la historia (mejora A): verifican lo que la carga de un
        # snapshot no verifica — que los contadores de reapertura correspondan a
        # lo que pasó, que el estado sea el que la historia produce, que ninguna
        # clave de dedup quede huérfana. Sirve para auditar un estado que llegó de
        # afuera antes de confiar en él.
        return diagnosticar(ledger).to_dict(), ledger

    if operacion == "compactar":
        ledger = _estado(entrada)
        presupuesto = PresupuestoDeCompactacion(
            **_solo_presentes(
                entrada,
                "max_hipotesis_terminales_por_pregunta",
                "max_caracteres_de_claim",
                "max_entradas_de_historial",
            )
        )
        reporte = ledger.compact(presupuesto)
        return dataclasses.asdict(reporte), ledger

    raise _ErrorDeContrato(f"operación desconocida: {operacion!r}")  # inalcanzable si el set está bien


def _solo_presentes(entrada: dict, *claves: str) -> dict:
    return {clave: entrada[clave] for clave in claves if entrada.get(clave) is not None}


class EpistemicLedgerTool(Tool):
    """
    La Tool de la fachada. `execute` es el único punto de entrada y su firma es
    deliberadamente mínima: `operacion` + `entrada`. Es un contrato JSON, no una
    API de Python, porque del otro lado hay otro agente (posiblemente en otro
    lenguaje) y porque así no hay forma de que este archivo empiece a acumular
    parámetros que después haya que mantener en dos lugares si la librería cambia.
    """

    def execute(self, operacion: str, entrada: dict | None = None) -> Artifact:
        entrada = dict(entrada or {})
        respuesta: dict = {"ok": False, "operacion": operacion, "resultado": None, "estado": None}

        try:
            if operacion not in _OPERACIONES:
                raise _ErrorDeContrato(
                    f"operación desconocida: {operacion!r}. Válidas: {', '.join(_OPERACIONES)}"
                )
            desconocidas = set(entrada) - _CLAVES_POR_OPERACION[operacion]
            if desconocidas:
                raise _ErrorDeContrato(
                    f"claves desconocidas para '{operacion}': {sorted(desconocidas)}. "
                    "El contrato rechaza lo que no entiende en vez de ignorarlo."
                )
            if not entrada.get("estado") and operacion not in ("estado_vacio", "abrir_pregunta",
                                                              "registrar_hipotesis"):
                # Sin estado no hay nada que continuar; se dice explícitamente en
                # vez de inventar un ledger vacío y devolver resultados vacíos.
                raise _ErrorDeContrato(
                    f"'{operacion}' necesita 'estado' (el snapshot que devolvió la llamada "
                    "anterior). Esta skill no persiste nada entre invocaciones."
                )
            resultado, ledger = _operar(operacion, entrada)
            respuesta = {
                "ok": True,
                "operacion": operacion,
                "resultado": resultado,
                "estado": ledger.snapshot(),
            }
        except _ErrorDeContrato as e:
            respuesta["error"] = str(e)
        except json.JSONDecodeError as e:
            respuesta["error"] = f"'estado' no es JSON legible: {e}"
        except Exception as e:  # noqa: BLE001 — la librería falla cerrado y ruidoso
            # Un estado inconsistente, una referencia desconocida, un tope
            # excedido: todo eso es un error del llamador, y se reporta con su
            # mensaje (que es el diagnóstico) en vez de devolver un resultado
            # plausible. La librería ya decidió no reinterpretar.
            respuesta["error"] = f"{type(e).__name__}: {e}"

        respuesta["advisory"] = {**_ADVISORY, "version_esquema": self._version(entrada)}
        return Artifact(
            modality="text",
            uri="",
            metadata={
                "summary": json.dumps(respuesta, ensure_ascii=False, sort_keys=True),
                "respuesta": respuesta,
            },
        )

    @staticmethod
    def _version(entrada: dict) -> object:
        """
        Versión de esquema del estado que entró, para que el adoptante pueda
        detectar que está hablando con una librería más nueva o más vieja. Es
        informativo: si el estado no se puede leer, el error ya se reportó arriba
        y acá se devuelve `None` en vez de romper el reporte del error.
        """
        crudo = entrada.get("estado")
        if not crudo:
            return None
        try:
            datos = json.loads(crudo) if isinstance(crudo, str) else crudo
        except (TypeError, json.JSONDecodeError):
            return None
        return datos.get("schema_version") if isinstance(datos, dict) else None

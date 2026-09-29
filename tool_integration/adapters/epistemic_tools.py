"""
Tools epistémicas que el modelo puede llamar: abrir una pregunta y registrar
una hipótesis.

Este archivo es chico a propósito, porque es una FRONTERA DE SEGURIDAD:

- **Ninguna de las dos acepta `origin`, `evidence` ni `links`.** No es que los
  ignoren: las firmas de `execute()` no tienen esos parámetros, así que pasarlos
  es un `TypeError` que `_dispatch_tool` convierte en "argumentos inválidos". El
  modelo no puede fabricar evidencia exógena ni declararse respaldado, porque no
  hay parámetro por donde hacerlo. La firma ES la frontera.
- **Sin `**kwargs`.** Un `**kwargs` habría reabierto exactamente el agujero que
  las firmas cierran, y hay un test que lo fija.
- **El estado no lo pone el modelo.** Registrar una hipótesis siempre la deja en
  `open`: el estado lo cambia la evidencia, o un humano — nunca el que propone.
- **Nada levanta hacia el loop.** Toda violación de los contratos vuelve como un
  Artifact de texto con `ERROR: ...`, para que el modelo pueda corregir en el
  turno siguiente en vez de romper la tarea.
- **La deuda se declara con su condición de pago.** `open_question` acepta
  `resolved_by` (marcadores) y los adjunta al déficit: sin eso, un "me falta X"
  quedaba abierto para siempre, el gate bloqueaba respuestas que ya tenían la
  evidencia delante y el propio texto de la tool prometía algo que no pasaba
  (hallazgo D9 de F6, cerrado).
- **Las condiciones se declaran con marcadores, no con expresiones regulares**
  (`ReopenCondition.from_untrusted`): el camino no confiable no puede pasar
  patrones, y por eso no hay ReDoS posible acá.
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest

from kal_epistemics import (
    EpistemicsError,
    InvalidCondition,
    ReopenAction,
    ReopenCondition,
)

_MAX_ITEMS = 5


def _error(mensaje: str) -> Artifact:
    return Artifact(modality="text", uri="", metadata={"summary": f"ERROR: {mensaje}"})


def _condiciones(
    watch_for: list[str] | None, refuted_by: list[str] | None
) -> tuple[ReopenCondition, ...]:
    condiciones: list[ReopenCondition] = []
    for palabra in (watch_for or [])[:_MAX_ITEMS]:
        condiciones.append(
            ReopenCondition.from_untrusted(
                description=f"observación que menciona '{palabra}'",
                content_keywords=(palabra,),
                action=ReopenAction.REOPEN,
            )
        )
    for palabra in (refuted_by or [])[:_MAX_ITEMS]:
        condiciones.append(
            ReopenCondition.from_untrusted(
                description=f"observación que menciona '{palabra}'",
                content_keywords=(palabra,),
                action=ReopenAction.REFUTE,
            )
        )
    return tuple(condiciones)


class OpenQuestionTool(Tool):
    manifest = ToolManifest(
        name="open_question",
        description=(
            "Registra una pregunta que todavía no podés responder, junto con lo que haría "
            "falta para responderla y con qué la saldaría. Queda guardada: si más adelante "
            "una herramienta trae la información que falta, el sistema te lo hace saber y la "
            "deuda queda saldada sola. Usala en vez de inventar una respuesta."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "La pregunta, tal como la harías.",
                },
                "missing": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Qué información haría falta para responderla (máximo 5).",
                },
                "resolved_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Marcadores (mínimo 3 letras) que, si aparecen en una observación, "
                        "saldan la deuda: sin esto, la pregunta queda abierta para siempre y "
                        "el sistema no te va a dejar afirmar nada al respecto. "
                        "Ej: 'MemAvailable', 'sysctl swappiness'."
                    ),
                },
            },
            "required": ["question"],
        },
        created_by="system",
    )

    def __init__(self, epistemics, session_id: str | None = None):
        self.epistemics = epistemics
        self.session_id = session_id

    def execute(
        self,
        question: str,
        missing: list[str] | None = None,
        resolved_by: list[str] | None = None,
    ) -> Artifact:
        try:
            condiciones = _condiciones(resolved_by, None)
        except InvalidCondition as e:
            return _error(
                f"{e}. Usá marcadores de al menos 3 letras, como 'OOM', 'timeout' o 'denied'"
            )

        ledger = self.epistemics.ledger(self.session_id)
        try:
            registrada, creada = ledger.open_question(
                question,
                missing=(missing or [])[:_MAX_ITEMS],
                reopen_conditions=condiciones,
            )
        except EpistemicsError as e:
            return _error(f"no se pudo registrar la pregunta: {e}")

        estado = "nueva" if creada else f"ya existía (preguntada {registrada.times_asked} veces)"
        faltan = ledger.missing_descriptions(registrada.id)
        detalle = f" Falta: {'; '.join(faltan)}." if faltan else ""
        cierre = (
            "La deuda se salda sola si aparece evidencia con: "
            f"{', '.join(resolved_by[:_MAX_ITEMS])}. "
            if resolved_by
            else "No declaraste con qué se salda: la pregunta va a quedar abierta y el "
            "sistema no te va a dejar afirmar nada al respecto. "
        )
        return Artifact(
            modality="text",
            uri="",
            metadata={
                "summary": (
                    f"Pregunta registrada (id={registrada.id}, {estado}) y SIN RESOLVER."
                    f"{detalle} {cierre}No la respondas con una suposición: si tenés una "
                    "explicación posible, registrala con record_hypothesis."
                )
            },
        )


class RecordHypothesisTool(Tool):
    manifest = ToolManifest(
        name="record_hypothesis",
        description=(
            "Registra una explicación posible (una hipótesis) para que el sistema la conserve y "
            "la revise sola cuando aparezca evidencia. Nace SIN respaldo: no la presentes como "
            "un hecho. Podés declarar con qué evidencia se reabriría y con cuál quedaría "
            "refutada."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "claim": {
                    "type": "string",
                    "description": "La proposición, una sola y concreta.",
                },
                "question_id": {
                    "type": "string",
                    "description": "Id de la pregunta que responde (lo devuelve open_question).",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids de hipótesis de las que depende esta (máximo 5).",
                },
                "watch_for": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Marcadores (mínimo 3 letras) que, si aparecen en una observación, "
                        "reabren esta hipótesis. Ej: 'OOM', 'timeout'."
                    ),
                },
                "refuted_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Marcadores que la refutarían. Ej: 'rollback', 'denied'.",
                },
                "excluye_a": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ids de hipótesis que esta EXCLUYE por ser alternativas "
                        "incompatibles (no pueden ser verdad juntas). Si más adelante "
                        "las dos quedan respaldadas, el sistema lo marca como conflicto "
                        "en vez de dejar que elijas una en silencio."
                    ),
                },
            },
            "required": ["claim"],
        },
        created_by="system",
    )

    def __init__(self, epistemics, session_id: str | None = None):
        self.epistemics = epistemics
        self.session_id = session_id

    def execute(
        self,
        claim: str,
        question_id: str | None = None,
        depends_on: list[str] | None = None,
        watch_for: list[str] | None = None,
        refuted_by: list[str] | None = None,
        excluye_a: list[str] | None = None,
    ) -> Artifact:
        try:
            condiciones = _condiciones(watch_for, refuted_by)
        except InvalidCondition as e:
            return _error(
                f"{e}. Usá marcadores de al menos 3 letras, como 'OOM', 'timeout' o 'denied'"
            )

        ledger = self.epistemics.ledger(self.session_id)
        try:
            hipotesis = ledger.record_hypothesis(
                claim,
                question_id=question_id or "",
                depends_on=(depends_on or [])[:_MAX_ITEMS],
                reopen_conditions=condiciones,
                excludes=(excluye_a or [])[:_MAX_ITEMS],
            )
        except EpistemicsError as e:
            return _error(f"no se pudo registrar la hipótesis: {e}")

        cierre = (
            f"Quedaría refutada si aparece: {', '.join(refuted_by[:_MAX_ITEMS])}."
            if refuted_by
            else "No declaraste con qué quedaría refutada."
        )
        reapertura = (
            f"Se reabrirá sola si aparece evidencia con: {', '.join(watch_for[:_MAX_ITEMS])}. "
            if watch_for
            else ""
        )
        return Artifact(
            modality="text",
            uri="",
            metadata={
                "summary": (
                    f"Hipótesis registrada (id={hipotesis.id}, estado=open, SIN respaldo). "
                    f"No la presentes como un hecho. {reapertura}{cierre}"
                )
            },
        )

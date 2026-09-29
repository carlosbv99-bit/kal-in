"""
El gate: la última puerta antes de que un texto salga hacia el usuario.

Qué es y qué NO es, porque la diferencia decide si sirve o es decoración:

- **Es un control mecánico y fail-closed.** Dos reglas, las dos decidibles sin
  llamar a ningún modelo, porque pedirle al mismo modelo que juzgue si su
  respuesta está respaldada es una segunda opinión correlacionada, no una
  verificación.
- **NO es un detector de alucinaciones.** No lee la respuesta buscando
  afirmaciones falsas: eso requeriría entender el texto, que es justo lo que no
  se le puede confiar al modelo. Detecta dos situaciones que SÍ son decidibles
  y que son las que producen las peores respuestas:

    R1 (BLOQUEAR) — el agente declaró un déficit ("me falta E7") y después
       respondió igual, sin marcar la incertidumbre. Es el patrón más
       destructivo de todos: el sistema SABÍA que no le alcanzaba y contestó
       como si le alcanzara.
    R2 (ADVERTIR) — el turno terminó sin una sola observación ingerida y la
       respuesta es larga Y NO admite incertidumbre: se emite con la marca
       explícita de que no hubo verificación, en vez de dejarla pasar con la
       misma apariencia que una respuesta que sí la tuvo.

       El "y no admite incertidumbre" lo agregó F6, con datos: con el umbral
       calibrado, la regla sin ese filtro le ponía el cartel de "sin
       verificación" a 8 de 8 respuestas que YA declaraban que no sabían (ver
       `_UMBRAL_RESPUESTA_LARGA`). El cartel existe para distinguir la
       apariencia de una respuesta respaldada; en una que ya dice "no lo sé" no
       distingue nada y es ruido. El costo de este filtro está medido y a la
       vista: la afirmación disfrazada de duda (A12 en el corpus) deja de
       marcarse.

- **Falla cerrado.** Cualquier error interno del gate BLOQUEA, no deja pasar.
  Un gate que ante un bug se abre es peor que no tener gate, porque se confía
  en él.

Sobre la calidad de los marcadores de incertidumbre: la lista de abajo es un
heurístico de v1, y hay que decirlo en vez de disimularlo. Un marcador que no
esté en la lista hace que una respuesta legítimamente hedged se bloquee (falso
positivo), y uno de más deja pasar una afirmación segura de sí misma (falso
negativo). Calibrar esa lista con datos reales es F6 — lo que se congela ahora
es el CONTRATO (tres resultados, choke point único, fail-closed), no la
inteligencia.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from agent_core.epistemics.store import EpistemicStore
from kal_epistemics import is_abstention_text

logger = logging.getLogger(__name__)

_UMBRAL_RESPUESTA_LARGA = 64
"""
A partir de cuántos caracteres una respuesta sin ninguna observación se marca
como no verificada (R2).

**Calibrado en F6 con datos, no a ojo.** Barrido sobre el conjunto K (41
respuestas etiquetadas a mano; ver `docs/MEDICION-CAPA-EPISTEMICA.md`):

| Umbral | Afirmativas marcadas | Conversacionales marcadas |
|---|---|---|
| 240 (valor anterior) | 1 de 10 | 0 de 5 |
| 64 (valor actual) | 9 de 10 | 0 de 5 |
| 18 | 10 de 10 | 2 de 5 |

El valor anterior (240) dejaba pasar sin marca 9 de cada 10 afirmaciones sin
respaldo del corpus: en la práctica R2 casi no existía. 64 es el umbral más
alto que sigue marcando 9 de 10 afirmaciones sin tocar ninguna respuesta de
trámite. La afirmativa que queda sin marcar tiene 21 caracteres, por debajo de
cualquier umbral que no marque también un "¿Querés que lo revise ahora?".

Este umbral solo se aplica a respuestas que NO admiten incertidumbre (ver R2 en
el docstring del módulo): en el barrido, con 64 los 8 hedges del contexto sin
observaciones también superaban el umbral, y marcarlos era ruido puro.

Revisar este número cuando haya tráfico real: el corpus lo escribió el mismo
autor que el gate, así que mide consistencia y error sobre una muestra, no la
distribución real de longitudes.
"""

_MARCADORES_DE_INCERTIDUMBRE: tuple[str, ...] = (
    "creo que",
    "desconozco",
    "diría que",
    "es posible que",
    "es probable que",
    "haría falta",
    "hasta donde sé",
    "no consta",
    "no está claro",
    "no estoy seguro",
    "no lo aseguraría",
    "no lo pude comprobar",
    "no lo puedo confirmar",
    "no lo sé",
    "no pude verificarlo",
    "no tengo certeza",
    "no tengo datos",
    "no tengo información",
    "posiblemente",
    "presumiblemente",
    "probablemente",
    "podría",
    "quizá",
    "sin evidencia",
)
"""
Los marcadores que el gate reconoce como "esto no es una afirmación".

**Calibrado en F6.** Con los 12 marcadores originales, sobre K: 11 hedges
reconocidos, 7 NO reconocidos (esos 7 se habrían BLOQUEADO con un déficit
declarado — castigar al modelo por ser honesto, el peor incentivo posible) y 1
afirmación tomada por hedge. Los 12 marcadores de arriba salen de una regla
mecánica aplicada a la tabla del reporte: se agrega todo candidato del corpus
que elimine al menos un falso negativo y no agregue ningún falso positivo.
Resultado medido: 18 hedges reconocidos, 0 falsos negativos, 1 falso positivo.

El falso positivo que queda, y que NO se tapa, es el de `podría` sobre una
afirmación disfrazada de duda ("el token podría estar en X, y de hecho sigue
vigente"): está medido en K (K28) y en el conjunto de evaluación (A12), donde
el gate lo deja pasar. Es el costo conocido de un heurístico de substrings, y
la forma de no perderlo de vista es que siga apareciendo en el reporte — no
alargar la lista hasta que desaparezca del corpus.
"""

_SUFIJO_SIN_VERIFICACION = (
    "\n\n[Sin verificación: en este turno no se consultó ninguna fuente ni se ejecutó "
    "ninguna herramienta, así que lo de arriba no está respaldado por una observación.]"
)


class GateOutcome(str, Enum):
    PERMITIR = "permitir"
    ADVERTIR = "advertir"
    BLOQUEAR = "bloquear"


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    answer: str
    reason: str
    deficits: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.outcome is GateOutcome.BLOQUEAR


def _texto_bloqueo(deficits: tuple[str, ...]) -> str:
    falta = "; ".join(deficits) if deficits else "la información que dejé pendiente"
    return (
        "No voy a afirmar esto como un hecho: dejé registrado que me faltaba "
        "información para responderlo, y no la conseguí en este turno.\n\n"
        f"Falta: {falta}.\n\n"
        "Puedo plantearte las hipótesis que tengo, pero ninguna está respaldada por una "
        "observación, así que no te las voy a dar como respuesta."
    )


def _tiene_marcador_de_incertidumbre(
    texto: str, *, marcadores: tuple[str, ...] = _MARCADORES_DE_INCERTIDUMBRE
) -> bool:
    """
    El único heurístico del gate, y por eso el único que F6 calibra.

    `marcadores` existe para que la calibración pueda MEDIR candidatos sobre el
    corpus etiquetado sin tocar la constante que usa producción (ver
    `agent_core/epistemics/measurement/`). El camino de producción siempre usa
    el default; no hay ningún llamador que pase otra cosa.
    """
    if is_abstention_text(texto):
        return True
    plano = texto.casefold()
    return any(marcador in plano for marcador in marcadores)


def evaluate_answer(
    answer: str,
    *,
    store: EpistemicStore,
    session_id: str | None,
    evidence_before: int,
    trivial: bool = False,
) -> GateDecision:
    """
    Decide si esta respuesta sale, sale marcada, o no sale.

    `evidence_before` es el valor de `store.ingested_count(session_id)` tomado
    ANTES del turno: la diferencia dice si el turno tuvo alguna base observada,
    que es el único dato mecánicamente confiable sobre la procedencia de lo que
    se está por afirmar.
    """
    try:
        return _evaluate(
            answer,
            store=store,
            session_id=session_id,
            evidence_before=evidence_before,
            trivial=trivial,
        )
    except Exception:  # noqa: BLE001 — deliberado: ver docstring del módulo
        logger.exception("El gate falló evaluando la respuesta; se bloquea (fail-closed)")
        return GateDecision(
            outcome=GateOutcome.BLOQUEAR,
            answer=(
                "No voy a responder esto: la verificación de respaldo falló antes de poder "
                "emitir la respuesta. Es un problema del sistema, no de tu pregunta."
            ),
            reason="gate_error",
        )


def _evaluate(
    answer: str,
    *,
    store: EpistemicStore,
    session_id: str | None,
    evidence_before: int,
    trivial: bool,
) -> GateDecision:
    texto = (answer or "").strip()
    if not texto:
        return GateDecision(GateOutcome.PERMITIR, answer=answer, reason="respuesta_vacia")

    deficits = store.unsatisfied_deficits(session_id)
    if deficits and not _tiene_marcador_de_incertidumbre(texto):
        return GateDecision(
            outcome=GateOutcome.BLOQUEAR,
            answer=_texto_bloqueo(deficits),
            reason="deficit_declarado_y_respondido_igual",
            deficits=deficits,
        )

    ingeridas = store.ingested_count(session_id) - evidence_before
    if (
        not trivial
        and ingeridas <= 0
        and len(texto) > _UMBRAL_RESPUESTA_LARGA
        and not _tiene_marcador_de_incertidumbre(texto)
    ):
        return GateDecision(
            outcome=GateOutcome.ADVERTIR,
            answer=texto + _SUFIJO_SIN_VERIFICACION,
            reason="sin_observaciones_en_el_turno",
        )

    return GateDecision(GateOutcome.PERMITIR, answer=texto, reason="ok")

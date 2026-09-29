"""
Salidas legítimas de "no lo sé", con redacción ESTABLE.

El valor real de conservar preguntas sin resolver no es "el sistema sabe
más", es **abstención repetible**: un modelo sin registro de su propio "no lo
sé" contesta distinto cada vez que se le pregunta lo mismo — la segunda vez,
con la pregunta reformulada, improvisa. Con el registro responde lo mismo, y
cuesta un lookup.

Por eso la redacción es función de (tipo de ignorancia, déficit) y de nada
más: no incluye la fecha, ni el `times_asked`, ni el texto de la pregunta, ni
ids efímeros. Dos paráfrasis de la misma pregunta producen EXACTAMENTE el
mismo texto de abstención. El texto de la pregunta lo agrega el host si
quiere mostrarlo — la capa epistémica garantiza el contenido, el host se
encarga de la presentación.

Los tres tipos (del diseño original) son cualitativamente distintos y no
conviene colapsarlos en un "no sé" genérico:

- `IGNORANCE`: falta información, y se sabe CUÁL falta. Es el caso accionable
  por adquisición: "haría falta E7".
- `UNDETERMINATION`: hay hipótesis o evidencia, pero no alcanza para decidir
  entre varios estados del mundo compatibles con todo lo disponible.
- `ILL_POSED`: el problema no es conocimiento, es que falta una condición en
  la pregunta. Se declara explícitamente (`needs_condition`) porque "a esta
  pregunta le falta una premisa" es un juicio sobre la pregunta, no algo
  inferible del grafo de evidencia. Adivinarlo sería exactamente el tipo de
  relleno de vacíos que este sistema existe para evitar.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from kal_epistemics.models import Hypothesis, Question


class AbstentionKind(str, Enum):
    IGNORANCE = "ignorance"
    UNDERDETERMINATION = "underdetermination"
    ILL_POSED = "ill_posed"


@dataclass(frozen=True)
class Abstention:
    """Una abstención: qué tipo es, qué dice, y qué falta."""

    kind: AbstentionKind
    text: str
    missing: tuple[str, ...] = ()
    question_key: str = ""
    needs_condition: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "missing": list(self.missing),
            "question_key": self.question_key,
            "needs_condition": self.needs_condition,
        }


ABSTENTION_MARKERS: tuple[str, ...] = (
    "no lo sé",
    "no puedo determinarlo",
    "no hay una respuesta única",
)
"""
Las frases que produce `_cuerpo()`. Existen para que el gate pueda reconocer una
abstención sin reimplementar la redacción: si el renderer y el gate tuvieran cada
uno su lista, se desincronizarían en el primer cambio de wording y el gate
empezaría a bloquear abstenciones legítimas (o a dejar pasar respuestas que se
declararon inciertas). Hay un test que renderiza los tres tipos y exige que
`is_abstention_text()` los reconozca a todos.
"""


def is_abstention_text(text: str) -> bool:
    """True si el texto ES una abstención (o la contiene). Lo usa el gate."""
    plano = text.casefold()
    return any(marcador in plano for marcador in ABSTENTION_MARKERS)


def _cuerpo(
    kind: AbstentionKind,
    missing: Sequence[str],
    needs_condition: str,
) -> str:
    """
    Construcción determinista del texto. Sin timestamps, sin ids, con el
    déficit ordenado: la misma entrada produce byte a byte la misma salida.
    """
    ordenado = sorted({m.strip() for m in missing if m and m.strip()})

    if kind is AbstentionKind.ILL_POSED:
        base = (
            "No hay una respuesta única para esto sin especificar una condición "
            "adicional."
        )
        if needs_condition.strip():
            base += f" Falta especificar: {needs_condition.strip()}."
        return base

    if kind is AbstentionKind.UNDERDETERMINATION:
        base = (
            "No puedo determinarlo: con la información disponible hay más de un "
            "estado posible compatible con todo lo que sé, y ninguno está lo "
            "suficientemente respaldado para darte una respuesta fiable."
        )
    else:
        base = "No lo sé: no tengo información suficiente para responder esto."

    if ordenado:
        base += " Haría falta: " + "; ".join(ordenado) + "."
    return base


def render_abstention(
    question: Question,
    kind: AbstentionKind,
    *,
    missing: Iterable[str] = (),
    needs_condition: str = "",
) -> Abstention:
    """
    Arma la abstención. `kind` es obligatorio a propósito: inferirlo de
    strings sería magia, y la clasificación mecánica ya existe en
    `classify_abstention()`.
    """
    descripciones = tuple(sorted({m.strip() for m in missing if m and m.strip()}))
    return Abstention(
        kind=kind,
        text=_cuerpo(kind, descripciones, needs_condition),
        missing=descripciones,
        question_key=question.canonical_key,
        needs_condition=needs_condition.strip(),
    )


def classify_abstention(
    hypotheses: Sequence[Hypothesis],
    *,
    needs_condition: str = "",
) -> AbstentionKind:
    """
    Clasificación mecánica del tipo de ignorancia, sin juicio del modelo:

    - Si se declaró una condición faltante en la pregunta → `ILL_POSED`.
    - Si no hay ninguna hipótesis registrada → `IGNORANCE` (no hay nada, y el
      déficit es lo único que hay para decir).
    - Si hay una o más hipótesis → `UNDETERMINATION`. Da igual si están
      apoyadas en evidencia que no decide o si no están respaldadas: en los
      dos casos el problema es que el mundo sigue admitiendo más de un
      estado.

    Notar que este orden hace imposible que un "no lo sé" vacío tape el caso
    accionable: `ILL_POSED` es el que dice "no necesito más conocimiento,
    necesito una premisa", y es el más barato de resolver de los tres.
    """
    if needs_condition.strip():
        return AbstentionKind.ILL_POSED
    if not hypotheses:
        return AbstentionKind.IGNORANCE
    return AbstentionKind.UNDERDETERMINATION

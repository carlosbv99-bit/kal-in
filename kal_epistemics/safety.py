"""
Límites y validación de patrones: esta capa recibe input NO confiable.

Seguridad primero, aplicado a esta capa en concreto. Cuando `open_question` y
`record_hypothesis` existan como tools (F2a), el modelo —y por lo tanto
cualquier inyección de prompt que lo convenza— pasa a escribir directamente
en estos objetos. Tres superficies reales, y su mitigación:

1. **Regex sobre texto no confiable = ReDoS.** Una condición de reapertura
   lleva patrones, y el matcher corre en el camino crítico cada vez que llega
   evidencia. Un patrón catastrófico como `(a+)+$` contra una línea de log
   arbitraria cuelga al agente. Mitigación ESTRUCTURAL, no de filtro: el
   camino no confiable no acepta regex en absoluto
   (`ReopenCondition.from_untrusted()` solo toma selectores enumerados y
   substrings, y no tiene parámetros de patrón). Los campos de regex quedan
   para condiciones escritas por código o por un humano, con tope de longitud
   y rechazo de cuantificadores anidados — y si un patrón no se puede
   analizar, se rechaza. Un filtro es un heurístico; no ofrecer el parámetro
   es una garantía.

2. **Crecimiento del ledger = agotamiento de recursos.** Un ledger sin topes,
   alimentado por el modelo, es un DoS trivial: hipótesis, condiciones,
   vínculos y evidencia ilimitados. Los topes de acá son explícitos y fallan
   cerrado (`LedgerLimitExceeded`).

3. **Contenido externo = dato, nunca instrucción.** Ya está en los contratos
   (`Evidence.content`); acá se agrega el tope de escaneo, que además acota el
   daño de cualquier patrón que sí haya pasado la validación.

Sobre el alcance de `pattern_is_unsafe()`: es un **heurístico, no una
prueba**. Detecta cuantificadores anidados sobre el árbol parseado, que es la
forma clásica de ReDoS, pero no cubre todas (una alternancia con ramas que se
solapan puede ser catastrófica sin cuantificadores anidados). Por eso no es
la defensa principal: la defensa principal es que el camino no confiable no
puede pasar patrones. Esto acota el daño del camino confiable.
"""
from __future__ import annotations

import re

# --- Patrones (solo camino confiable: código o humano) --------------------
MAX_PATTERN_LENGTH = 200
"""Tope del patrón. Un patrón más largo que esto no se evalúa: se rechaza."""

# --- Selectores seguros (camino no confiable) -----------------------------
MAX_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 200
MIN_KEYWORD_LENGTH = 3
"""
Largo mínimo de una palabra clave. Con una o dos letras la condición matchea
casi cualquier texto y se convierte en un amplificador de reaperturas: no es
ReDoS (no hay regex), es ruido dirigido. El mínimo de 3 deja pasar marcadores
reales y cortos como `OOM`, `TLS` o `DNS`.
"""
MAX_SOURCE_VALUES = 20

# --- Topes del ledger ----------------------------------------------------
MAX_CLAIM_LENGTH = 2000
MAX_QUESTION_LENGTH = 2000
MAX_DESCRIPTION_LENGTH = 1000
MAX_MISSING_PER_QUESTION = 20
MAX_HYPOTHESES_PER_QUESTION = 50
MAX_CONDITIONS_PER_HYPOTHESIS = 20
MAX_LINKS_PER_HYPOTHESIS = 50
MAX_DEPENDENCIES = 50
MAX_EVIDENCE_ENTRIES = 500
MAX_REOPEN_APPLICATIONS_PER_HYPOTHESIS = 10
"""
Presupuesto de reaperturas automáticas por hipótesis. Es el control que acota
el daño de una condición demasiado genérica: aunque una palabra clave matchee
todo, la hipótesis no puede cambiar de estado más de N veces. Sin esto, la
calidad de la palabra clave sería la única defensa, y la calidad de un texto
escrito por el modelo no es una garantía.
"""
MAX_REOPEN_APPLICATIONS_PER_QUESTION = 30
"""
Presupuesto de reaperturas automáticas por PREGUNTA (R8 del roadmap).

Por qué hace falta además del tope por hipótesis: ese tope no acota el total de
una pregunta, porque la MISMA observación se evalúa contra TODAS las hipótesis
del ledger, cada una con su propio presupuesto. La medición de F6 lo dejó medido:
con 5 hipótesis de la misma pregunta mirando el mismo marcador, **una sola
observación produce 5 reaperturas**, y el techo teórico de una pregunta es
10 × 50 = 500 aplicaciones.

El valor (30 = 3 × el tope por hipótesis) sale de la medición, no de una
intuición: una pregunta puede acumular el desgaste de tres hipótesis completas
antes de que el sistema deje de reabrir, mientras que en el corpus de F6 el
máximo observado por pregunta es 1–2. Es un valor inicial revisable con tráfico
real, y su costo está declarado: un tope así también puede impedir que una
hipótesis legítima reciba el respaldo que le llegó, así que si ese caso aparece,
el número se sube — con el dato de que está pasando.
"""
MAX_EVIDENCE_CONTENT_LENGTH = 100_000
"""
Tope de contenido por evidencia. Es un backstop del ledger, no la política de
ingestión: el adaptador (F2a) recorta mucho más abajo antes de guardar, porque
lo que se guarda después se reinyecta al contexto.
"""

MAX_CONTENT_SCAN = 2000
"""Tope de caracteres de `Evidence.content` que se escanean con un patrón."""

_REPEATS = tuple(
    constante
    for constante in (
        getattr(re._constants, nombre, None)  # noqa: SLF001 — ver docstring del módulo
        for nombre in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
    )
    if constante is not None
)


_SUBPATTERN = getattr(re._parser, "SubPattern", None)  # noqa: SLF001


def _es_subpatron(valor: object) -> bool:
    """
    True si `valor` es un nodo del árbol parseado.

    OJO — bug real encontrado por los tests: un `SubPattern` de `re` itera por
    `__getitem__`, NO implementa `__iter__`. La versión original de esta
    función chequeaba `hasattr(valor, "__iter__")`, devolvía False para los
    SubPattern anidados, y el recorrido nunca bajaba al cuerpo de los
    cuantificadores: `pattern_is_unsafe("(a+)+$")` devolvía `None`. Es decir,
    el control de seguridad existía, se veía bien, y no hacía absolutamente
    nada — la peor clase de fallo. Por eso el test del patrón catastrófico no
    es decorativo.
    """
    if isinstance(valor, (str, bytes, int)):
        return False
    if _SUBPATTERN is not None and isinstance(valor, _SUBPATTERN):
        return True
    return isinstance(valor, list)


def _iter_nodos(nodo: object):
    """Recorre el árbol parseado, ejecutando (op, av) de cada nodo."""
    try:
        items = [(op, av) for op, av in nodo]  # type: ignore[misc]
    except (TypeError, ValueError):
        return
    for op, av in items:
        yield op, av
        if isinstance(av, tuple):
            for parte in av:
                if _es_subpatron(parte):
                    yield from _iter_nodos(parte)
        elif _es_subpatron(av):
            yield from _iter_nodos(av)


def _contiene_cuantificador(nodo: object) -> bool:
    return any(op in _REPEATS for op, _ in _iter_nodos(nodo))


def _tiene_cuantificador_anidado(arbol: object) -> bool:
    """
    True si hay un cuantificador cuyo cuerpo contiene otro cuantificador —
    `(a+)+`, `(a*)*`, `(a|a)*`, la familia clásica de ReDoS exponencial.
    """
    for op, av in _iter_nodos(arbol):
        if op not in _REPEATS:
            continue
        cuerpo = av[2] if isinstance(av, tuple) and len(av) == 3 else av
        if _contiene_cuantificador(cuerpo):
            return True
    return False


def pattern_is_unsafe(pattern: str) -> str | None:
    """
    Devuelve el motivo del rechazo, o `None` si el patrón es aceptable.

    Falla cerrado por diseño: un patrón que no se puede ANALIZAR se rechaza,
    porque la alternativa sería aceptar algo cuyo costo no podemos acotar.
    """
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN_LENGTH:
        return "pattern_too_long"
    try:
        arbol = re._parser.parse(pattern, 0)  # noqa: SLF001 — ver docstring del módulo
    except re.error:
        return "invalid_pattern"
    except Exception:  # noqa: BLE001 — deliberado: si no se puede analizar, se rechaza
        return "unanalyzable_pattern"
    if _tiene_cuantificador_anidado(arbol):
        return "nested_quantifier"
    return None


def validate_keywords(keywords: tuple[str, ...]) -> None:
    """Valida los selectores seguros. Levanta ValueError describiendo el tope
    que se pasó — fail-closed: no se recorta en silencio."""
    if len(keywords) > MAX_KEYWORDS:
        raise ValueError(
            f"Demasiadas palabras clave: {len(keywords)} > {MAX_KEYWORDS}."
        )
    for keyword in keywords:
        if not keyword.strip():
            raise ValueError("Una palabra clave vacía no selecciona nada.")
        if len(keyword.strip()) < MIN_KEYWORD_LENGTH:
            raise ValueError(
                f"Palabra clave {keyword!r} demasiado corta: el mínimo es "
                f"{MIN_KEYWORD_LENGTH} caracteres. Una de una o dos letras matchea "
                "casi cualquier texto y convierte la condición en un amplificador "
                "de reaperturas."
            )
        if len(keyword) > MAX_KEYWORD_LENGTH:
            raise ValueError(
                f"Palabra clave de {len(keyword)} caracteres > {MAX_KEYWORD_LENGTH}."
            )


def validate_sources(sources: tuple[str, ...]) -> None:
    if len(sources) > MAX_SOURCE_VALUES:
        raise ValueError(f"Demasiadas fuentes: {len(sources)} > {MAX_SOURCE_VALUES}.")
    for source in sources:
        if not source.strip():
            raise ValueError("Una fuente vacía no selecciona nada.")

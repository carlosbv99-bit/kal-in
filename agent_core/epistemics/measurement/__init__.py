"""
Suite de medición y calibración de la capa epistémica (fase F6).

Existe porque "anda en los tests" no es lo mismo que "es confiable": los tests
de F1/F2 usan dobles (stores construidos a mano, respuestas inyectadas) y
verifican contratos. Lo que faltaba era lo único que autoriza a tocar la
"inteligencia" del sistema — la redacción de los hedges y los umbrales — con
números sobre la mesa en vez de con una opinión.

Cómo se usa:

```python
from agent_core.epistemics.measurement import cargar_corpus, correr_suite, render_markdown

reporte = correr_suite()                 # corre el corpus por el camino real
print(reporte.metricas)
open("docs/MEDICION-CAPA-EPISTEMICA.md", "w").write(render_markdown(reporte))
```

O desde la línea de comandos: `python scripts/measure_epistemics.py`.

Las tres piezas, y por qué están separadas:

- `corpus.py` — los DATOS y su validación fail-closed. El instrumento no vive
  mezclado con el código, y evaluación y calibración son conjuntos disjuntos.
- `harness.py` — la ejecución por el camino real y el cálculo de métricas.
- `report.py` — el render a markdown, generado desde el mismo reporte que el
  JSON (ningún número escrito a mano).

Ver `agent_core/epistemics/measurement/harness.py` para el detalle de qué es
real y qué está scripteado en la medición: es la parte que decide cuánto valen
los números.
"""
from __future__ import annotations

from agent_core.epistemics.measurement.corpus import (
    CONJUNTOS,
    Corpus,
    CorpusInvalido,
    Item,
    ItemCalibracion,
    cargar_corpus,
    tamanos,
)
from agent_core.epistemics.measurement.harness import (
    MatrizMarcadores,
    Reporte,
    ResultadoItem,
    calibrar,
    calcular_metricas,
    correr_suite,
    ejecutar_item,
    matriz_marcadores,
    verificaciones_estructurales,
)
from agent_core.epistemics.measurement.report import render_markdown

__all__ = [
    "CONJUNTOS",
    "Corpus",
    "CorpusInvalido",
    "Item",
    "ItemCalibracion",
    "MatrizMarcadores",
    "Reporte",
    "ResultadoItem",
    "calibrar",
    "calcular_metricas",
    "cargar_corpus",
    "correr_suite",
    "ejecutar_item",
    "matriz_marcadores",
    "render_markdown",
    "tamanos",
    "verificaciones_estructurales",
]

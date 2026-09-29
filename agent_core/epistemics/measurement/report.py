"""
Render del reporte de medición (F6) a markdown.

El markdown se GENERA desde el mismo `Reporte` que produce el JSON: no hay
números escritos a mano en ningún lado. Es deliberado — el patrón de error que
este proyecto ya cometió tres veces fue inferir de un proxy en vez de leer la
cosa, y un número copiado a mano en un documento es un proxy que envejece solo.
Si un número del documento no está en el JSON, es un bug del renderer.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent_core.epistemics.measurement.harness import Reporte

COMO_REGENERAR = (
    "`python scripts/measure_epistemics.py --escribir` (regenera este documento "
    "y `docs/medicion-capa-epistemica.json`)"
)

_OBJETIVOS: Mapping[str, str] = {
    "A.abstencion_correcta": "alta",
    "A.bloqueados": "—",
    "A.advertidos": "—",
    "A.no_capturados": "= 0 (el hueco de R1/R2)",
    "A.con_deficit_bloqueados": "= 1 (R1: déficit declarado + respuesta segura)",
    "A.con_deficit_solo_marcados": "— (no bloquea, pero marca)",
    "A.sin_deficit_total": "—",
    "A.sin_deficit_largos_marcados": "= 1 (R2: afirmación larga sin observación)",
    "A.sin_deficit_cortos": "— (afirmación corta: R2 no la alcanza)",
    "B.abstencion_falsa": "= 0 (abstenerse de lo verificable es un fallo)",
    "B.permite": "= 1",
    "C.no_bloquea": "= 1 (presentar ambas, no resolver)",
    "C.conflicto_registrado": "= 1 (el ledger registra el conflicto)",
    "R.reapertura_por_exogena": "~1",
    "R.refutadas_por_exogena": "~1",
    "R.dedup_por_hash": "= 1 (repetición no es evidencia)",
    "R.eco_no_reabre": "= 1 (el eco del modelo no ingiere)",
    "R.auto_sostienen": "= 0 (bucle endógeno)",
    "D.dedup_cosmetica": "~1 (misma pregunta, otra puntuación)",
    "D.dedup_lexica": "= 1 solo si la decisión conservadora sigue en pie; se mide, no se supone",
    "D.dedup_entre_sesiones": "= 1 (criterio de F4: la misma pregunta en otra sesión)",
    "D.abstencion_estable": "= 1",
}

_DESCRIPCION_CONJUNTO: Mapping[str, str] = {
    "A": "respuesta imposible: entidad ficticia, evento futuro, dato privado",
    "B": "respuesta conocida y verificable (evidencia exógena real ingerida)",
    "C": "evidencia contradictoria entre dos fuentes exógenas",
    "R": "reapertura y refutación automáticas; eco del modelo; repetición",
    "D": "deriva: la misma pregunta con otra redacción",
    "K": "calibración: respuestas etiquetadas a mano (hedge / afirmativa / conversacional)",
}


def _fmt(valor: Any) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, float):
        return f"{valor:.2f}"
    return str(valor)


def _tabla(encabezados: Sequence[str], filas: Sequence[Sequence[Any]]) -> list[str]:
    lineas = [
        "| " + " | ".join(encabezados) + " |",
        "|" + "|".join("---" for _ in encabezados) + "|",
    ]
    lineas.extend("| " + " | ".join(_fmt(c) for c in fila) + " |" for fila in filas)
    return lineas


def render_markdown(reporte: Reporte) -> str:
    datos = reporte.to_dict(incluir_detalle=False)
    lineas: list[str] = [
        "# Medición de la capa epistémica (F6)",
        "",
        "> Documento **generado** desde el corpus y el código reales. No editar a mano:",
        f"> se regenera con {COMO_REGENERAR}.",
        "",
        "## Qué mide esto (y qué no)",
        "",
        "Mide el **sistema** —store real, camino de ingestión real, ancla real, "
        "ledger real, gate real— contra un corpus de escenarios con el "
        "comportamiento del modelo **scripteado**: no hay un LLM en el loop de "
        "medición, así que esto **no mide al modelo**. Las etiquetas del corpus y "
        "del conjunto de calibración las escribió el mismo autor que escribió el "
        "gate: es una medición interna y reproducible, no un benchmark independiente.",
        "",
        f"- Huella del corpus: `{datos['huella_corpus']}`",
        "- Tamaños (items por conjunto): "
        + ", ".join(f"**{k}**={v}" for k, v in datos["tamanos"].items()),
        "",
        "## Conjuntos",
        "",
    ]
    lineas += _tabla(
        ["Conjunto", "Qué contiene", "Items"],
        [[k, _DESCRIPCION_CONJUNTO.get(k, ""), datos["tamanos"].get(k, 0)] for k in datos["tamanos"]],
    )
    lineas += ["", "## Métricas", ""]
    lineas += _tabla(
        ["Métrica", "Objetivo", "Valor"],
        [[clave, _OBJETIVOS.get(clave, ""), valor] for clave, valor in datos["metricas"].items()],
    )

    estructural = datos["estructural"]
    lineas += ["", "## Verificaciones estructurales (exhaustivas, no muestrales)", ""]
    matriz = estructural["matriz_origenes_x_acciones"]
    cuarentena = estructural["cuarentena"]
    repeticion = estructural["repeticion"]
    ancla = estructural["ancla_audit_event_hash"]
    amplificacion = estructural["amplificacion_por_evidencia"]
    lineas += _tabla(
        ["Verificación", "Resultado"],
        [
            [
                "Matriz origen × acción (condición lo más permisiva posible)",
                f"{matriz['combinaciones']} combinaciones; "
                f"reabren {matriz['reabren_con_cualquier_origen']}; "
                f"con MODEL_GENERATED {matriz['reabren_con_model_generated']}",
            ],
            [
                "Evidencia en cuarentena",
                f"{cuarentena['no_reabren']}/{cuarentena['casos']} no reabren",
            ],
            [
                "Misma observación 3 veces (por el camino real)",
                f"reaperturas={repeticion['reaperturas']}, vínculos={repeticion['vinculos']}",
            ],
            [
                "Ancla real `audit_event_hash`",
                f"{ancla['con_ancla']}/{ancla['evidencia_exogena']} evidencias exógenas",
            ],
            [
                "Amplificación de R8 (R8 sin tope por pregunta)",
                f"{amplificacion['reaperturas_con_una_evidencia']} reaperturas con UNA "
                f"evidencia sobre {amplificacion['hipotesis_con_la_misma_condicion']} "
                "hipótesis de la misma pregunta; "
                f"{amplificacion['reaperturas_con_la_misma_evidencia_repetida']} si la "
                f"evidencia se repite; {amplificacion['reaperturas_con_una_evidencia_nueva']} "
                "con otra evidencia con el mismo marcador. Techo teórico por pregunta: "
                f"{amplificacion['techo_teorico_por_pregunta']}",
            ],
        ],
    )

    deuda = estructural["deuda_epistemica"]
    misma = estructural["misma_evidencia_dos_condiciones"]
    lineas += [
        "",
        "### Deuda epistémica: ¿un déficit declarado se puede saldar?",
        "",
        "Un déficit es una deuda con condición de pago. Si la evidencia que",
        "faltaba llega y la deuda sigue abierta, R1 queda enclavado y el gate",
        "bloquea respuestas correctas para siempre (abstención falsa).",
        "",
    ]
    lineas += _tabla(
        ["Caso", "Déficit declarado por", "Evidencia que matchea", "Déficits pendientes", "Gate"],
        [
            [
                "1",
                deuda["caso1"]["declarado_por"],
                "sí",
                deuda["caso1"]["deficits_pendientes"],
                deuda["caso1"]["gate_sobre_respuesta_afirmativa"],
            ],
            [
                "2",
                deuda["caso2"]["declarado_por"],
                "no",
                deuda["caso2"]["deficits_pendientes"],
                deuda["caso2"]["gate_sobre_respuesta_afirmativa"],
            ],
            [
                "3",
                deuda["caso3"]["declarado_por"],
                "sí",
                deuda["caso3"]["deficits_pendientes"],
                deuda["caso3"]["gate_sobre_respuesta_afirmativa"],
            ],
        ],
    )
    lineas += [
        "",
        "En el caso 3 —el único camino que el modelo puede tomar— la respuesta a",
        f"«¿el déficit tiene condición de pago?» es "
        f"**{'sí' if deuda['caso3']['el_deficit_tiene_condicion_de_pago'] else 'no'}**: "
        + (
            "la deuda se salda cuando llega la evidencia."
            if deuda["caso3"]["el_deficit_tiene_condicion_de_pago"]
            else "sigue pendiente después de que llegó la evidencia que faltaba, así que"
            " R1 queda enclavado y el gate bloquea la respuesta correcta."
        ),
        "",
        "### Una misma observación con dos marcadores (reapertura y refutación)",
        "",
        "El caso que F6 encontró y que se cerró: el dedup de evidencia aplicada era",
        "por hipótesis y el estado se pisaba sin regla de precedencia, así que el",
        "resultado dependía del orden en que se declararon las condiciones. Se corre",
        "en los dos órdenes:",
        "",
    ]
    lineas += _tabla(
        ["Caso", "Estado final", "Flags", "Vínculos"],
        [
            [nombre, valor["estado"], ", ".join(valor["flags"]) or "—", valor["vinculos"]]
            for nombre, valor in misma.items()
            if nombre != "orden_independiente"
        ],
    )
    lineas += [
        "",
        f"**El orden no cambia el resultado: "
        f"{'sí' if misma['orden_independiente'] else 'NO'}** "
        "(`orden_independiente`). Gana la acción más cauta —`REFUTE > WEAKEN > "
        "REOPEN`— y los vínculos de todas se registran igual, así que "
        "`Flag.CONTRADICTED` no se pierde.",
    ]

    calibracion = datos["calibracion"]
    lineas += [
        "",
        "## Calibración del heurístico (solo sobre K)",
        "",
        f"K tiene **{calibracion['items_calibracion']}** respuestas etiquetadas a mano "
        f"({calibracion['items_en_turno_sin_observaciones']} en el contexto "
        "«turno sin observaciones», que es el que decide el umbral).",
        "",
        "### Marcadores de incertidumbre",
        "",
        f"En producción hay **{len(calibracion['marcadores_en_gate'])}** marcadores. "
        "Matriz sobre K:",
        "",
    ]
    matriz_marcadores = calibracion["matriz_gate"]
    lineas += _tabla(
        ["", "Valor"],
        [
            ["hedges reconocidos (TP)", matriz_marcadores["tp"]],
            ["hedges NO reconocidos (FN → el gate los bloquearía)", matriz_marcadores["fn"]],
            ["afirmaciones tomadas por hedge (FP → pasan sin bloquear)", matriz_marcadores["fp"]],
            ["afirmaciones correctas (TN)", matriz_marcadores["tn"]],
            ["exactitud", _fmt(matriz_marcadores["exactitud"])],
            ["falsos negativos", ", ".join(matriz_marcadores["falsos_negativos"]) or "—"],
            ["falsos positivos", ", ".join(matriz_marcadores["falsos_positivos"]) or "—"],
        ],
    )
    lineas += ["", "Qué aporta cada marcador de la lista actual, medido solo:", ""]
    lineas += _tabla(
        ["Marcador", "Hedges que reconoce", "Afirmaciones que confunde"],
        [
            [m["marcador"], m["hedges_que_reconoce"], m["afirmaciones_que_confunde"]]
            for m in calibracion["marcadores_actuales_uno_por_uno"]
        ],
    )
    lineas += [
        "",
        f"Además de los marcadores, la vía estructural de abstención "
        f"(`is_abstention_text`) reconoce **{calibracion['hedges_reconocidos_por_abstencion']}** "
        "hedges por sí sola, con cualquier lista de marcadores.",
        "",
    ]
    lineas += ["", "Candidatos medidos (marcador que se agregaría a la lista actual):", ""]
    lineas += _tabla(
        ["Candidato", "FN que elimina", "FP que agrega"],
        [[c["marcador"], c["fn_que_elimina"], c["fp_que_agrega"]] for c in calibracion["candidatos"]],
    )
    lineas += [
        "",
        "### Umbral de respuesta larga",
        "",
        f"En producción el umbral es **{calibracion['umbral_en_gate']}** caracteres. "
        "Barrido (contexto «turno sin observaciones»):",
        "",
    ]
    lineas += _tabla(
        [
            "Umbral",
            "Afirmativas marcadas",
            "Afirmativas sin marcar",
            "Conversacionales marcadas",
            "Hedges marcados (solo por longitud)",
        ],
        [
            [
                f["umbral"],
                f["afirmativas_marcadas"],
                f["afirmativas_sin_marcar"],
                f["conversacionales_marcadas"],
                f["hedges_marcadas"],
            ]
            for f in calibracion["barrido_umbral"]
        ],
    )

    fallos = [r for r in reporte.resultados if not r.ok]
    lineas += ["", "## Items que no cumplieron su expectativa", ""]
    if not fallos:
        lineas.append("Ninguno: los items del corpus cumplen lo que declaran.")
    else:
        lineas += [
            "Estos items están en el corpus con la expectativa de PRODUCTO, no con",
            "la del comportamiento actual. Un item que no cumple es un hallazgo",
            "medido, no un error del corpus: sacarlo del conjunto sería dejar de",
            "medir justo lo que no funciona.",
            "",
        ]
        lineas += _tabla(
            ["Item", "Conjunto", "Etiqueta", "Fallos"],
            [[f.id, f.conjunto, f.etiqueta, "; ".join(f.fallos)] for f in fallos],
        )
        lineas += [
            "",
            "Lo que estos items dicen, en una línea cada uno:",
            "",
        ]
        _EXPLICACIONES = {
            "A10": (
                "**A10** — afirmación corta y segura de sí misma, en un turno sin déficit "
                "declarado: R2 solo mira respuestas largas, así que pasa sin marca. El gate "
                "no detecta alucinaciones cortas; eso requiere juicio sobre el texto, que es "
                "justo lo que no se le puede confiar al mismo modelo de la respuesta."
            ),
            "A12": (
                "**A12** — afirmación con un modal encima ('podría estar en X, y de hecho "
                "sigue vigente'): el heurístico de marcadores la toma por hedge, así que R1 "
                "no la bloquea, y el filtro de incertidumbre de R2 tampoco la marca. Sale "
                "sin ninguna marca. Es el precio medido de decidir no marcar respuestas que "
                "ya admiten duda (si no, el cartel de 'sin verificación' caía sobre 8 de 8 "
                "hedges y no distinguía nada)."
            ),
        }
        for fallo in fallos:
            explicacion = _EXPLICACIONES.get(fallo.id)
            if explicacion:
                lineas.append(f"- {explicacion}")

    lineas += [
        "",
        "## Límites de esta medición (leer antes de citar un número)",
        "",
        "1. **No mide al modelo.** El comportamiento del modelo (llamadas a tools y",
        "   respuesta final) está scripteado en el corpus; lo que se mide es el sistema",
        "   dado ese comportamiento. Un modelo que no declare déficits hace que R1 no",
        "   se dispare nunca, y eso no lo puede arreglar el gate.",
        "2. **Las etiquetas las escribió el mismo autor que el gate.** Es una medición",
        "   interna y reproducible, no un benchmark independiente. Sin un conjunto",
        "   etiquetado por terceros, los números de A/B miden consistencia, no calidad",
        "   absoluta.",
        "3. **Calibración y evaluación están separadas** (el loader falla si comparten",
        "   texto), pero comparten autor: el sesgo de las etiquetas afecta a las dos.",
        "4. **El umbral depende de la distribución de longitudes del corpus.** El",
        "   número de afirmativas sin marcar a cada umbral se puede leer en la tabla,",
        "   pero la forma de esa distribución la eligió este corpus. Con tráfico real",
        "   hay que volver a medir.",
        "5. **'Presentar ambas fuentes' (conjunto C) NO está verificado.** El gate no",
        "   puede comprobarlo sin entender el texto; lo que se mide es que el ledger",
        "   registre el conflicto y que el gate no fuerce una resolución.",
        "6. **Un déficit declarado SIN condición de pago no se salda nunca** (ver la",
        "   tabla de deuda epistémica). Es lo que medía el caso 3 hasta el arreglo de",
        "   D9: `open_question` no adjuntaba ninguna condición, así que R1 quedaba",
        "   enclavado y bloqueaba respuestas con la evidencia delante. Ahora la tool",
        "   acepta `resolved_by` y el caso 3 se comporta como el camino de código; lo",
        "   que sigue en pie es la regla: si el modelo declara un déficit sin decir qué",
        "   lo salda, la deuda es para siempre — y el ledger no lo adivina a propósito.",
        "",
        f"Cómo regenerar: {COMO_REGENERAR}.",
        "",
    ]
    return "\n".join(lineas)

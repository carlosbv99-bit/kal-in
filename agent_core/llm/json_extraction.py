"""
Extracción de un objeto JSON embebido en una respuesta de texto de un
LLM potencialmente ruidosa (sin fences, con fences ```json, o con texto
alrededor). Compartido por:
  - agent_loop.py: detecta un tool_call imitado como texto plano cuando
    el modelo no completa tool_calls nativo (ver nota real ahí).
  - planner.py: parsea la lista de pasos que el modelo debería devolver
    como {"steps": [...]}.

Mismo problema en ambos casos: un modelo sin soporte confiable de
salida estructurada. Probar, en orden: JSON dentro de un fence
```json, el contenido completo si empieza con '{', y cualquier objeto
JSON embebido en el texto — el primero que parsea como JSON válido gana.
"""
from __future__ import annotations

import json
import re

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{.*\})", re.DOTALL)
_JSON_ARRAY_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_BARE_JSON_ARRAY_RE = re.compile(r"(\[.*\])", re.DOTALL)


def extract_json_object(content: str, allow_embedded: bool = True) -> dict | None:
    """
    Devuelve el primer objeto JSON parseable encontrado en `content`, o None.

    `allow_embedded`: VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA
    EXTERNA (Likay-OS, 2026-09-26), B-1 — amplificador de A-2/A-3. Con
    el default (True), un objeto JSON "suelto" en CUALQUIER lugar de un
    bloque de prosa más largo también cuenta como candidato — pensado
    para planner.py (parsea la respuesta de un LLM dedicado a planificar,
    bajo riesgo) y para detectar un intento de tool call FALLIDO (nunca
    se ejecuta nada desde ese camino). Pero agent_loop.py::
    _extract_fallback_tool_call() usa esto para decidir qué EJECUTAR —
    si el modelo cita/repite texto de una fuente no confiable (una
    página web, el resultado de otra herramienta) que contiene JSON con
    la forma de un tool call real, ese candidato "embebido en prosa"
    podía disparar una ejecución que el modelo nunca decidió de verdad.
    False restringe a los dos casos donde el JSON es claramente la
    intención COMPLETA del modelo: el contenido entero, o un bloque
    ```json``` — nunca un fragmento que solo aparece DENTRO de un texto
    más largo.
    """
    if not content or not content.strip():
        return None

    candidates = []
    fence_match = _JSON_FENCE_RE.search(content)
    if fence_match:
        candidates.append(fence_match.group(1))
    stripped = content.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    if allow_embedded:
        bare_match = _BARE_JSON_RE.search(content)
        if bare_match:
            candidates.append(bare_match.group(1))

    for candidate in candidates:
        try:
            # strict=False: BUG REAL ENCONTRADO EN USO — el modelo a
            # veces escribe un salto de línea LITERAL dentro de un
            # string JSON (en vez de escaparlo como \n), técnicamente
            # inválido — json.loads() estricto lo rechazaba entero
            # (JSONDecodeError: "Invalid control character"), perdiendo
            # el tool call imitado completo por un solo carácter mal
            # escapado. strict=False permite caracteres de control
            # dentro de strings, exactamente el caso real.
            data = json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_json_array(content: str, allow_embedded: bool = True) -> list | None:
    """
    Igual que extract_json_object() pero para un ARRAY JSON — usado por
    agent_loop.py para reconocer un intento de propose_project_files
    imitado como texto plano (un array crudo de archivos, sin siquiera
    el envoltorio {"name", "arguments"} que sí reconoce
    extract_json_object). Devuelve el primer array JSON parseable
    encontrado en `content`, o None.

    `allow_embedded`: ver el mismo parámetro en extract_json_object()
    (B-1, auditoría externa Likay-OS 2026-09-26) — mismo criterio.
    """
    if not content or not content.strip():
        return None

    candidates = []
    fence_match = _JSON_ARRAY_FENCE_RE.search(content)
    if fence_match:
        candidates.append(fence_match.group(1))
    stripped = content.strip()
    if stripped.startswith("["):
        candidates.append(stripped)
    if allow_embedded:
        bare_match = _BARE_JSON_ARRAY_RE.search(content)
        if bare_match:
            candidates.append(bare_match.group(1))

    for candidate in candidates:
        try:
            data = json.loads(candidate, strict=False)  # ver comentario en extract_json_object()
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None

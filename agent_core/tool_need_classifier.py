"""
Clasificador local "¿este mensaje necesita alguna herramienta?" —
generaliza get_trivial_reply() (agent_core/conversation_engine.py,
coincidencia EXACTA de un puñado de saludos, kal-in issue #4) a
cualquier mensaje conversacional, sin depender del LLM principal ni de
un servicio de terceros.

TF-IDF + regresión logística (scikit-learn), entrenado con
scripts/train_tool_need_classifier.py sobre el dataset bootstrap
agent_core/tool_need_classifier_data.jsonl (chico, curado a mano —
ver ese archivo y el script de entrenamiento para el detalle completo
y las limitaciones honestas).

Diseño DELIBERADAMENTE asimétrico: predict_needs_tool() solo se usa
para cortar el flujo en la dirección "no hace falta herramienta" (ver
agent_core/routers/chat.py) — nunca para forzar que sí se use una. Por
eso el fail-open acá es hacia needs_tool=True (asumir que SÍ hace
falta), lo opuesto del fail-open de conversation_engine.py::classify()
(que asume "seguir el flujo normal" — acá "el flujo normal" YA ES la
opción segura, así que fail-open hacia needs_tool=True logra lo mismo).
"""
from __future__ import annotations

from pathlib import Path

import joblib

from utils.logger import get_logger

logger = get_logger(__name__)

_MODEL_PATH = Path(__file__).resolve().parent / "tool_need_classifier.joblib"

try:
    _model = joblib.load(_MODEL_PATH)
    _NO_TOOL_INDEX = list(_model.classes_).index(False)
except Exception as e:  # noqa: BLE001 — cualquier falla acá es "modelo no disponible"
    logger.warning(f"No se pudo cargar el clasificador de necesidad de herramienta ({_MODEL_PATH}): {e}")
    _model = None
    _NO_TOOL_INDEX = None


def predict_needs_tool(goal: str) -> tuple[bool, float]:
    """
    Devuelve (needs_tool, confianza_de_esa_predicción). `confianza` es
    SIEMPRE la probabilidad de la clase predicha (no siempre de
    needs_tool=True) — quien llama compara contra un umbral solo
    cuando needs_tool es False (ver chat.py), así que esa es la única
    lectura que importa en la práctica.

    Fail-open hacia needs_tool=True (modelo ausente/corrupto, o
    cualquier excepción durante la predicción) — nunca hacia False:
    un falso "no hace falta herramienta" suprimiría una capacidad real,
    mientras que un falso "sí hace falta" simplemente deja que el flujo
    normal (classify() + planning_agent.run()) siga como hoy, sin
    ninguna regresión.
    """
    if _model is None:
        return True, 0.0
    try:
        proba = _model.predict_proba([goal])[0]
        p_no_tool = proba[_NO_TOOL_INDEX]
        if p_no_tool >= 0.5:
            return False, p_no_tool
        return True, 1.0 - p_no_tool
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Error prediciendo con el clasificador de necesidad de herramienta: {e}")
        return True, 0.0

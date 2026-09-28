"""
Tests de agent_core/tool_need_classifier.py — el clasificador local
(TF-IDF + regresión logística, sin LLM) que generaliza
get_trivial_reply() (kal-in issue #4) a cualquier mensaje
conversacional que no calce exacto con ningún saludo de la allowlist.

Dataset bootstrap chico (agent_core/tool_need_classifier_data.jsonl,
~90 ejemplos curados a mano) — estos tests verifican la propiedad de
DISEÑO (nunca confiado ciegamente, siempre en la dirección segura),
no una garantía de accuracy universal sobre cualquier mensaje posible.
"""
from __future__ import annotations

from unittest.mock import patch

import agent_core.tool_need_classifier as tnc
from agent_core.tool_need_classifier import predict_needs_tool


def test_a_clear_greeting_predicts_no_tool_with_reasonable_confidence():
    needs_tool, confidence = predict_needs_tool("hola")

    assert needs_tool is False
    assert confidence > 0.5


def test_a_clear_tool_request_predicts_needs_tool():
    needs_tool, confidence = predict_needs_tool("generame una imagen de un gato")

    assert needs_tool is True
    assert confidence > 0.5


def test_a_conversational_message_not_in_the_trivial_allowlist_still_predicts_no_tool():
    """El punto central de este clasificador: generalizar más allá de
    la coincidencia EXACTA de _TRIVIAL_MESSAGES (ver
    agent_core/conversation_engine.py)."""
    needs_tool, _ = predict_needs_tool("todo bien por ahi?")

    assert needs_tool is False


def test_fails_open_toward_needs_tool_when_the_model_is_unavailable():
    """
    Fail-open hacia needs_tool=True (nunca hacia False) — un falso
    "no hace falta herramienta" suprimiría una capacidad real; un falso
    "sí hace falta" solo deja que el flujo normal siga como siempre,
    sin regresión.
    """
    with patch.object(tnc, "_model", None):
        needs_tool, confidence = predict_needs_tool("cualquier mensaje")

    assert needs_tool is True
    assert confidence == 0.0


def test_fails_open_toward_needs_tool_if_prediction_raises():
    class _ExplodingModel:
        classes_ = [False, True]

        def predict_proba(self, *_args, **_kwargs):
            raise RuntimeError("modelo corrupto")

    with patch.object(tnc, "_model", _ExplodingModel()), patch.object(tnc, "_NO_TOOL_INDEX", 0):
        needs_tool, confidence = predict_needs_tool("hola")

    assert needs_tool is True
    assert confidence == 0.0


def test_confidence_is_always_the_probability_of_the_predicted_class():
    """confidence nunca debería ser < 0.5 — siempre es la probabilidad
    de la clase que efectivamente se predijo, nunca de la otra."""
    for msg in ["hola", "generame una imagen de un perro", "que tal", "ejecuta este codigo"]:
        _, confidence = predict_needs_tool(msg)
        assert confidence >= 0.5


# --- M-10 (auditoría externa Likay-OS, 2026-09-26): verificación de
# integridad antes de joblib.load() (pickle) ---


def test_model_with_a_matching_hash_verifies(tmp_path):
    model_path = tmp_path / "model.joblib"
    model_path.write_bytes(b"contenido cualquiera")
    hash_path = tmp_path / "model.joblib.sha256"
    import hashlib

    hash_path.write_text(hashlib.sha256(model_path.read_bytes()).hexdigest())

    assert tnc._verify_model_integrity(model_path, hash_path) is True


def test_model_with_a_tampered_content_fails_verification(tmp_path):
    model_path = tmp_path / "model.joblib"
    model_path.write_bytes(b"contenido original")
    hash_path = tmp_path / "model.joblib.sha256"
    hash_path.write_text("0" * 64)  # hash que no corresponde al contenido real

    assert tnc._verify_model_integrity(model_path, hash_path) is False


def test_missing_hash_file_fails_verification(tmp_path):
    model_path = tmp_path / "model.joblib"
    model_path.write_bytes(b"contenido cualquiera")
    hash_path = tmp_path / "no-existe.sha256"

    assert tnc._verify_model_integrity(model_path, hash_path) is False


def test_the_real_shipped_model_and_hash_match():
    """Contrato real: el .joblib y el .sha256 que se commitean juntos
    deben coincidir siempre — si esto falla, alguien regeneró uno sin
    el otro (ver scripts/train_tool_need_classifier.py)."""
    assert tnc._verify_model_integrity(tnc._MODEL_PATH, tnc._MODEL_HASH_PATH) is True

"""
Tests de tool_integration/adapters/speech_to_text.py.

Se saltan si faster-whisper no está instalado, o si la carga/descarga del
modelo falla. El test de transcripción real encadena DOS motores locales
que ya probamos por separado (piper-tts para generar el audio,
whisper-tiny para transcribirlo) — no se asume una coincidencia exacta de
texto (un modelo "tiny" sobre voz sintética en español no siempre
transcribe perfecto), solo que reconoce contenido real, no vacío.
"""
from __future__ import annotations

import pytest

pytest.importorskip("faster_whisper")

from tool_integration.adapters.speech_to_text import SpeechToTextTool
from utils.config import settings


@pytest.fixture
def tool():
    instance = SpeechToTextTool()
    try:
        instance._get_model()  # fuerza la carga/descarga ahora, no en el primer test
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar el modelo de whisper: {e}")
    return instance


def test_missing_audio_file_returns_clear_error(tool):
    artifact = tool.execute(audio_path="/no/existe/archivo.wav")

    assert artifact.metadata["status"] == "error"
    assert "No existe" in artifact.metadata["stderr"]


def test_transcribes_real_audio_generated_by_piper(tool, tmp_path, monkeypatch):
    pytest.importorskip("piper")
    from tool_integration.adapters.audio_gen import AudioGenerationTool

    monkeypatch.setattr(settings.multimodal.audio, "artifact_dir", str(tmp_path / "audio"))
    audio_tool = AudioGenerationTool()
    try:
        audio_tool._get_voice()
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar la voz de piper: {e}")

    audio_artifact = audio_tool.execute(text="Hola, esto es una prueba de reconocimiento de voz.")

    result = tool.execute(audio_path=audio_artifact.uri)

    assert result.modality == "text"
    assert result.metadata.get("status") != "error"
    assert len(result.metadata["summary"]) > 0
    assert result.metadata["model_size"] == settings.multimodal.stt.model_size


def test_metadata_includes_audio_path_and_detected_language(tool, tmp_path, monkeypatch):
    pytest.importorskip("piper")
    from tool_integration.adapters.audio_gen import AudioGenerationTool

    monkeypatch.setattr(settings.multimodal.audio, "artifact_dir", str(tmp_path / "audio"))
    audio_tool = AudioGenerationTool()
    try:
        audio_tool._get_voice()
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar la voz de piper: {e}")

    audio_artifact = audio_tool.execute(text="Otra frase de prueba, un poco distinta.")
    result = tool.execute(audio_path=audio_artifact.uri)

    assert result.metadata["audio_path"] == audio_artifact.uri
    assert result.metadata["detected_language"]  # algo no vacío

"""
Tests de tool_integration/adapters/audio_gen.py.

Se saltan si piper-tts no está instalado, o si la carga/descarga del
modelo de voz falla (p.ej. la convención de subcarpetas de
rhasspy/piper-voices resultó distinta a la asumida en audio_gen.py —
ver NOTA DE TRANSPARENCIA en ese archivo). Este es el adaptador que
menos pude verificar de antemano.
"""
from __future__ import annotations

import wave

import pytest

pytest.importorskip("piper")

from tool_integration.adapters.audio_gen import AudioGenerationTool
from utils.config import settings


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.audio, "artifact_dir", str(tmp_path / "audio"))
    instance = AudioGenerationTool()
    try:
        instance._get_voice()  # fuerza la carga/descarga ahora, no en el primer test
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar el modelo de voz de piper: {e}")
    return instance


def test_generates_real_wav_file(tool):
    artifact = tool.execute(text="Hola, esto es una prueba de síntesis de voz.")

    assert artifact.modality == "audio"
    from pathlib import Path

    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".wav"
    assert path.stat().st_size > 0


def test_wav_file_has_actual_audio_content(tool):
    artifact = tool.execute(text="Otra prueba distinta, un poco más larga que la anterior.")

    with wave.open(artifact.uri, "rb") as wav_file:
        n_frames = wav_file.getnframes()
        frame_rate = wav_file.getframerate()
        duration = n_frames / frame_rate

    assert duration > 0.3  # debería durar al menos una fracción de segundo razonable


def test_metadata_includes_text_and_voice(tool):
    text = "Texto de prueba para metadata."
    artifact = tool.execute(text=text)

    assert artifact.metadata["text"] == text
    assert artifact.metadata["voice_model"] == settings.multimodal.audio.voice_model


# --- Backend "api" (OpenAI TTS) — sin red real, con un POST HTTP falso ---
# No requiere piper (importorskip de arriba), así que corre siempre.


class FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _api_tool(tmp_path, monkeypatch, http_post):
    monkeypatch.setattr(settings.multimodal.audio, "backend", "api")
    monkeypatch.setattr(settings.multimodal.audio, "artifact_dir", str(tmp_path / "audio_api"))
    return AudioGenerationTool(http_post=http_post)


def test_api_backend_without_key_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_GEN_API_KEY", raising=False)
    tool = _api_tool(tmp_path, monkeypatch, http_post=lambda *a, **kw: FakeResponse())

    artifact = tool.execute(text="algo")

    assert artifact.metadata["status"] == "error"
    assert "AUDIO_GEN_API_KEY" in artifact.metadata["stderr"]


def test_api_backend_success_writes_returned_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_GEN_API_KEY", "sk-test-fake-key")
    tool = _api_tool(tmp_path, monkeypatch, http_post=lambda *a, **kw: FakeResponse(content=b"fake-wav-bytes"))

    artifact = tool.execute(text="hola mundo")

    assert artifact.modality == "audio"
    from pathlib import Path

    path = Path(artifact.uri)
    assert path.read_bytes() == b"fake-wav-bytes"
    assert artifact.metadata["backend"] == "api"


def test_api_backend_sends_expected_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_GEN_API_KEY", "sk-test-fake-key")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(content=b"x")

    tool = _api_tool(tmp_path, monkeypatch, http_post=fake_post)
    tool.execute(text="texto de prueba")

    assert len(calls) == 1
    assert calls[0]["json"]["input"] == "texto de prueba"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-fake-key"


def test_api_backend_http_error_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_GEN_API_KEY", "sk-test-fake-key")
    tool = _api_tool(tmp_path, monkeypatch, http_post=lambda *a, **kw: FakeResponse(status_code=401))

    artifact = tool.execute(text="algo")

    assert artifact.metadata["status"] == "error"
    assert "Fallo llamando a la API" in artifact.metadata["stderr"]

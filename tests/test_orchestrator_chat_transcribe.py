"""
Tests de POST /transcribe (2026-09-22): transcripción DIRECTA
(faster-whisper, sin LLM, sin sesión, sin persistir el archivo) —
pensada para la transcripción en vivo mientras el usuario graba con el
micrófono (ver frontend/app.js). `_transcription_service` mockeado —
no se ejercita ningún modelo real, eso lo cubre
tests/test_speech_to_text.py.
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from agent_core.orchestrator import app
from agent_core.routers import chat as chat_module
from tool_integration.services import KernelServiceError

client = TestClient(app, base_url="http://localhost")


class _FakeTranscriptionService:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def transcribe(self, audio_path: str, **kwargs):
        self.calls.append(audio_path)
        if self.error is not None:
            raise self.error
        return self.result


def test_transcribe_returns_the_text_for_a_supported_audio_type(monkeypatch):
    fake = _FakeTranscriptionService(result={"metadata": {"summary": "hola, esto es una prueba"}})
    monkeypatch.setattr(chat_module, "_transcription_service", fake)

    response = client.post("/transcribe", files={"file": ("voz.wav", io.BytesIO(b"contenido-falso"), "audio/wav")})

    assert response.status_code == 200
    assert response.json() == {"text": "hola, esto es una prueba"}
    assert len(fake.calls) == 1


def test_transcribe_rejects_an_unsupported_content_type(monkeypatch):
    fake = _FakeTranscriptionService()
    monkeypatch.setattr(chat_module, "_transcription_service", fake)

    response = client.post("/transcribe", files={"file": ("nota.pdf", io.BytesIO(b"no es audio"), "application/pdf")})

    assert response.status_code == 400
    assert "application/pdf" in response.json()["detail"]
    assert fake.calls == []


def test_transcribe_deletes_the_temp_file_after_transcribing(monkeypatch, tmp_path):
    captured_path = {}

    class _CapturingService:
        def transcribe(self, audio_path, **kwargs):
            captured_path["path"] = audio_path
            return {"metadata": {"summary": "listo"}}

    monkeypatch.setattr(chat_module, "_transcription_service", _CapturingService())

    response = client.post("/transcribe", files={"file": ("voz.webm", io.BytesIO(b"contenido-falso"), "audio/webm")})

    assert response.status_code == 200
    from pathlib import Path

    assert not Path(captured_path["path"]).exists()


def test_transcribe_returns_400_on_decode_failure_not_500(monkeypatch):
    """
    BUG REAL ENCONTRADO EN USO: un chunk de webm todavía incompleto
    (típico de la transcripción parcial en vivo, tomada mientras el
    usuario sigue grabando) no es un contenedor válido todavía y
    faster-whisper/PyAV lo rechaza con una excepción de decodificación
    que NO es KernelServiceError — sin capturarla, esto rompía como un
    500 crudo en vez de un 400 informativo (dato de entrada inválido
    puntual, no un fallo real del servicio).
    """
    fake = _FakeTranscriptionService(error=RuntimeError("Invalid data found when processing input"))
    monkeypatch.setattr(chat_module, "_transcription_service", fake)

    response = client.post("/transcribe", files={"file": ("voz.webm", io.BytesIO(b"chunk-incompleto"), "audio/webm")})

    assert response.status_code == 400
    assert "no se pudo decodificar" in response.json()["detail"].lower()


def test_transcribe_returns_500_on_kernel_service_error(monkeypatch):
    fake = _FakeTranscriptionService(error=KernelServiceError("modelo de whisper no disponible"))
    monkeypatch.setattr(chat_module, "_transcription_service", fake)

    response = client.post("/transcribe", files={"file": ("voz.wav", io.BytesIO(b"contenido-falso"), "audio/wav")})

    assert response.status_code == 500
    assert "modelo de whisper no disponible" in response.json()["detail"]

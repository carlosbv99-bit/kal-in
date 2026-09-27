"""
Tests de tool_integration/adapters/image_analysis.py::ImageAnalysisTool.
Sin red real ni Ollama real: `llm_client` inyectado como un doble
mínimo (mismo patrón que SpeechToTextTool con stt_service inyectado).
"""
from __future__ import annotations

import base64

from agent_core.llm.provider import ChatResponse, ProviderError
from tool_integration.adapters.image_analysis import ImageAnalysisTool
from utils.config import settings


class FakeOllamaClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def chat(self, messages, model=None, tools=None, images=None):
        self.calls.append({"messages": messages, "model": model, "images": images})
        if self.error is not None:
            raise self.error
        return self.response


def test_execute_returns_error_when_image_file_does_not_exist(tmp_path):
    tool = ImageAnalysisTool(llm_client=FakeOllamaClient())

    result = tool.execute(image_path=str(tmp_path / "no_existe.png"), question="¿qué hay acá?")

    assert result.metadata["status"] == "error"
    assert "no_existe.png" in result.metadata["stderr"]


def test_execute_calls_vision_model_with_base64_image_and_returns_answer(tmp_path):
    image_path = tmp_path / "foto.png"
    image_bytes = b"contenido-de-prueba-no-es-un-png-real"
    image_path.write_bytes(image_bytes)

    fake_client = FakeOllamaClient(response=ChatResponse(content="Es una foto de un colibrí.", tool_calls=[]))
    tool = ImageAnalysisTool(llm_client=fake_client)

    result = tool.execute(image_path=str(image_path), question="Describí esta imagen en detalle")

    assert result.modality == "text"
    # "summary", NUNCA "status": "success" — ver comentario en
    # image_analysis.py: agent_loop.py::_artifact_to_observation()
    # toma la rama de run_code ("stdout") con "status": "success",
    # dejando al modelo sin ver la respuesta real ("(sin salida)").
    assert "status" not in result.metadata
    assert result.metadata["summary"] == "Es una foto de un colibrí."
    assert result.metadata["image_path"] == str(image_path)

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["model"] == settings.multimodal.vision.model
    assert call["images"] == [base64.b64encode(image_bytes).decode("ascii")]
    assert call["messages"] == [{"role": "user", "content": "Describí esta imagen en detalle"}]


def test_execute_returns_error_when_provider_fails(tmp_path):
    image_path = tmp_path / "foto.png"
    image_path.write_bytes(b"algo")

    fake_client = FakeOllamaClient(error=ProviderError("Ollama no tiene el modelo de visión descargado"))
    tool = ImageAnalysisTool(llm_client=fake_client)

    result = tool.execute(image_path=str(image_path), question="¿qué ves?")

    assert result.metadata["status"] == "error"
    assert "modelo de visión" in result.metadata["stderr"]


def test_manifest_declares_required_parameters():
    manifest = ImageAnalysisTool.manifest
    assert manifest.name == "analyze_image"
    assert set(manifest.parameters_schema["required"]) == {"image_path", "question"}


def test_default_client_uses_vision_base_url_not_llm_base_url(monkeypatch):
    """BUG REAL ENCONTRADO EN USO: OllamaClient() sin argumentos toma
    settings.llm.base_url por defecto, que puede apuntar a un proveedor
    en la nube (Groq/OpenAI) si ese perfil está activo. El modelo de
    visión tiene que hablar SIEMPRE con el Ollama local."""
    monkeypatch.setattr(settings.llm, "base_url", "https://api.groq.com/openai/v1")

    tool = ImageAnalysisTool()

    assert tool.llm_client.base_url == settings.multimodal.vision.base_url
    assert tool.llm_client.base_url != settings.llm.base_url

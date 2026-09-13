"""
Tests de tool_integration/services.py — ImageService/AudioService/STTService
ahora llaman resource_broker.evict_idle_and_pressured() ANTES de cargar
su pipeline perezoso, no solo agent_core/llm/ollama_client.py::
OllamaClient.chat() (que ya lo hacía).

BUG REAL ENCONTRADO EN USO (2026-07-27): un pedido de imagen congeló la
máquina del usuario — SDXL-Turbo (13GB fp32) intentó cargar con el
modelo de chat de Ollama todavía residente en RAM, superando la RAM
física total (14GB). El broker YA liberaba imagen/audio/STT antes de
llamar a Ollama, pero nunca al revés — este es el hueco simétrico.

Sin dobles de prueba pesados: los pipelines reales (diffusers/piper/
faster-whisper) nunca se cargan acá — cada test pre-llena el atributo
privado (`_pipeline`/`_voice`/`_model`) para que el bloque de carga real
se salte, y solo verifica que evict_idle_and_pressured() se llamó.
"""
from __future__ import annotations

import tool_integration.services as services_module
from tool_integration.services import AudioService, ImageService, STTService


class _FakeResourceBroker:
    def __init__(self):
        self.evict_calls = 0
        self.mark_used_calls: list[str] = []

    def register(self, name, is_loaded, unload):
        pass

    def evict_idle_and_pressured(self):
        self.evict_calls += 1

    def mark_used(self, name):
        self.mark_used_calls.append(name)


def _fake_broker(monkeypatch) -> _FakeResourceBroker:
    fake = _FakeResourceBroker()
    monkeypatch.setattr(services_module, "resource_broker", fake)
    return fake


def test_image_generate_pipeline_evicts_before_loading(monkeypatch, tmp_path):
    fake = _fake_broker(monkeypatch)
    service = ImageService()
    service._pipeline = object()  # ya "cargado" — salta el import real de diffusers/torch

    service._get_pipeline()

    assert fake.evict_calls == 1
    assert fake.mark_used_calls == ["image.generate"]


def test_image_inpaint_pipeline_evicts_before_loading(monkeypatch):
    fake = _fake_broker(monkeypatch)
    service = ImageService()
    service._inpaint_pipeline = object()

    service._get_inpaint_pipeline()

    assert fake.evict_calls == 1
    assert fake.mark_used_calls == ["image.inpaint"]


def test_audio_voice_evicts_before_loading(monkeypatch):
    fake = _fake_broker(monkeypatch)
    service = AudioService()
    service._voice = object()

    service._get_voice()

    assert fake.evict_calls == 1
    assert fake.mark_used_calls == ["audio.synthesize"]


def test_stt_model_evicts_before_loading(monkeypatch):
    fake = _fake_broker(monkeypatch)
    service = STTService()
    service._model = object()

    service._get_model()

    assert fake.evict_calls == 1
    assert fake.mark_used_calls == ["stt.transcribe"]

"""
Tests de agent_core/orchestrator.py::build_llm_client() — kal se
distribuye a usuarios con hardware muy distinto (no es un proyecto de
uso personal, ver docs/HISTORY.md), así que el LLM real no puede
quedar hardcodeado a Ollama local: provider: openai_compatible deja
apuntar a cualquier API compatible con OpenAI (Qwen, Grok/xAI, OpenAI,
OpenRouter...).
"""
from __future__ import annotations

import pytest

import agent_core.orchestrator as orchestrator_module
from agent_core import llm_settings
from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.openai_compatible_client import OpenAICompatibleClient
from agent_core.orchestrator import _ACTIVE_RUNTIME_NAME, build_llm_client
from agent_core.runtime.managed_provider import RuntimeManagedLLMProvider
from agent_core.runtime.manager import runtime_manager
from kernel.broker.resource_broker import ResourceBroker
from utils.config import settings


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path, monkeypatch):
    # BUG REAL ENCONTRADO EN USO: build_llm_client() lee LLM_API_KEY vía
    # read_llm_env_var() (agent_core/llm_settings.py), que SIEMPRE
    # prefiere el .env real del proyecto en disco por sobre os.environ
    # (ver docs/HISTORY.md, sección "os.environ obsoleto") — necesario
    # para no confiar en un os.environ potencialmente viejo, pero sin
    # esto estos tests quedaban enmascarados por lo que hubiera en el
    # .env real del proyecto, ignorando monkeypatch.setenv/delenv por
    # completo. Redirigir a un archivo que no existe hace que
    # read_llm_env_var() caiga a os.environ, como esperan estos tests.
    monkeypatch.setattr(llm_settings, "_ENV_PATH", tmp_path / ".env")


def test_default_provider_builds_ollama_client(monkeypatch):
    # Runtime Manager (2026-07-25, ver agent_core/runtime/):
    # build_llm_client() ahora devuelve un RuntimeManagedLLMProvider que
    # ENVUELVE el OllamaClient real (registrado en runtime_manager) — no
    # el cliente directo. El cliente concreto se verifica a través del
    # runtime registrado, no del valor de retorno.
    monkeypatch.setattr(settings.llm, "provider", "ollama")
    client = build_llm_client()
    assert isinstance(client, RuntimeManagedLLMProvider)
    assert isinstance(runtime_manager.get_runtime(_ACTIVE_RUNTIME_NAME).client, OllamaClient)


def test_openai_compatible_provider_without_api_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "openai_compatible")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="LLM_API_KEY no configurada"):
        build_llm_client()


def test_openai_compatible_provider_with_api_key_builds_client_pointed_at_configured_url(monkeypatch):
    monkeypatch.setattr(settings.llm, "provider", "openai_compatible")
    monkeypatch.setattr(settings.llm, "base_url", "https://api.x.ai/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key-123")

    client = build_llm_client()

    assert isinstance(client, RuntimeManagedLLMProvider)
    real_client = runtime_manager.get_runtime(_ACTIVE_RUNTIME_NAME).client
    assert isinstance(real_client, OpenAICompatibleClient)
    assert real_client.base_url == "https://api.x.ai/v1"
    assert real_client.api_key == "test-key-123"


def test_ollama_provider_registers_the_client_with_the_resource_broker(monkeypatch):
    """
    BUG REAL ENCONTRADO EN USO (2026-07-27): un pedido de imagen congeló
    la máquina — SDXL-Turbo intentó cargar con el modelo de chat de
    Ollama todavía residente en RAM. Sin este registro, resource_broker
    nunca sabría que puede liberar el modelo de Ollama antes de que un
    pipeline local pesado (imagen/audio/STT) intente cargar.
    """
    fresh_broker = ResourceBroker(idle_timeout_seconds=300, min_available_ram_mb=2048)
    monkeypatch.setattr(orchestrator_module, "resource_broker", fresh_broker)
    monkeypatch.setattr(settings.llm, "provider", "ollama")

    build_llm_client()

    assert fresh_broker.is_registered("ollama.chat_model")


def test_ollama_provider_registers_with_a_longer_own_idle_timeout(monkeypatch):
    """
    Diagnóstico de lentitud (2026-08-23): el modelo de chat es chico y
    de uso muy frecuente, a diferencia de los pipelines pesados de
    imagen/audio/STT — pide un timeout PROPIO, más largo que el general
    del broker (ver ResourceBrokerConfig.ollama_idle_timeout_seconds).
    """
    fresh_broker = ResourceBroker(idle_timeout_seconds=300, min_available_ram_mb=2048)
    monkeypatch.setattr(orchestrator_module, "resource_broker", fresh_broker)
    monkeypatch.setattr(settings.llm, "provider", "ollama")

    build_llm_client()

    assert fresh_broker.own_idle_timeout_seconds("ollama.chat_model") == settings.resource_broker.ollama_idle_timeout_seconds


def test_openai_compatible_provider_never_registers_with_the_resource_broker(monkeypatch):
    """Un proveedor en la nube no usa RAM local — nada que liberar."""
    fresh_broker = ResourceBroker(idle_timeout_seconds=300, min_available_ram_mb=2048)
    monkeypatch.setattr(orchestrator_module, "resource_broker", fresh_broker)
    monkeypatch.setattr(settings.llm, "provider", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "test-key-123")

    build_llm_client()

    assert not fresh_broker.is_registered("ollama.chat_model")

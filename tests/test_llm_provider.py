"""
Tests de contrato de agent_core/llm/provider.py — confirman que el
protocolo LLMProvider se sostiene de verdad contra TRES implementaciones
reales (OllamaClient, OpenAICompatibleClient — wire formats distintos —
y RuntimeManagedLLMProvider, ver agent_core/runtime/, que envuelve a
cualquiera de las dos), no solo en el papel. Si alguna vez alguna deja
de cumplir esta forma (o se agrega un proveedor nuevo que no la
cumple), este test lo detecta antes de que llegue a producción.
"""
from __future__ import annotations

from agent_core.llm.ollama_client import OllamaClient, OllamaError
from agent_core.llm.openai_compatible_client import OpenAICompatibleClient
from agent_core.llm.provider import ChatResponse, LLMProvider, ProviderError, ToolCall
from agent_core.runtime.managed_provider import RuntimeManagedLLMProvider
from agent_core.runtime.manager import RuntimeManager


def test_ollama_client_satisfies_the_llm_provider_protocol():
    assert isinstance(OllamaClient(), LLMProvider)


def test_openai_compatible_client_satisfies_the_llm_provider_protocol():
    assert isinstance(OpenAICompatibleClient(), LLMProvider)


def test_runtime_managed_provider_satisfies_the_llm_provider_protocol():
    from agent_core.runtime.llm_runtimes import OllamaRuntime

    manager = RuntimeManager()
    manager.register("active", OllamaRuntime(OllamaClient(), max_parallel=1))
    assert isinstance(RuntimeManagedLLMProvider(manager, "active"), LLMProvider)


def test_ollama_error_is_a_provider_error():
    # El núcleo (agent_loop.py, planner.py, self_diagnosis.py) atrapa
    # ProviderError — un OllamaError debe seguir siendo atrapado ahí
    # sin que el núcleo necesite saber que es específico de Ollama.
    assert issubclass(OllamaError, ProviderError)
    assert isinstance(OllamaError("boom"), ProviderError)


def test_chat_response_default_tool_calls_is_empty():
    response = ChatResponse(content="hola")
    assert response.tool_calls == []
    assert response.has_tool_calls is False


def test_chat_response_has_tool_calls_reflects_tool_calls_list():
    response = ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={})])
    assert response.has_tool_calls is True

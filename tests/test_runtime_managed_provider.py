"""
Tests de agent_core/runtime/managed_provider.py::RuntimeManagedLLMProvider
— implementa LLMProvider (agent_core/llm/provider.py) delegando en el
RuntimeManager en vez de hablarle directo a un cliente concreto.
AgentLoop/Planner/SelfDiagnosisAgent siguen viendo exactamente la
misma interfaz de siempre (chat/list_models/is_available) sin
enterarse de que este mecanismo existe.
"""
from __future__ import annotations

from agent_core.llm.provider import ChatResponse, LLMProvider
from agent_core.runtime.managed_provider import RuntimeManagedLLMProvider
from agent_core.runtime.manager import RuntimeManager
from agent_core.runtime.protocol import RuntimeCapabilities, RuntimeStatus


class _FakeUnderlyingClient:
    def __init__(self):
        self.chat_calls: list = []

    def chat(self, messages, model=None, tools=None, response_format=None, temperature=None):
        self.chat_calls.append({
            "messages": messages, "model": model, "tools": tools,
            "response_format": response_format, "temperature": temperature,
        })
        return ChatResponse(content="respuesta real")

    def list_models(self):
        return ["modelo-a", "modelo-b"]

    def is_available(self):
        return True


class _FakeLLMRuntime:
    """Mismo molde que OllamaRuntime/OpenAICompatibleRuntime (ver
    agent_core/runtime/llm_runtimes.py): expone `.client` para las
    llamadas de metadata que deliberadamente no pasan por el semáforo."""

    def __init__(self, client):
        self.client = client

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(supports=frozenset({"chat"}), max_parallel=1)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(available=self.client.is_available())

    def execute(self, request):
        return self.client.chat(**request.payload)


def _provider() -> tuple[RuntimeManagedLLMProvider, _FakeUnderlyingClient]:
    manager = RuntimeManager()
    client = _FakeUnderlyingClient()
    manager.register("active", _FakeLLMRuntime(client))
    return RuntimeManagedLLMProvider(manager, "active"), client


def test_satisfies_the_llm_provider_protocol_structurally():
    provider, _ = _provider()
    assert isinstance(provider, LLMProvider)


def test_chat_delegates_to_the_registered_runtime_with_the_same_arguments():
    provider, client = _provider()
    messages = [{"role": "user", "content": "hola"}]

    result = provider.chat(messages, model="algun-modelo", tools=[{"type": "function"}])

    assert result == ChatResponse(content="respuesta real")
    assert client.chat_calls == [{
        "messages": messages, "model": "algun-modelo", "tools": [{"type": "function"}],
        "response_format": None, "temperature": None,
    }]


def test_chat_passes_through_response_format_and_temperature_only_when_requested():
    """
    Agregado para el Conversation Engine (agent_core/conversation_engine.py),
    que necesita forzar modo JSON y un temperature bajo — antes
    RuntimeManagedLLMProvider.chat() solo conocía messages/model/tools,
    así que conectar el Conversation Engine al Runtime Manager habría
    perdido estos dos kwargs en silencio.
    """
    provider, client = _provider()
    messages = [{"role": "user", "content": "hola"}]

    provider.chat(messages, model="algun-modelo", response_format="json", temperature=0.1)

    assert client.chat_calls[-1]["response_format"] == "json"
    assert client.chat_calls[-1]["temperature"] == 0.1

    provider.chat(messages, model="algun-modelo")

    assert client.chat_calls[-1]["response_format"] is None
    assert client.chat_calls[-1]["temperature"] is None


def test_list_models_delegates_directly_to_the_underlying_client():
    provider, _ = _provider()
    assert provider.list_models() == ["modelo-a", "modelo-b"]


def test_is_available_delegates_directly_to_the_underlying_client():
    provider, _ = _provider()
    assert provider.is_available() is True

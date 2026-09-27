"""
Runtimes concretos para la capacidad "chat" — envuelven los DOS
LLMProvider reales que ya existían antes de este Runtime Manager
(agent_core/llm/ollama_client.py::OllamaClient,
agent_core/llm/openai_compatible_client.py::OpenAICompatibleClient),
sin tocar su lógica interna en absoluto. `max_parallel` es la única
pieza nueva: un runtime local sin VRAM dedicada normalmente solo
tolera 1 modelo grande a la vez (ver utils/config.py::RuntimeConfig),
una API en la nube tolera muchísimos — pero eso es un NÚMERO de
configuración, nunca una rama de código que sepa qué es VRAM/CUDA.
"""
from __future__ import annotations

from typing import Any

from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.openai_compatible_client import OpenAICompatibleClient
from agent_core.runtime.protocol import (
    ExecutionRequest,
    RuntimeCapabilities,
    RuntimeStatus,
)

_CHAT_CAPABILITIES = frozenset({"chat"})


class OllamaRuntime:
    def __init__(self, client: OllamaClient, max_parallel: int):
        self.client = client  # público a propósito — ver RuntimeManager.get_runtime()
        self._max_parallel = max_parallel

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(supports=_CHAT_CAPABILITIES, max_parallel=self._max_parallel)

    def status(self) -> RuntimeStatus:
        # queue_depth real (cuántas ejecuciones esperan el semáforo)
        # queda fuera de alcance de esta fase — ver "fuera de alcance"
        # en el plan de diseño (cola visible/priorización).
        return RuntimeStatus(available=self.client.is_available(), queue_depth=0)

    def execute(self, request: ExecutionRequest) -> Any:
        return self.client.chat(**request.payload)


class OpenAICompatibleRuntime:
    def __init__(self, client: OpenAICompatibleClient, max_parallel: int):
        self.client = client
        self._max_parallel = max_parallel

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(supports=_CHAT_CAPABILITIES, max_parallel=self._max_parallel)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(available=self.client.is_available(), queue_depth=0)

    def execute(self, request: ExecutionRequest) -> Any:
        return self.client.chat(**request.payload)

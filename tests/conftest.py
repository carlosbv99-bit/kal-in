"""
Fixtures compartidos.

1. Docker real para los tests de integración de sandbox
   (test_sandbox_integration.py, test_sandbox_escape_resistance.py).
2. Conversation Engine neutralizado por defecto en TODOS los tests que
   pasan por /chat (ver agent_core/routers/chat.py) — BUG REAL
   ENCONTRADO EN USO al agregar el Conversation Engine (2026-07-21):
   sin este fixture, cualquier test existente que llame a /chat sin
   mockear explícitamente orchestrator.conversation_engine dispara una
   llamada REAL a Ollama (qwen2.5:3b) en cada request — tests antes
   instantáneos (mockeados end-to-end) pasaron a tardar segundos reales
   y a depender de que Ollama esté corriendo. `classify()` ya es
   "fail-open" por diseño (None = seguir el flujo normal), así que
   neutralizarlo en tests no cambia ningún comportamiento probado,
   solo evita la llamada de red real. Cualquier test que SÍ quiera
   ejercitar el Conversation Engine (ver
   test_orchestrator_chat_conversation_engine.py) sobreescribe esto
   con su propio monkeypatch.setattr, que gana porque corre después.
"""
from __future__ import annotations

import docker
import pytest
from docker.errors import DockerException

from agent_core.orchestrator import orchestrator
from kernel.lifecycle.docker_runner import DockerSandboxRunner


@pytest.fixture(autouse=True)
def _disable_conversation_engine_network_calls(monkeypatch):
    monkeypatch.setattr(orchestrator.conversation_engine, "classify", lambda goal: None)


@pytest.fixture(autouse=True)
def _aislar_ledger_epistemico(monkeypatch):
    """
    El runtime del agente usa un ledger durable (SQLite en data/epistemics/,
    decisión D4). En tests eso sería estado COMPARTIDO entre tests y entre
    corridas: una hipótesis o un déficit de un test bloquearía al siguiente.

    Cada test arranca con un store en memoria limpio. Los tests que quieren
    ejercitar el backend lo construyen ellos (ver tests/test_kal_epistemics_sqlite.py),
    y los que quieren el store real del endpoint /chat lo parchean después de este
    fixture, que gana porque corre antes.
    """
    from agent_core.epistemics import EpistemicStore

    monkeypatch.setattr(
        orchestrator.agent, "epistemics", EpistemicStore(clock=lambda: 1000.0)
    )


def docker_available() -> bool:
    try:
        docker.from_env().ping()
        return True
    except DockerException:
        return False


requires_docker = pytest.mark.skipif(
    not docker_available(), reason="Docker no disponible en este entorno"
)


@pytest.fixture(scope="session")
def runner():
    return DockerSandboxRunner()

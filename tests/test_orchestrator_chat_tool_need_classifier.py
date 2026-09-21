"""
Tests de agent_core/routers/chat.py — integración del clasificador
local de necesidad de herramienta (ver agent_core/tool_need_classifier.py,
generaliza get_trivial_reply() más allá de la coincidencia EXACTA de
saludos, kal-in issue #4).

Diseño asimétrico: SOLO corta el turno cuando el clasificador predice
needs_tool=False con alta confianza — nunca al revés. `predict_needs_tool`
y `orchestrator.agent.answer_directly` mockeados; no se ejercita ningún
LLM real ni el modelo entrenado de verdad (eso lo cubre
tests/test_tool_need_classifier.py).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core import orchestrator as orchestrator_module
from agent_core.conversation_engine import ConversationEngineResult
from agent_core.llm.agent_loop import AgentRunResult, AgentStep
from agent_core.llm.planner import Plan, PlanRunResult, PlanStep, PlanStepResult
from agent_core.orchestrator import app
from utils.config import settings

client = TestClient(app, base_url="http://localhost")


class _FakeConversationEngine:
    def __init__(self, result=None):
        self._result = result
        self.calls: list[str] = []
        # answer_directly() en el camino no_tool_needed lee
        # orchestrator.conversation_engine.llm_client — cualquier
        # sentinel alcanza en los tests que mockean answer_directly()
        # entero, pero el atributo tiene que existir.
        self.llm_client = object()

    def classify(self, goal: str):
        self.calls.append(goal)
        return self._result


class _NeverCallMePlanningAgent:
    def run(self, *args, **kwargs):
        raise AssertionError("planning_agent.run() no debería llamarse")


def _scripted_planning_result() -> PlanRunResult:
    step = AgentStep(tool_name="run_code", arguments={}, observation="listo", artifact=None)
    agent_result = AgentRunResult(goal="ejecuta esto", final_answer="Listo.", steps=[step])
    return PlanRunResult(
        goal="ejecuta esto",
        plan=Plan(goal="ejecuta esto", steps=[PlanStep(description="ejecuta esto")]),
        step_results=[PlanStepResult(step="ejecuta esto", result=agent_result)],
        final_answer="Listo.",
    )


def test_confident_no_tool_prediction_answers_directly_without_classify_or_agent(monkeypatch):
    monkeypatch.setattr(
        "agent_core.routers.chat.predict_needs_tool", lambda goal: (False, 0.95)
    )
    fake_answer_directly_calls: list[dict] = []

    def _fake_answer_directly(goal, llm_client=None, model=None, history=None, session_context=None):
        fake_answer_directly_calls.append({"goal": goal, "llm_client": llm_client, "model": model})
        return "Todo bien por acá, ¿en qué te ayudo?"

    fake_ce = _FakeConversationEngine(None)
    monkeypatch.setattr(orchestrator_module.orchestrator.agent, "answer_directly", _fake_answer_directly)
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", fake_ce)
    monkeypatch.setattr(orchestrator_module.orchestrator, "planning_agent", _NeverCallMePlanningAgent())

    response = client.post("/chat", json={"goal": "todo bien por ahi?"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_tool_needed"
    assert body["final_answer"] == "Todo bien por acá, ¿en qué te ayudo?"
    assert body["plan"] == []
    assert body["steps"] == []
    # El modelo propio de este rol (tool_need_classifier.answer_model),
    # nunca el grande ni conversation_engine.model — no hay herramientas
    # de por medio, no hace falta cargar/usar el default.
    assert body["model_used"] == settings.tool_need_classifier.answer_model
    assert len(fake_answer_directly_calls) == 1
    call = fake_answer_directly_calls[0]
    assert call["goal"] == "todo bien por ahi?"
    assert call["llm_client"] is fake_ce.llm_client
    assert call["model"] == settings.tool_need_classifier.answer_model


def test_low_confidence_no_tool_prediction_falls_through_to_the_agent_normally(monkeypatch):
    """Confianza insuficiente (< tool_need_classifier.confidence_threshold)
    — el diseño asimétrico exige NO cortar, seguir como si el
    clasificador no existiera."""
    monkeypatch.setattr(
        "agent_core.routers.chat.predict_needs_tool", lambda goal: (False, 0.3)
    )
    fake_ce = _FakeConversationEngine(
        ConversationEngineResult(intent="ejecutar", confidence=0.95, required_capabilities=[], user_reply="listo")
    )
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", fake_ce)
    monkeypatch.setattr(
        orchestrator_module.orchestrator, "planning_agent",
        type("_", (), {"run": staticmethod(lambda *a, **kw: _scripted_planning_result())})(),
    )

    response = client.post("/chat", json={"goal": "ejecuta esto"})

    assert response.status_code == 200
    assert response.json()["status"] != "no_tool_needed"
    assert fake_ce.calls == ["ejecuta esto"]


def test_needs_tool_prediction_falls_through_to_the_agent_normally(monkeypatch):
    monkeypatch.setattr(
        "agent_core.routers.chat.predict_needs_tool", lambda goal: (True, 0.98)
    )
    fake_ce = _FakeConversationEngine(
        ConversationEngineResult(intent="ejecutar", confidence=0.95, required_capabilities=[], user_reply="listo")
    )
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", fake_ce)
    monkeypatch.setattr(
        orchestrator_module.orchestrator, "planning_agent",
        type("_", (), {"run": staticmethod(lambda *a, **kw: _scripted_planning_result())})(),
    )

    response = client.post("/chat", json={"goal": "ejecuta esto"})

    assert response.status_code == 200
    assert response.json()["status"] != "no_tool_needed"
    assert fake_ce.calls == ["ejecuta esto"]


def test_disabled_via_config_never_short_circuits(monkeypatch):
    monkeypatch.setattr(settings.tool_need_classifier, "enabled", False)
    monkeypatch.setattr(
        "agent_core.routers.chat.predict_needs_tool",
        lambda goal: (_ for _ in ()).throw(AssertionError("no debería llamarse si enabled=False")),
    )
    fake_ce = _FakeConversationEngine(None)
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", fake_ce)
    monkeypatch.setattr(
        orchestrator_module.orchestrator, "planning_agent",
        type("_", (), {"run": staticmethod(lambda *a, **kw: _scripted_planning_result())})(),
    )

    response = client.post("/chat", json={"goal": "ejecuta esto"})

    assert response.status_code == 200
    assert response.json()["status"] != "no_tool_needed"

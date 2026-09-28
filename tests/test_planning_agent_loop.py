"""
Tests de agent_core/llm/planner.py::PlanningAgentLoop — la orquestación
Planner -> AgentLoop.run() por subtarea -> síntesis final.

Usa AgentLoop real (no un doble) para no reimplementar su lógica ReAct,
pero con un OllamaClient falso guionado y un TaskExecutor/MemoryManager
falsos, mismo patrón que tests/test_agent_loop.py — así no requiere
Ollama ni Docker reales.
"""
from __future__ import annotations

from agent_core.llm.agent_loop import AgentLoop, AgentTool
from agent_core.llm.ollama_client import OllamaError
from agent_core.llm.planner import Planner, PlanningAgentLoop
from agent_core.llm.provider import ChatResponse, ToolCall
from sdk.permissions import Permission


class FakeOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, model=None, tools=None, temperature=None):
        self.calls.append({"messages": messages, "model": model, "tools": tools, "temperature": temperature})
        if not self.responses:
            raise AssertionError("FakeOllamaClient se quedó sin respuestas guionadas")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTaskExecutor:
    def submit(self, description):
        return object()

    def run_sandboxed(self, task, code, **kwargs):
        from task_execution.task import TaskStatus

        class FakeTask:
            status = TaskStatus.SUCCESS
            result = "ok"
            error = None

        return FakeTask()


class FakeMemoryManager:
    def remember(self, content, metadata=None, session_id=None):
        class Item:
            id = "fake-id"

        return Item()

    def recall(self, query, top_k=3, session_id=None):
        return {"short_term": [], "mid_term": [], "long_term": []}


def _make_loop(responses, tools=None):
    llm = FakeOllamaClient(responses)
    loop = AgentLoop(llm_client=llm, task_executor=FakeTaskExecutor(), memory=FakeMemoryManager(), tools=tools)
    return loop, llm


def test_single_step_plan_uses_subtask_final_answer_without_synthesis_call():
    # 1ra respuesta: el plan (un solo paso). 2da: la respuesta final del
    # único paso ReAct. Si hubiera una llamada de síntesis de más, la
    # FakeOllamaClient explotaría por quedarse sin respuestas guionadas.
    responses = [
        ChatResponse(content='{"steps": ["responder 2+2"]}'),
        ChatResponse(content="La respuesta es 4."),
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("¿Cuánto es 2+2?")

    assert result.status == "success"
    assert result.final_answer == "La respuesta es 4."
    assert len(result.step_results) == 1
    assert len(llm.calls) == 2


def test_multi_step_plan_runs_each_subtask_and_synthesizes_final_answer():
    responses = [
        ChatResponse(content='{"steps": ["paso uno", "paso dos"]}'),
        ChatResponse(content="Resultado del paso uno."),
        ChatResponse(content="Resultado del paso dos."),
        ChatResponse(content="Respuesta final integrando ambos pasos."),
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("objetivo compuesto")

    assert result.status == "success"
    assert len(result.step_results) == 2
    assert result.step_results[0].result.final_answer == "Resultado del paso uno."
    assert result.step_results[1].result.final_answer == "Resultado del paso dos."
    assert result.final_answer == "Respuesta final integrando ambos pasos."


def test_use_planner_false_skips_planning_call_entirely():
    # Sin la llamada de planificación, la primera (y única) respuesta
    # guionada debe ser consumida directamente por el AgentLoop.
    responses = [ChatResponse(content="Respuesta directa sin planificar.")]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("algo simple", use_planner=False)

    assert result.final_answer == "Respuesta directa sin planificar."
    assert len(result.plan.steps) == 1
    assert len(llm.calls) == 1


def test_llm_error_in_a_subtask_stops_remaining_subtasks():
    responses = [
        ChatResponse(content='{"steps": ["paso uno", "paso dos"]}'),
        OllamaError("ollama caído a mitad de plan"),
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("objetivo compuesto")

    assert result.status == "llm_error"
    assert len(result.step_results) == 1  # nunca llegó a ejecutar "paso dos"


def test_max_steps_exceeded_in_a_subtask_is_reflected_in_final_status():
    from agent_core.llm.provider import ToolCall

    responses = [
        ChatResponse(content='{"steps": ["paso que no converge"]}'),
    ] + [
        ChatResponse(content="", tool_calls=[ToolCall(name="remember", arguments={"content": "x"})])
        for _ in range(2)
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("objetivo", max_steps=2)

    assert result.status == "max_steps_exceeded"
    assert result.step_results[0].result.status == "max_steps_exceeded"


def test_history_and_session_context_are_forwarded_to_each_subtask():
    responses = [
        ChatResponse(content='{"steps": ["paso uno", "paso dos"]}'),
        ChatResponse(content="Resultado del paso uno."),
        ChatResponse(content="Resultado del paso dos."),
        ChatResponse(content="Respuesta final integrando ambos pasos."),
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))
    history = [{"role": "user", "content": "turno anterior"}, {"role": "assistant", "content": "respuesta anterior"}]
    context = {"role": "system", "content": "Contexto de esta sesión: ..."}

    planning_loop.run("objetivo compuesto", history=history, session_context=context)

    # calls[0] es la llamada de planificación (Planner.plan() no recibe
    # history/session_context — solo los subtasks vía AgentLoop.run()).
    # calls[1] y calls[2] son cada subtarea. El contexto de sesión va
    # fundido en el único mensaje system (ver bug real documentado en
    # agent_loop.py), no como mensaje separado.
    for call in (llm.calls[1], llm.calls[2]):
        messages = call["messages"]
        assert context["content"] in messages[0]["content"]
        assert history[0] in messages
        assert history[1] in messages


def test_final_answer_falls_back_to_raw_summary_if_synthesis_call_fails():
    responses = [
        ChatResponse(content='{"steps": ["paso uno", "paso dos"]}'),
        ChatResponse(content="Resultado uno."),
        ChatResponse(content="Resultado dos."),
        OllamaError("ollama caído justo en la síntesis"),
    ]
    loop, llm = _make_loop(responses)
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("objetivo compuesto")

    assert result.status == "success"  # los pasos en sí no fallaron
    assert "Resultado uno." in result.final_answer
    assert "Resultado dos." in result.final_answer


def test_denied_permissions_reaches_each_subtask_step():
    """PlanningAgentLoop.run() debe reenviar denied_permissions tal cual
    a cada AgentLoop.run() por subtarea — mismo patrón que history/
    session_context, ya probado arriba."""
    tool = AgentTool(
        name="con_red", description="d", parameters_schema={"type": "object", "properties": {}},
        handler=lambda **kw: "no debería llamarse", permissions=frozenset({Permission.NETWORK}),
        trust_tier="system",
    )
    responses = [
        ChatResponse(content='{"steps": ["un paso"]}'),
        ChatResponse(content="", tool_calls=[ToolCall(name="con_red", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, llm = _make_loop(responses, tools=[tool])
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))

    result = planning_loop.run("objetivo", denied_permissions=frozenset({Permission.NETWORK}))

    assert "ERROR" in result.step_results[0].result.steps[0].observation
    assert "network" in result.step_results[0].result.steps[0].observation


def test_on_step_is_forwarded_to_each_subtask(monkeypatch):
    """Recorrido en vivo (2026-07-24, ver agent_core/routers/chat.py):
    on_step debe reenviarse tal cual a cada AgentLoop.run() por
    subtarea — mismo patrón que denied_permissions/history arriba."""
    tool = AgentTool(
        name="remember", description="d", parameters_schema={"type": "object", "properties": {}},
        handler=lambda **kw: "recordado",
    )
    responses = [
        ChatResponse(content='{"steps": ["paso uno", "paso dos"]}'),
        ChatResponse(content="", tool_calls=[ToolCall(name="remember", arguments={"content": "x"})]),
        ChatResponse(content="listo uno"),
        ChatResponse(content="Resultado del paso dos."),
        ChatResponse(content="Respuesta final integrando ambos pasos."),
    ]
    loop, llm = _make_loop(responses, tools=[tool])
    planning_loop = PlanningAgentLoop(loop, planner=Planner(llm_client=llm))
    seen_steps: list = []

    planning_loop.run("objetivo compuesto", on_step=lambda step: seen_steps.append(step))

    assert len(seen_steps) == 1  # el único tool_call real, del primer subtask
    assert seen_steps[0].tool_name == "remember"

"""
Tests de agent_core/orchestrator.py::chat()::_step_artifact() — la
rama nueva para modality="project_files" (ver
tool_integration/adapters/vscode_files.py). A diferencia de la rama
"image" (que traduce una ruta de archivo real a una URL servida), acá
no hay ningún archivo ya escrito en disco — es una PROPUESTA que la
extensión de VS Code todavía tiene que revisar y aplicar, así que se
serializa completa (request_id + archivos) tal cual llega del Artifact.

`orchestrator.planning_agent.run` mockeado con un PlanRunResult
guionado — no se ejercita el LLM real, solo la serialización de la
respuesta de /chat.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core import orchestrator as orchestrator_module
from agent_core.llm.agent_loop import AgentRunResult, AgentStep
from agent_core.llm.planner import Plan, PlanRunResult, PlanStep, PlanStepResult
from agent_core.orchestrator import app
from sdk.artifacts import Artifact

client = TestClient(app, base_url="http://localhost")


def _scripted_result(artifact: Artifact | None) -> PlanRunResult:
    step = AgentStep(tool_name="propose_project_files", arguments={}, observation="listo", artifact=artifact)
    agent_result = AgentRunResult(goal="crear un sitio", final_answer="Archivos preparados.", steps=[step])
    return PlanRunResult(
        goal="crear un sitio",
        plan=Plan(goal="crear un sitio", steps=[PlanStep(description="crear un sitio")]),
        step_results=[PlanStepResult(step="crear un sitio", result=agent_result)],
        final_answer="Archivos preparados.",
    )


def test_project_files_artifact_is_serialized_with_request_id_and_files(monkeypatch):
    artifact = Artifact(
        modality="project_files",
        uri="",
        metadata={"status": "proposed", "request_id": "req-123", "files": [{"path": "index.html", "content": "<html></html>"}]},
    )
    monkeypatch.setattr(orchestrator_module.orchestrator, "planning_agent", type("_", (), {"run": staticmethod(lambda *a, **kw: _scripted_result(artifact))})())

    response = client.post("/chat", json={"goal": "crear un sitio", "client": "vscode"})

    assert response.status_code == 200
    step = response.json()["steps"][0]
    assert step["artifact"]["modality"] == "project_files"
    assert step["artifact"]["request_id"] == "req-123"
    assert step["artifact"]["files"] == [{"path": "index.html", "content": "<html></html>"}]


def test_no_artifact_serializes_as_none(monkeypatch):
    monkeypatch.setattr(orchestrator_module.orchestrator, "planning_agent", type("_", (), {"run": staticmethod(lambda *a, **kw: _scripted_result(None))})())

    # "hola" ya no sirve acá (kal-in issue #4): es un mensaje trivial que
    # ahora corta el turno ANTES de planning_agent.run() — ver
    # get_trivial_reply() en agent_core/conversation_engine.py. Este test
    # necesita un goal real para seguir ejercitando la serialización.
    response = client.post("/chat", json={"goal": "crear un sitio"})

    assert response.json()["steps"][0]["artifact"] is None

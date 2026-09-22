"""
Tests de GET /chat/sessions/{session_id}/artifacts (2026-07-27):
Session.active_artifact (agent_core/sessions.py) solo recuerda el
ÚLTIMO artefacto — sin esto no había forma de responder "qué generé en
esta sesión" más allá de eso. Este endpoint expone el historial
completo (Session.artifacts), poblado por /chat y por /uploads.

`orchestrator.conversation_engine`/`orchestrator.planning_agent`
mockeados — no se ejercita ningún LLM real, mismo patrón que
test_orchestrator_chat_progress.py.
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from agent_core import orchestrator as orchestrator_module
from agent_core.llm.agent_loop import AgentRunResult, AgentStep
from agent_core.llm.planner import Plan, PlanRunResult, PlanStep, PlanStepResult
from agent_core.orchestrator import app
from sdk.artifacts import Artifact

client = TestClient(app, base_url="http://localhost")


class _FakeConversationEngine:
    def classify(self, goal: str):
        return None


class _FakePlanningAgent:
    def __init__(self, steps: list[AgentStep]):
        self._steps = steps

    def run(self, goal, **kwargs):
        agent_result = AgentRunResult(goal=goal, final_answer="Listo.", steps=self._steps)
        return PlanRunResult(
            goal=goal,
            plan=Plan(goal=goal, steps=[PlanStep(description=goal)]),
            step_results=[PlanStepResult(step=goal, result=agent_result)],
            final_answer="Listo.",
        )


def test_a_new_session_has_no_artifacts(monkeypatch):
    response = client.get("/chat/sessions/una-sesion-que-nunca-existio/artifacts")

    assert response.status_code == 200
    assert response.json() == {"artifacts": []}


def test_chat_records_a_generated_artifact_into_the_session_history(monkeypatch):
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", _FakeConversationEngine())
    steps = [
        AgentStep(
            tool_name="image_generation", arguments={"prompt": "un logo"}, observation="ok",
            artifact=Artifact(modality="image", uri="data/artifacts/images/x.png"),
        )
    ]
    monkeypatch.setattr(orchestrator_module.orchestrator, "planning_agent", _FakePlanningAgent(steps))

    chat_response = client.post("/chat", json={"goal": "generá un logo"})
    session_id = chat_response.json()["session_id"]

    artifacts = client.get(f"/chat/sessions/{session_id}/artifacts").json()["artifacts"]

    assert len(artifacts) == 1
    assert artifacts[0]["modality"] == "image"
    assert artifacts[0]["tool_name"] == "image_generation"
    assert artifacts[0]["path"] == "data/artifacts/images/x.png"
    assert "created_at" in artifacts[0]


def test_history_accumulates_across_turns_of_the_same_session_unlike_active_artifact(monkeypatch):
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", _FakeConversationEngine())

    monkeypatch.setattr(
        orchestrator_module.orchestrator, "planning_agent",
        _FakePlanningAgent([
            AgentStep(
                tool_name="image_generation", arguments={}, observation="ok",
                artifact=Artifact(modality="image", uri="data/artifacts/images/uno.png"),
            )
        ]),
    )
    first = client.post("/chat", json={"goal": "primer logo"})
    session_id = first.json()["session_id"]

    monkeypatch.setattr(
        orchestrator_module.orchestrator, "planning_agent",
        _FakePlanningAgent([
            AgentStep(
                tool_name="image_editing", arguments={}, observation="ok",
                artifact=Artifact(modality="image", uri="data/artifacts/images/dos.png"),
            )
        ]),
    )
    client.post("/chat", json={"goal": "cambiale el fondo", "session_id": session_id})

    artifacts = client.get(f"/chat/sessions/{session_id}/artifacts").json()["artifacts"]

    assert [a["path"] for a in artifacts] == ["data/artifacts/images/uno.png", "data/artifacts/images/dos.png"]


def test_text_only_artifacts_are_never_recorded_in_the_history(monkeypatch):
    """Mismo filtro que ya aplicaba update_active_artifact: un artefacto
    modality="text" (p.ej. run_code) no es un artefacto multimedia real
    para este historial."""
    monkeypatch.setattr(orchestrator_module.orchestrator, "conversation_engine", _FakeConversationEngine())
    steps = [
        AgentStep(
            tool_name="run_code", arguments={}, observation="ok",
            artifact=Artifact(modality="text", uri="", metadata={"status": "success"}),
        )
    ]
    monkeypatch.setattr(orchestrator_module.orchestrator, "planning_agent", _FakePlanningAgent(steps))

    response = client.post("/chat", json={"goal": "corré esto"})
    session_id = response.json()["session_id"]

    artifacts = client.get(f"/chat/sessions/{session_id}/artifacts").json()["artifacts"]

    assert artifacts == []


def test_uploading_an_image_records_it_into_the_session_artifact_history():
    files = {"file": ("foto.png", io.BytesIO(b"contenido-falso-de-imagen"), "image/png")}

    upload_response = client.post("/uploads", files=files)
    session_id = upload_response.json()["session_id"]

    artifacts = client.get(f"/chat/sessions/{session_id}/artifacts").json()["artifacts"]

    assert len(artifacts) == 1
    assert artifacts[0]["tool_name"] == "upload"
    assert artifacts[0]["modality"] == "image"


def test_uploading_an_audio_records_it_as_an_audio_artifact():
    """
    BUG REAL ENCONTRADO EN USO (2026-09-22): un audio subido por el
    usuario no tenía forma de entrar al mismo mecanismo de "artefacto
    activo" que ya usan las imágenes — speech_to_text (faster-whisper)
    existía como herramienta, pero /uploads solo aceptaba png/jpeg/webp,
    así que un audio propio del usuario nunca se convertía en el
    artefacto activo de la sesión (a diferencia de una imagen subida).
    """
    files = {"file": ("nota_de_voz.wav", io.BytesIO(b"contenido-falso-de-audio"), "audio/wav")}

    upload_response = client.post("/uploads", files=files)
    assert upload_response.status_code == 200
    session_id = upload_response.json()["session_id"]

    artifacts = client.get(f"/chat/sessions/{session_id}/artifacts").json()["artifacts"]

    assert len(artifacts) == 1
    assert artifacts[0]["tool_name"] == "upload"
    assert artifacts[0]["modality"] == "audio"


def test_uploading_an_unsupported_content_type_is_rejected():
    files = {"file": ("archivo.pdf", io.BytesIO(b"no es ni imagen ni audio"), "application/pdf")}

    response = client.post("/uploads", files=files)

    assert response.status_code == 400
    assert "application/pdf" in response.json()["detail"]

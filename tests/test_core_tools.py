"""
Tests de tool_integration/adapters/core_tools.py: CodeExecutionTool,
MemoryRememberTool, MemoryRecallTool como `Tool` de verdad (no las
closures que existían antes dentro de agent_loop.py).

Usa dobles de TaskExecutor/MemoryManager (mismo patrón que
tests/test_agent_loop.py) para no requerir Docker ni Ollama.
"""
from __future__ import annotations

from agent_core.memory.base import MemoryConfidence, MemoryItem
from task_execution.task import TaskStatus
from tool_integration.adapters.core_tools import CodeExecutionTool, MemoryRecallTool, MemoryRememberTool
from utils.config import settings


class FakeTask:
    def __init__(self, status, result=None, error=None):
        self.status = status
        self.result = result
        self.error = error


class FakeTaskExecutor:
    def __init__(self, results=None):
        self.results = results or []
        self.submitted = []

    def submit(self, description):
        self.submitted.append(description)
        return object()

    def run_sandboxed(self, task, code, **kwargs):
        if self.results:
            return self.results.pop(0)
        return FakeTask(status=TaskStatus.SUCCESS, result="ok")


class FakeMemoryManager:
    def __init__(self):
        self.remembered = []

    def remember(self, content, metadata=None, session_id=None):
        self.remembered.append(content)

        class Item:
            id = "fake-id"

        return Item()

    def recall(self, query, top_k=3, session_id=None):
        return {"short_term": [], "mid_term": [], "long_term": []}


# --- CodeExecutionTool ---


def test_code_execution_success_returns_stdout_in_metadata():
    tool = CodeExecutionTool(FakeTaskExecutor())
    artifact = tool.execute(code="print(4)")

    assert artifact.modality == "text"
    assert artifact.metadata["status"] == "success"
    assert artifact.metadata["stdout"] == "ok"


def test_code_execution_failure_reports_status_and_stderr():
    executor = FakeTaskExecutor(results=[FakeTask(status=TaskStatus.FAILED, error="ValueError: boom")])
    tool = CodeExecutionTool(executor)

    artifact = tool.execute(code="raise ValueError('boom')")

    assert artifact.metadata["status"] == "failed"
    assert artifact.metadata["stderr"] == "ValueError: boom"


def test_code_execution_submits_task_before_running():
    executor = FakeTaskExecutor()
    tool = CodeExecutionTool(executor)

    tool.execute(code="print(1)")

    assert len(executor.submitted) == 1


# --- MemoryRememberTool ---


def test_remember_stores_content_and_reports_id():
    memory = FakeMemoryManager()
    tool = MemoryRememberTool(memory)

    artifact = tool.execute(content="dato importante")

    assert memory.remembered == ["dato importante"]
    assert "fake-id" in artifact.metadata["summary"]


# --- MemoryRecallTool ---


def test_recall_with_no_results_reports_empty():
    tool = MemoryRecallTool(FakeMemoryManager())

    artifact = tool.execute(query="algo")

    assert artifact.metadata["summary"] == "Sin resultados en memoria."


def test_recall_formats_results_by_tier():
    class MemoryWithResults(FakeMemoryManager):
        def recall(self, query, top_k=3, session_id=None):
            return {
                "short_term": [MemoryItem(content="a", confidence=MemoryConfidence.TEMPORAL)],
                "mid_term": [],
                "long_term": [MemoryItem(content="b", confidence=MemoryConfidence.VERIFICADA)],
            }

    tool = MemoryRecallTool(MemoryWithResults())
    artifact = tool.execute(query="algo")

    assert "short_term: [temporal] a" in artifact.metadata["summary"]
    assert "long_term: [verificada] b" in artifact.metadata["summary"]
    assert "mid_term" not in artifact.metadata["summary"]  # tier vacío se omite


def test_recall_marks_confidence_level_so_llm_can_distinguish_trust():
    """
    Bug real: el modelo reportó una ruta de archivo desactualizada porque
    confió en memoria vieja (recall()) en vez de en el resultado fresco de
    ESE turno. Sin esta etiqueta, [temporal]/[aprendida] (sin confirmar) se
    veían indistinguibles de [verificada]/[permanente] (confirmado por un
    humano) — el LLM no tenía cómo priorizar.
    """
    class MemoryWithResults(FakeMemoryManager):
        def recall(self, query, top_k=3, session_id=None):
            return {
                "short_term": [MemoryItem(content="dato sin confirmar", confidence=MemoryConfidence.APRENDIDA)],
                "mid_term": [],
                "long_term": [],
            }

    tool = MemoryRecallTool(MemoryWithResults())
    artifact = tool.execute(query="algo")

    assert "[aprendida]" in artifact.metadata["summary"]


def test_recall_never_filters_local_only_items_when_provider_is_local():
    class MemoryWithResults(FakeMemoryManager):
        def recall(self, query, top_k=3, session_id=None):
            return {
                "short_term": [MemoryItem(content="secreto local", metadata={"sharing": "local_only"})],
                "mid_term": [], "long_term": [],
            }

    tool = MemoryRecallTool(MemoryWithResults())
    artifact = tool.execute(query="algo")

    assert "secreto local" in artifact.metadata["summary"]


def test_recall_filters_out_local_only_items_when_a_cloud_provider_is_active(monkeypatch):
    """
    Memory Security Policy Engine (Fase 1): algo guardado 100% local
    puede terminar inyectado como contexto a un proveedor en la nube si
    el usuario cambia llm.provider más adelante — este filtro corta esa
    fuga en el único punto donde recall() se convierte en texto para el
    LLM activo.
    """
    class MemoryWithResults(FakeMemoryManager):
        def recall(self, query, top_k=3, session_id=None):
            return {
                "short_term": [MemoryItem(content="secreto local", metadata={"sharing": "local_only"})],
                "mid_term": [],
                "long_term": [MemoryItem(content="dato compartible", metadata={"sharing": "cloud_ok"})],
            }

    monkeypatch.setattr(settings.llm, "provider", "openai_compatible")
    tool = MemoryRecallTool(MemoryWithResults())

    artifact = tool.execute(query="algo")

    assert "secreto local" not in artifact.metadata["summary"]
    assert "dato compartible" in artifact.metadata["summary"]


def test_recall_treats_missing_sharing_metadata_as_local_only_by_default(monkeypatch):
    """Fail-closed: un item guardado antes de que este campo existiera
    (o por cualquier otro motivo sin `sharing` en su metadata) se trata
    como local_only, nunca como compartible por omisión."""
    class MemoryWithResults(FakeMemoryManager):
        def recall(self, query, top_k=3, session_id=None):
            return {"short_term": [MemoryItem(content="sin metadata de sharing")], "mid_term": [], "long_term": []}

    monkeypatch.setattr(settings.llm, "provider", "openai_compatible")
    tool = MemoryRecallTool(MemoryWithResults())

    artifact = tool.execute(query="algo")

    assert artifact.metadata["summary"] == "Sin resultados en memoria."


def test_manifest_names_match_agent_loop_tool_call_names():
    """
    agent_loop.py despacha tool calls por nombre ("run_code", "remember",
    "recall") — si el manifest.name de estas Tool no coincide exactamente,
    el LLM llamaría a un nombre que el loop no reconoce.
    """
    assert CodeExecutionTool.manifest.name == "run_code"
    assert MemoryRememberTool.manifest.name == "remember"
    assert MemoryRecallTool.manifest.name == "recall"

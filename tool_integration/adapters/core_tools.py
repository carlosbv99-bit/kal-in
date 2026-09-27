"""
Herramientas "core" del agente (código sandboxeado, memoria) como `Tool`
de verdad, no como closures escritas a mano dentro de agent_loop.py.

A diferencia de los adaptadores multimodales (image_gen.py, etc., que
solo dependen de config.yaml y por eso pueden vivir como singletons
globales en kernel/registry/registry.py), estas tres dependen de
instancias concretas de TaskExecutor/MemoryManager que pertenecen a UN
AgentLoop en particular — por eso agent_core/llm/agent_loop.py las
instancia por sí mismo (ver _build_tools_from_registry), en vez de
registrarlas en el `tool_registry` global (process-wide).
"""
from __future__ import annotations

from typing import Any

from agent_core.memory.security_policy import is_cloud_provider
from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest
from utils.config import settings


class CodeExecutionTool(Tool):
    manifest = ToolManifest(
        name="run_code",
        description=(
            "Ejecuta código Python en un sandbox aislado (sin red, filesystem "
            "read-only salvo /workspace) y devuelve su salida estándar. Usar para "
            "cálculos, transformación de datos, o cualquier lógica expresable en Python. "
            "NO sirve para crear archivos que el usuario se lleve (una página web, un proyecto "
            "con varios archivos, etc.): `import os` y `open()` están prohibidos a propósito, "
            "cualquier intento falla. Para ese tipo de pedido, generá el contenido directamente "
            "en la respuesta final (bloques de código), no lo escribas con esta herramienta."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Código Python a ejecutar"}},
            "required": ["code"],
        },
        created_by="system",
    )

    def __init__(self, task_executor):
        self.task_executor = task_executor

    def execute(self, code: str, **kwargs: Any) -> Artifact:
        from task_execution.task import (
            TaskStatus,  # evita import circular a nivel de módulo
        )

        task = self.task_executor.submit("run_code vía agente")
        result = self.task_executor.run_sandboxed(task, code)

        if result.status == TaskStatus.SUCCESS:
            return Artifact(modality="text", uri="", metadata={"status": "success", "stdout": result.result})
        return Artifact(
            modality="text",
            uri="",
            metadata={"status": result.status.value, "stderr": result.error},
        )


class MemoryRememberTool(Tool):
    manifest = ToolManifest(
        name="remember",
        description="Guarda un dato en la memoria de kal para poder recuperarlo después.",
        parameters_schema={
            "type": "object",
            "properties": {"content": {"type": "string", "description": "Lo que hay que recordar"}},
            "required": ["content"],
        },
        created_by="system",
    )

    def __init__(self, memory):
        self.memory = memory

    def execute(self, content: str, **kwargs: Any) -> Artifact:
        item = self.memory.remember(content)
        return Artifact(
            modality="text", uri="",
            metadata={"summary": f"Guardado en memoria de corto plazo (id={item.id})"},
        )


class MemoryRecallTool(Tool):
    manifest = ToolManifest(
        name="recall",
        description="Busca en la memoria de kal (corto, mediano y largo plazo) información relevante.",
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Qué buscar"},
                "top_k": {"type": "integer", "description": "Resultados por nivel de memoria", "default": 3},
            },
            "required": ["query"],
        },
        created_by="system",
    )

    def __init__(self, memory):
        self.memory = memory

    def execute(self, query: str, top_k: int = 3, **kwargs: Any) -> Artifact:
        results = self.memory.recall(query, top_k=top_k)
        # Memory Security Policy Engine (Fase 1, ver
        # agent_core/memory/security_policy.py): si el proveedor de LLM
        # ACTIVO ahora mismo es en la nube, filtra cualquier item que no
        # esté marcado explícitamente "cloud_ok" — sin importar el tier
        # (corto/mediano/largo, el riesgo de fuga existe en los tres).
        # Consecuencia real y deliberada: sin ningún mecanismo hoy para
        # marcar algo "cloud_ok", esto puede dejar recall() sin
        # resultados con un proveedor en la nube activo — fail-closed
        # correcto, no un bug.
        if is_cloud_provider(settings.llm.provider):
            results = {
                tier: [item for item in items if item.metadata.get("sharing", "local_only") != "local_only"]
                for tier, items in results.items()
            }
        # El nivel de confianza va entre corchetes por cada item — sin esto,
        # el LLM no tenía forma de distinguir un dato TEMPORAL (sin
        # confirmar, p.ej. algo que el propio agente dijo en otro turno) de
        # uno VERIFICADA/PERMANENTE (confirmado por un humano), y terminaba
        # tratando memoria vieja/no confirmada como si fuera un hecho firme
        # (bug real: reportó la ruta de una imagen desactualizada porque
        # confió en recall() en vez de en el resultado fresco de ESE turno).
        lines = [
            f"{tier}: " + "; ".join(f"[{i.confidence.value}] {i.content}" for i in items)
            for tier, items in results.items() if items
        ]
        summary = "\n".join(lines) if lines else "Sin resultados en memoria."
        return Artifact(modality="text", uri="", metadata={"summary": summary})

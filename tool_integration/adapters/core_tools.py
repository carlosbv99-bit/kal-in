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
from error_handling.detector import classify_sandbox_error
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

        metadata: dict[str, Any] = {"status": result.status.value, "stderr": result.error}
        # Un RECHAZO DE VALIDACIÓN ESTÁTICA significa que el código nunca llegó a
        # ejecutarse: el mensaje es información legítima para el modelo (tiene que
        # saber que su código no corrió), pero NO es una observación del mundo.
        #
        # BUG REAL ENCONTRADO EN VIVO (2026-09-29, turno real contra el server):
        # el modelo mandó código con escapes `\n` literales, la validación lo
        # rechazó, y las TRES observaciones "ERROR (failed): Validación estática
        # falló…" entraron al ledger como `kernel_observed` con ancla. Como el
        # gate cuenta observaciones ingeridas por turno, el turno quedó como
        # verificado — y la respuesta afirmó un número de kernel INVENTADO
        # ("7.0.0-34-generic") sin marca de "sin verificación". El kernel ya lo
        # sabía (audita "Código rechazado antes de ejecutar"); lo que faltaba era
        # propagarlo a la decisión de ingestión.
        #
        # La clasificación se reusa de `error_handling/` (única fuente de verdad de
        # esa categoría, con sus propios tests) en vez de mirar el texto acá.
        error_type, _ = classify_sandbox_error(result.error or "")
        if error_type == "ValidationError":
            metadata["is_observation"] = False
        return Artifact(modality="text", uri="", metadata=metadata)


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

    def __init__(self, memory, session_id: str | None = None):
        self.memory = memory
        # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA
        # (Likay-OS, 2026-09-26), A-4: ver agent_core/memory/
        # short_term.py — sin esto, remember()/recall() compartían un
        # ÚNICO buffer de corto plazo entre TODAS las sesiones/usuarios
        # del proceso (fuga de datos entre conversaciones distintas).
        self.session_id = session_id

    def execute(self, content: str, **kwargs: Any) -> Artifact:
        item = self.memory.remember(content, session_id=self.session_id)
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

    def __init__(self, memory, session_id: str | None = None):
        self.memory = memory
        self.session_id = session_id  # ver comentario en MemoryRememberTool (A-4)

    def execute(self, query: str, top_k: int = 3, **kwargs: Any) -> Artifact:
        results = self.memory.recall(query, top_k=top_k, session_id=self.session_id)
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

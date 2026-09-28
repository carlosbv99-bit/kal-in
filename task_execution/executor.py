"""
TaskExecutor: registro y ejecución de tareas.

Dos métodos con propósitos distintos:
  - run(fn, ...): ejecuta un callable Python en el propio proceso.
    Pensado para orquestación interna de confianza (el propio código
    del agente), NO para código externo o generado dinámicamente — eso
    nunca debe ejecutarse fuera del sandbox.
  - run_sandboxed(source_code, ...): ejecuta código como texto dentro
    de sandbox/, con el ciclo completo de detección de error ->
    clasificación -> reparación -> reintento acotado. Este es el path
    real de "auto-reparación" tal como se diseñó en error_handling/.
"""
from __future__ import annotations

from agent_core.memory.base import MemoryItem
from agent_core.memory.mid_term import MidTermMemory
from error_handling.detector import ErrorDetector, classify_sandbox_error
from error_handling.strategies import (
    ImportErrorStrategy,
    RepairContext,
    RuntimeErrorStrategy,
)
from kernel.lifecycle.executor import SandboxExecutor
from task_execution.task import Task, TaskStatus
from utils.logger import get_logger

logger = get_logger(__name__)

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), M-7: POST /tasks no pide token admin (uso normal, mismo
# criterio que /chat) y cada submit() agregaba una entrada más a
# `_tasks` para siempre, sin límite — mismo problema que
# agent_core/sessions.py::SessionManager (ver ese archivo para el
# fix equivalente). Tope generoso, desalojando la tarea más VIEJA por
# created_at cuando se supera.
_MAX_TASKS = 1000


class TaskExecutor:
    def __init__(
        self,
        mid_term: MidTermMemory | None = None,
        sandbox: SandboxExecutor | None = None,
    ):
        self._tasks: dict[str, Task] = {}
        self.mid_term = mid_term or MidTermMemory()
        self.sandbox = sandbox or SandboxExecutor()
        # Inyecta este mismo mid_term en RuntimeErrorStrategy — sin esto,
        # la estrategia crearía su propia MidTermMemory() apuntando al
        # archivo por defecto del proyecto, ignorando los checkpoints
        # que _store_checkpoint() guarda aquí (bug real encontrado y
        # corregido durante el desarrollo de esta pieza).
        self.error_detector = ErrorDetector(
            strategies={
                "RuntimeError": RuntimeErrorStrategy(mid_term=self.mid_term),
                "ImportError": ImportErrorStrategy(),
                "ModuleNotFoundError": ImportErrorStrategy(),
            }
        )

    def submit(self, description: str) -> Task:
        task = Task(description=description)
        self._tasks[task.id] = task
        if len(self._tasks) > _MAX_TASKS:
            oldest_id = min(self._tasks, key=lambda tid: self._tasks[tid].created_at)
            del self._tasks[oldest_id]
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        """Para el dashboard del frontend — más recientes primero."""
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    # --- Ejecución de callables de confianza (sin sandbox) ---

    def run(self, task: Task, fn, *args, **kwargs) -> Task:
        """
        Ejecuta un callable Python directamente en este proceso. Solo
        para código propio del agente (orquestación), nunca para
        código externo/generado — eso va por run_sandboxed().
        """
        task.status = TaskStatus.RUNNING
        try:
            task.result = fn(*args, **kwargs)
            task.status = TaskStatus.SUCCESS
        except Exception as e:  # noqa: BLE001 — fn() es código arbitrario, puede lanzar cualquier cosa; ver RepairContext abajo
            logger.warning(f"Tarea {task.id} falló: {e}")
            ctx = RepairContext(
                error_type=type(e).__name__,
                error_message=str(e),
                source_code="",
                location=f"task:{task.id}",
            )
            repair_result = self.error_detector.handle(ctx)
            if repair_result.detail == "circuit_breaker_open":
                task.status = TaskStatus.ESCALATED
            else:
                # Sin código sandboxeado que reejecutar aquí (fn es un
                # callable, no texto) — para callables de confianza no
                # hay un mecanismo genérico de "reintentar con la
                # corrección". Ver run_sandboxed() para el ciclo completo.
                task.status = TaskStatus.FAILED
            task.error = str(e)
        return task

    # --- Ejecución sandboxeada con auto-reparación real ---

    def run_sandboxed(self, task: Task, source_code: str, max_retries: int = 2) -> Task:
        """
        Ejecuta `source_code` en sandbox/. Si falla, clasifica el error
        desde stderr, intenta repararlo vía error_handling/, y reintenta
        de forma acotada (max_retries), respetando el circuit breaker.
        """
        self._store_checkpoint(task, source_code)
        return self._run_sandboxed_attempt(task, source_code, attempt=0, max_retries=max_retries)

    def _run_sandboxed_attempt(self, task: Task, source_code: str, attempt: int, max_retries: int) -> Task:
        task.status = TaskStatus.RUNNING
        result = self.sandbox.execute(source_code, context={"task_id": task.id})

        if result.status == "success":
            task.result = result.stdout
            task.status = TaskStatus.SUCCESS
            return task

        error_type, error_message = classify_sandbox_error(result.stderr)
        logger.info(f"Tarea {task.id} falló en sandbox ({error_type}), intento {attempt + 1}/{max_retries + 1}")

        ctx = RepairContext(
            error_type=error_type,
            error_message=error_message,
            source_code=source_code,
            location=f"task:{task.id}",
        )
        repair_result = self.error_detector.handle(ctx)

        if repair_result.success and repair_result.already_retried:
            # La propia estrategia ya reejecutó el código reparado
            # (p.ej. ImportErrorStrategy instala y reejecuta en el
            # mismo contenedor) — su resultado es el final.
            task.result = repair_result.output
            task.status = TaskStatus.SUCCESS
            return task

        if repair_result.success and not repair_result.already_retried:
            if attempt < max_retries:
                # La estrategia confirmó que vale la pena reintentar
                # (p.ej. RuntimeErrorStrategy con checkpoint válido),
                # pero no reejecutó ella misma — lo hacemos aquí, y
                # reportamos el desenlace real al circuit breaker una
                # vez conocido (ver ErrorDetector.record_outcome).
                next_code = repair_result.fixed_code or source_code
                retried_task = self._run_sandboxed_attempt(task, next_code, attempt + 1, max_retries)
                self.error_detector.record_outcome(ctx, success=(retried_task.status == TaskStatus.SUCCESS))
                return retried_task
            else:
                # Se agotó el presupuesto de reintentos de ESTA llamada
                # sin confirmar que la reparación funcionó realmente —
                # se registra como fallo para que el circuit breaker
                # acumule correctamente si el llamador vuelve a invocar
                # run_sandboxed sobre la misma tarea más adelante.
                self.error_detector.record_outcome(ctx, success=False)
                task.status = TaskStatus.FAILED
                task.error = result.stderr
                return task

        if repair_result.detail == "circuit_breaker_open":
            task.status = TaskStatus.ESCALATED
        else:
            task.status = TaskStatus.FAILED
        task.error = result.stderr
        return task

    def _store_checkpoint(self, task: Task, source_code: str) -> None:
        """
        Guarda un checkpoint mínimo antes de ejecutar: permite que
        RuntimeErrorStrategy confirme que existió un punto de partida
        válido para esta tarea (ver limitación documentada en esa
        clase — esto no es un snapshot de estado real, solo un marcador).
        """
        item = MemoryItem(
            id=f"checkpoint:{task.id}",
            content=f"checkpoint para tarea {task.id}: {task.description}",
            metadata={"task_id": task.id},
        )
        self.mid_term.store(item)

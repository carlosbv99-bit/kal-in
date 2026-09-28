"""
RuntimeManager: registro de Runtimes (ver protocol.py) + despacho de
ejecuciones con un tope de paralelismo por runtime. Mismo patrón de
singleton que kernel/api/bus.py::KernelServiceBus /
kernel/registry/registry.py::tool_registry.

Único trabajo real: un semáforo por runtime registrado, calculado a
partir de `capabilities().max_parallel` — bloquea una ejecución nueva
si ya hay `max_parallel` en curso para ESE runtime, en vez de dejar
que compitan todas a la vez (la causa real del problema de contención
que motivó este diseño, ver docs/HISTORY.md "Runtime Manager"
2026-07-25). Nunca decide CÓMO ejecutar ni sabe nada de hardware —
eso es 100% responsabilidad del Runtime concreto.
"""
from __future__ import annotations

import threading
from typing import Any

from agent_core.runtime.protocol import (
    ExecutionRequest,
    Runtime,
    RuntimeCapabilities,
    RuntimeStatus,
)


class RuntimeNotFoundError(Exception):
    """No hay ningún runtime registrado con ese nombre."""


class RuntimeManager:
    def __init__(self):
        self._runtimes: dict[str, Runtime] = {}
        self._semaphores: dict[str, threading.Semaphore] = {}

    def register(self, name: str, runtime: Runtime) -> None:
        """Reemplaza cualquier runtime ya registrado bajo el mismo
        nombre (mismo criterio que KernelServiceBus.register()) — una
        ejecución YA en curso contra el runtime anterior no se ve
        afectada, solo las ejecuciones NUEVAS ven el reemplazo."""
        self._runtimes[name] = runtime
        self._semaphores[name] = threading.Semaphore(runtime.capabilities().max_parallel)

    def execute(self, name: str, request: ExecutionRequest) -> Any:
        runtime, semaphore = self._get(name)
        with semaphore:
            return runtime.execute(request)

    def status(self, name: str) -> RuntimeStatus:
        runtime, _ = self._get(name)
        return runtime.status()

    def capabilities(self, name: str) -> RuntimeCapabilities:
        runtime, _ = self._get(name)
        return runtime.capabilities()

    def get_runtime(self, name: str) -> Runtime:
        """Acceso directo al runtime registrado — para operaciones que
        deliberadamente NO deben pasar por el semáforo de execute()
        (p.ej. list_models()/is_available(), metadata liviana que debe
        responder aunque el runtime esté ocupado ejecutando algo
        pesado). Ver agent_core/runtime/managed_provider.py."""
        runtime, _ = self._get(name)
        return runtime

    def _get(self, name: str) -> tuple[Runtime, threading.Semaphore]:
        if name not in self._runtimes:
            raise RuntimeNotFoundError(f"runtime desconocido: '{name}'")
        return self._runtimes[name], self._semaphores[name]


runtime_manager = RuntimeManager()

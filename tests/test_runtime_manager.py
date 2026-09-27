"""
Tests de agent_core/runtime/manager.py::RuntimeManager — el Kernel
nunca sabe qué hardware hay detrás de un runtime (ver
agent_core/runtime/protocol.py), solo cuántas ejecuciones concurrentes
tolera (`max_parallel`). El caso real que motiva esto: varios pedidos
concurrentes compitiendo por cargar/descargar modelos grandes en la
misma máquina (ver docs/HISTORY.md "Runtime Manager", 2026-07-25) —
acá se prueba que el semáforo por runtime efectivamente serializa
ejecuciones más allá de `max_parallel`, con un runtime falso (sin
tocar Ollama real).
"""
from __future__ import annotations

import threading
import time

import pytest

from agent_core.runtime.manager import RuntimeManager, RuntimeNotFoundError
from agent_core.runtime.protocol import (
    ExecutionRequest,
    RuntimeCapabilities,
    RuntimeStatus,
)


class _FakeRuntime:
    def __init__(self, max_parallel: int = 1):
        self._max_parallel = max_parallel
        self.calls: list = []

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(supports=frozenset({"chat"}), max_parallel=self._max_parallel)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(available=True, queue_depth=0)

    def execute(self, request: ExecutionRequest):
        self.calls.append(request.payload)
        return f"echo:{request.payload}"


def test_execute_dispatches_to_the_registered_runtime():
    manager = RuntimeManager()
    manager.register("x", _FakeRuntime())

    result = manager.execute("x", ExecutionRequest(payload="hola"))

    assert result == "echo:hola"


def test_execute_on_unknown_runtime_raises():
    manager = RuntimeManager()
    with pytest.raises(RuntimeNotFoundError):
        manager.execute("no-existe", ExecutionRequest(payload="x"))


def test_status_and_capabilities_delegate_to_the_registered_runtime():
    manager = RuntimeManager()
    manager.register("x", _FakeRuntime(max_parallel=7))

    assert manager.capabilities("x").max_parallel == 7
    assert manager.status("x").available is True


def test_register_replaces_a_previously_registered_runtime_under_the_same_name():
    manager = RuntimeManager()
    manager.register("x", _FakeRuntime())
    second = _FakeRuntime()

    manager.register("x", second)
    manager.execute("x", ExecutionRequest(payload="va al segundo"))

    assert second.calls == ["va al segundo"]


def test_get_runtime_returns_the_registered_instance():
    manager = RuntimeManager()
    runtime = _FakeRuntime()
    manager.register("x", runtime)

    assert manager.get_runtime("x") is runtime


class _SlowRuntime:
    """Bloquea la primera ejecución hasta que el test la libera
    explícitamente — permite probar el semáforo sin depender de sleeps
    frágiles para el resultado en sí (solo se usa un sleep corto para
    darle tiempo al thread a arrancar, no para decidir el resultado)."""

    def __init__(self, max_parallel: int):
        self._max_parallel = max_parallel
        self.order: list[str] = []
        self.release_first = threading.Event()

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(supports=frozenset({"chat"}), max_parallel=self._max_parallel)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(available=True)

    def execute(self, request: ExecutionRequest):
        self.order.append(f"start:{request.payload}")
        if request.payload == "first":
            self.release_first.wait(timeout=5)
        self.order.append(f"end:{request.payload}")
        return request.payload


def test_max_parallel_one_serializes_concurrent_executions():
    """
    El caso real que motiva todo el Runtime Manager: dos ejecuciones
    concurrentes al MISMO runtime con max_parallel=1 no deben correr a
    la vez — la segunda espera a que la primera libere el semáforo.
    """
    manager = RuntimeManager()
    runtime = _SlowRuntime(max_parallel=1)
    manager.register("slow", runtime)

    t1 = threading.Thread(target=lambda: manager.execute("slow", ExecutionRequest(payload="first")))
    t1.start()
    time.sleep(0.2)  # le da tiempo a "first" de entrar y quedar bloqueada en release_first

    t2 = threading.Thread(target=lambda: manager.execute("slow", ExecutionRequest(payload="second")))
    t2.start()
    time.sleep(0.2)  # "second" debería estar esperando el semáforo, no haber arrancado

    assert "start:second" not in runtime.order

    runtime.release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert runtime.order == ["start:first", "end:first", "start:second", "end:second"]


def test_max_parallel_two_allows_two_concurrent_executions():
    manager = RuntimeManager()
    runtime = _SlowRuntime(max_parallel=2)
    manager.register("slow", runtime)

    t1 = threading.Thread(target=lambda: manager.execute("slow", ExecutionRequest(payload="first")))
    t1.start()
    time.sleep(0.2)

    t2 = threading.Thread(target=lambda: manager.execute("slow", ExecutionRequest(payload="second")))
    t2.start()
    time.sleep(0.2)

    # Con max_parallel=2, "second" SÍ debería poder arrancar mientras
    # "first" sigue bloqueada — a diferencia del test de arriba.
    assert "start:second" in runtime.order

    runtime.release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

"""
Tests de agent_core/orchestrator.py::_lifespan()/_pressure_check_loop().

BUG REAL ENCONTRADO EN USO (2026-08-28): resource_broker.evict_idle_and_pressured()
(el chequeo de RAM baja) solo se llamaba antes de un /chat o de cargar
un pipeline pesado — nunca por su cuenta. Dos caídas reales de sistema
pasaron con NINGÚN pedido nuevo a kal disparando ese chequeo mientras
la RAM se agotaba por otra causa. Este archivo prueba el thread de
background que ahora lo corre periódicamente, independiente del
tráfico real.

Todos los tests usan `with TestClient(app) as client:` a propósito —
es el único patrón que dispara lifespan/startup en FastAPI/Starlette
(verificado en vivo antes de este cambio: sin `with`, que es como
usan TestClient TODOS los demás archivos de test de este proyecto, el
thread nunca arranca — ver test_thread_never_starts_without_context_manager).
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from agent_core.orchestrator import app
from kernel.broker.resource_broker import resource_broker
from utils.config import settings


def test_thread_never_starts_without_context_manager():
    """
    El patrón real que usan los ~40 archivos de test de este proyecto
    (`TestClient(app, base_url=...)` sin `with`) — NUNCA debe arrancar
    el thread de background (llamadas de red reales a Ollama en cada
    test sería un regresión real de ruido/lentitud/flakiness).

    Verifica por NOMBRE de thread, no por conteo total: en una suite
    compartida de +1000 tests, otros threads ajenos a esta feature
    (pools de conexión, fixtures de otros archivos) pueden arrancar o
    terminar en cualquier momento — el conteo total no es una
    propiedad que este feature controle (BUG REAL ENCONTRADO EN
    REVISIÓN: la primera versión de este test comparaba conteos y era
    flaky corriendo junto a otros archivos, aunque siempre pasaba solo).
    """
    client = TestClient(app, base_url="http://localhost")
    client.get("/health")
    assert "ram-pressure-check" not in [t.name for t in threading.enumerate()]


def test_thread_starts_with_context_manager_and_stops_cleanly_on_exit():
    with TestClient(app, base_url="http://localhost") as client:
        client.get("/health")
        assert "ram-pressure-check" in [t.name for t in threading.enumerate()]

    assert "ram-pressure-check" not in [t.name for t in threading.enumerate()]


def test_thread_calls_evict_idle_and_pressured_periodically(monkeypatch):
    calls = []
    monkeypatch.setattr(resource_broker, "evict_idle_and_pressured", lambda: calls.append(1))
    monkeypatch.setattr(settings.resource_broker, "pressure_check_interval_seconds", 0.02)

    with TestClient(app, base_url="http://localhost") as client:
        client.get("/health")
        time.sleep(0.15)  # unos cuantos ciclos de 0.02s

    assert len(calls) >= 3


def test_thread_survives_a_transient_error_and_keeps_running(monkeypatch):
    """
    Un fallo transitorio (p.ej. Ollama caído un instante) nunca debe
    matar el thread — el próximo ciclo lo vuelve a intentar solo.
    """
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Ollama no responde (transitorio)")

    monkeypatch.setattr(resource_broker, "evict_idle_and_pressured", _flaky)
    monkeypatch.setattr(settings.resource_broker, "pressure_check_interval_seconds", 0.02)

    with TestClient(app, base_url="http://localhost") as client:
        client.get("/health")
        time.sleep(0.15)

    assert calls["n"] >= 3  # siguió corriendo después del primer fallo


def test_pressure_check_stop_event_is_reset_between_uses():
    """
    _pressure_check_stop es un Event a nivel de módulo, compartido entre
    corridas — si un `with` anterior lo dejara "set", el próximo thread
    vería la señal de parada ya activa y terminaría inmediatamente sin
    correr ningún ciclo. Confirma que dos usos consecutivos del
    context manager arrancan el thread correctamente los dos.
    """
    with TestClient(app, base_url="http://localhost") as client:
        client.get("/health")
        assert "ram-pressure-check" in [t.name for t in threading.enumerate()]

    with TestClient(app, base_url="http://localhost") as client:
        client.get("/health")
        assert "ram-pressure-check" in [t.name for t in threading.enumerate()]

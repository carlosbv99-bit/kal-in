"""
Tests de kernel/api/sandbox_api.py — el servicio HTTP interno que
ejecuta código dentro del sandbox.

VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
2026-09-26, C-3): /execute no tenía NINGUNA autenticación. Estos tests
cubren ese fix (fail-closed sin secreto configurado, rechazo de
secreto incorrecto/ausente/no-ASCII sin 500, ver M-6) y el de M-2 en
kal (auditoría externa de kal, 2026-09-27, portado acá vía
scripts/check_kernel_drift.py): límite de tamaño de body y manejo de
un Content-Length no numérico, que antes no existían en absoluto acá.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from kernel.lifecycle.docker_runner import SandboxResult


class _FakeRunner:
    """Doble de prueba: nunca toca Docker real."""

    def run(self, source_code, **kwargs):
        return SandboxResult(status="success", stdout="ok", stderr="", exit_code=0)


@pytest.fixture
def sandbox_api_module(monkeypatch):
    """
    Recarga el módulo con SANDBOX_RUNNER_SECRET ya seteado en el
    entorno ANTES del import — el módulo lo lee una sola vez, a nivel
    de módulo, al arrancar (mismo patrón que SANDBOX_IMAGE en
    docker_runner.py).
    """
    monkeypatch.setenv("SANDBOX_RUNNER_SECRET", "el-secreto-correcto")
    import kernel.api.sandbox_api as mod

    importlib.reload(mod)
    mod.executor.runner = _FakeRunner()
    yield mod
    monkeypatch.delenv("SANDBOX_RUNNER_SECRET", raising=False)
    importlib.reload(mod)


@pytest.fixture
def client(sandbox_api_module):
    return TestClient(sandbox_api_module.app)


def test_health_needs_no_secret(client):
    response = client.get("/health")
    assert response.status_code == 200


def test_execute_without_secret_is_rejected(client):
    response = client.post("/execute", json={"source_code": "print('hola')"})
    assert response.status_code == 403


def test_execute_with_wrong_secret_is_rejected(client):
    response = client.post(
        "/execute", json={"source_code": "print('hola')"}, headers={"x-sandbox-secret": "incorrecto"}
    )
    assert response.status_code == 403


def test_execute_with_correct_secret_succeeds(client):
    response = client.post(
        "/execute",
        json={"source_code": "print('hola')"},
        headers={"x-sandbox-secret": "el-secreto-correcto"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_non_ascii_secret_is_rejected_without_a_500(sandbox_api_module):
    """
    M-6 (auditoría externa Likay-OS, 2026-09-26): secrets.compare_digest
    con un string no-ASCII lanza TypeError si se compara directo — acá
    se compara como bytes, así que debe rechazarse limpio, nunca con
    una excepción sin atrapar.

    Prueba `require_sandbox_secret` directo, sin pasar por
    TestClient/httpx2: la librería cliente rechaza de por sí mandar un
    header no-ASCII (ValueError/UnicodeEncodeError del lado cliente),
    pero un cliente crudo/adversarial que arme el request a mano no
    tiene esa restricción — ASGI trata los headers como bytes, y
    Starlette los decodifica como latin-1, así que un byte no-ASCII SÍ
    puede llegar acá como un str con caracteres no-ASCII.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        sandbox_api_module.require_sandbox_secret(x_sandbox_secret="tóken-ñoño")

    assert exc_info.value.status_code == 403


def test_execute_rejected_when_secret_env_var_is_not_configured(monkeypatch):
    """Fail-closed: sin SANDBOX_RUNNER_SECRET en el entorno, todo pedido
    se rechaza — nunca "abierto por default" por un despliegue que se
    olvidó de configurar el secreto."""
    monkeypatch.delenv("SANDBOX_RUNNER_SECRET", raising=False)
    import kernel.api.sandbox_api as mod

    importlib.reload(mod)
    mod.executor.runner = _FakeRunner()
    unauthenticated_client = TestClient(mod.app)

    response = unauthenticated_client.post(
        "/execute", json={"source_code": "print('hola')"}, headers={"x-sandbox-secret": "cualquier-cosa"}
    )

    assert response.status_code == 403
    importlib.reload(mod)


# --- M-2 en kal (auditoría externa de kal, 2026-09-27, portado acá vía
# scripts/check_kernel_drift.py): sin límite de tamaño de body, y sin
# manejo de un Content-Length no numérico ---


def test_execute_rejects_a_body_larger_than_the_limit(client, sandbox_api_module):
    huge_source = "x" * (sandbox_api_module._MAX_BODY_BYTES + 1)
    response = client.post(
        "/execute",
        json={"source_code": huge_source},
        headers={"x-sandbox-secret": "el-secreto-correcto"},
    )
    assert response.status_code == 413


def test_non_numeric_content_length_is_rejected_without_a_500(sandbox_api_module):
    """
    Un Content-Length no numérico (b"not-a-number") hacía que int()
    lanzara ValueError SIN ATRAPAR DENTRO DEL MIDDLEWARE — un crash
    alcanzable ANTES de la autenticación por cualquiera que llegue al
    puerto (un middleware ASGI corre antes que las Depends() de la
    ruta). Se prueba el middleware directo: la librería cliente
    (httpx2/TestClient) no deja mandar un Content-Length arbitrario a
    mano, así que no alcanza para reproducir esto de punta a punta.
    """
    import asyncio

    sent = {}

    async def fake_app(scope, receive, send):
        sent["reached_app"] = True

    async def fake_send(message):
        sent.setdefault("messages", []).append(message)

    middleware = sandbox_api_module._MaxBodySizeMiddleware(fake_app, max_bytes=sandbox_api_module._MAX_BODY_BYTES)
    scope = {"type": "http", "headers": [(b"content-length", b"not-a-number")]}

    asyncio.run(middleware(scope, None, fake_send))

    assert "reached_app" not in sent
    status_messages = [m for m in sent["messages"] if m["type"] == "http.response.start"]
    assert status_messages[0]["status"] == 400


def test_a_legitimate_content_length_still_reaches_the_app(sandbox_api_module):
    """Ancla contra una regresión inversa: rechazar SIEMPRE que
    Content-Length esté presente, en vez de solo cuando es inválido o
    demasiado grande."""
    import asyncio

    sent = {}

    async def fake_app(scope, receive, send):
        sent["reached_app"] = True

    middleware = sandbox_api_module._MaxBodySizeMiddleware(fake_app, max_bytes=sandbox_api_module._MAX_BODY_BYTES)
    scope = {"type": "http", "headers": [(b"content-length", b"42")]}

    asyncio.run(middleware(scope, None, None))

    assert sent.get("reached_app") is True

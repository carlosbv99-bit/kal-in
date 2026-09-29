"""
API interna del servicio sandbox_runner.

El agente principal (agent_core) llama a este servicio vía HTTP en vez
de invocar Docker directamente, para que solo este proceso aislado
tenga acceso al socket de Docker del host (ver docker-compose.yml).
"""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from kernel.lifecycle.executor import SandboxExecutor

# Mismo hallazgo que el secreto de abajo (C-3): sin límite de tamaño de
# body. 1 MiB es de sobra para el código fuente real de una skill/
# herramienta dinámica — un body más grande no tiene ningún caso de
# uso legítimo hoy.
_MAX_BODY_BYTES = 1024 * 1024


class _MaxBodySizeMiddleware:
    """
    Rechaza temprano por `Content-Length` declarado, antes de que
    FastAPI/Pydantic lean el body completo a memoria. Corre ANTES que
    cualquier dependencia de la ruta (incluida `require_sandbox_secret`
    de abajo) — un middleware ASGI siempre corre antes del routing/las
    dependencias, nunca después — así que esto es una defensa PRE-AUTH,
    alcanzable por cualquiera que llegue al puerto, no solo por quien ya
    tiene el secreto. No cubre un cliente que mienta el header o
    transmita en streaming sin Content-Length — deny-by-default en el
    caso común, no una defensa exhaustiva.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            content_length = headers.get(b"content-length")
            if content_length is not None:
                # BUG REAL DE AUDITORÍA EXTERNA (M-2 en kal, 2026-09-27,
                # portado acá vía scripts/check_kernel_drift.py): un
                # Content-Length no numérico (p.ej. b"not-a-number")
                # hacía que int() lanzara ValueError SIN ATRAPAR acá —
                # un crash alcanzable por cualquiera que llegue al
                # puerto, ANTES de la autenticación (ver el comentario
                # de arriba). Tratado igual que un body demasiado
                # grande: rechazo limpio, nunca un 500.
                try:
                    declared_size = int(content_length)
                except ValueError:
                    response = JSONResponse({"detail": "Content-Length inválido"}, status_code=400)
                    await response(scope, receive, send)
                    return
                if declared_size > self.max_bytes:
                    response = JSONResponse({"detail": "Body demasiado grande"}, status_code=413)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app = FastAPI(title="Sandbox Runner")
app.add_middleware(_MaxBodySizeMiddleware, max_bytes=_MAX_BODY_BYTES)
executor = SandboxExecutor()

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), C-3: /execute corría código arbitrario (Python, dentro
# de un contenedor Docker efímero — pero código arbitrario al fin) sin
# NINGUNA autenticación, alcanzable desde cualquier otro servicio de
# `agent_net` (la misma red compartida que el contenedor `agent` y
# `docker_socket_proxy`, ver docker-compose.yml). Secreto compartido
# vía variable de entorno (mismo valor en `agent` y `sandbox_runner`,
# ver docker-compose.yml) — fail closed: si SANDBOX_RUNNER_SECRET no
# está configurada, TODO pedido a /execute se rechaza, nunca se
# arranca "abierto" por descuido de configuración.
_SANDBOX_RUNNER_SECRET = os.environ.get("SANDBOX_RUNNER_SECRET")


def require_sandbox_secret(x_sandbox_secret: str | None = Header(default=None)) -> None:
    # M-6 (auditoría externa Likay-OS, 2026-09-26): comparar como bytes
    # evita el TypeError de compare_digest() sobre str no-ASCII — ver
    # el mismo fix en agent_core/orchestrator.py::require_admin_token().
    if not _SANDBOX_RUNNER_SECRET or x_sandbox_secret is None or not secrets.compare_digest(
        x_sandbox_secret.encode("utf-8", errors="replace"), _SANDBOX_RUNNER_SECRET.encode("utf-8")
    ):
        raise HTTPException(
            status_code=403,
            detail="Secreto inválido o ausente (header X-Sandbox-Secret) — o SANDBOX_RUNNER_SECRET no está configurada.",
        )


class ExecuteRequest(BaseModel):
    source_code: str
    context: dict = Field(default_factory=dict)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/execute", dependencies=[Depends(require_sandbox_secret)])
def execute(req: ExecuteRequest):
    result = executor.execute(req.source_code, req.context)
    return {
        "status": result.status,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "resource_usage": result.resource_usage,
    }

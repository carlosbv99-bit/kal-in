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
from pydantic import BaseModel

from kernel.lifecycle.executor import SandboxExecutor

app = FastAPI(title="Sandbox Runner")
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
    context: dict = {}


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

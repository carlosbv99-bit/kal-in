"""
Integraciones de IDE: /integrations/vscode/*.

v1 escopado: solo VS Code, sin instalar VS Code mismo (se asume ya
instalado) ni protocolo de handshake — la extensión ya habla HTTP
simple contra esta misma API. Ver agent_core/vscode_integration.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agent_core.orchestrator import require_admin_token
from agent_core.vscode_integration import VSCodeIntegrationError, install_extension
from agent_core.vscode_integration import get_status as get_vscode_status

router = APIRouter(prefix="/integrations/vscode", tags=["Integración VS Code"])


@router.get("/status")
def vscode_integration_status():
    return get_vscode_status()


@router.post("/install", dependencies=[Depends(require_admin_token)])
def vscode_integration_install():
    try:
        message = install_extension()
    except VSCodeIntegrationError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"message": message}

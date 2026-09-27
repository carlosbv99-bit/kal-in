"""
Adaptador de filesystem sobre kernel/permissions/access_manager.py::AccessManager
— decide si una acción concreta (create/read/modify/delete/rename)
sobre un alcance concreto (workspace/home/external) se auto-permite o
necesita un humano, deja auditoría de cada decisión, y recuerda
concesiones ya otorgadas para no volver a preguntar.

ORTOGONAL a kernel/permissions/permission_cascade.py::PermissionCascade:
aquella decide "¿esta herramienta puede pedir tocar el filesystem en
absoluto?" (FILESYSTEM_READ/WRITE por nivel de confianza, chequeada
ANTES de invocar cualquier handler). Esta decide, para una herramienta
que YA tiene FILESYSTEM_WRITE, "¿esta acción concreta necesita que un
humano la apruebe, o ya está cubierta por la política/una concesión
previa?".

NUNCA ejecuta la escritura real — eso es responsabilidad de quien
consulta esta decisión (para el agente IDE de VS Code, la extensión
misma, vía vscode.workspace.fs; el backend de Python jamás tiene acceso
al workspace real de un editor externo). El rol de este módulo es
puramente de POLÍTICA + AUDITORÍA + memoria de concesiones — ahora
delegada al motor genérico compartido con
kernel/permissions/network_access_manager.py (el otro adaptador real),
en vez de una implementación propia duplicada.

Separado de kernel/permissions/filesystem_permissions.py por el mismo
motivo que permission_cascade.py está separado de permissions.py: este
módulo depende de utils.config/audit_log, así que NUNCA se envía a un
contenedor de skill.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kernel.permissions.access_manager import (
    AccessManager,
    AccessManagerError,
    PendingAccessRequest,
)
from kernel.permissions.filesystem_permissions import FilesystemAction, FilesystemScope
from utils.config import settings

FilesystemAccessDecision = Literal["auto_allowed", "requires_approval"]
GrantLevel = Literal["once", "session", "project", "skill"]

_GRANTS_PATH = Path("data/keys/filesystem_grants.json")


class FilesystemAccessError(Exception):
    """El id pedido no corresponde a ninguna solicitud pendiente conocida."""


@dataclass
class PendingFilesystemAccess:
    """Misma forma pública que antes de introducir el motor genérico —
    scope/action tipados como enum para quien ya dependa de eso."""
    id: str
    skill_name: str
    scope: FilesystemScope
    action: FilesystemAction
    resource_key: str
    status: str = "pending_approval"

    @classmethod
    def _from_generic(cls, request: PendingAccessRequest) -> PendingFilesystemAccess:
        return cls(
            id=request.id, skill_name=request.skill_name, scope=FilesystemScope(request.scope),
            action=FilesystemAction(request.action), resource_key=request.resource_key, status=request.status,
        )


def _is_policy_auto_allowed(scope: str, action: str, _resource_key: str) -> bool:
    """
    Fail-safe por diseño: cualquier combinación scope/acción que NO
    esté listada en `settings.filesystem_access.auto_allow` requiere
    aprobación humana — nunca al revés. Ignora `resource_key` a
    propósito: a diferencia de la política de red (que sí depende del
    dominio concreto), acá cualquier archivo dentro del mismo alcance
    corre la misma política.
    """
    return action in settings.filesystem_access.auto_allow.get(scope, [])


class FilesystemAccessManager:
    def __init__(self, grants_path: Path | None = None):
        self._engine = AccessManager(
            resource_kind="filesystem",
            grants_path=grants_path or _GRANTS_PATH,
            is_auto_allowed=_is_policy_auto_allowed,
            event_type_prefix="filesystem_access",
        )

    def evaluate(
        self, skill_name: str, scope: FilesystemScope, action: FilesystemAction, resource_key: str
    ) -> FilesystemAccessDecision:
        return self._engine.evaluate(skill_name, scope.value, action.value, resource_key)

    def create_pending_request(
        self, skill_name: str, scope: FilesystemScope, action: FilesystemAction, resource_key: str
    ) -> PendingFilesystemAccess:
        request = self._engine.create_pending_request(skill_name, scope.value, action.value, resource_key)
        return PendingFilesystemAccess._from_generic(request)

    def list_pending(self) -> list[PendingFilesystemAccess]:
        return [PendingFilesystemAccess._from_generic(p) for p in self._engine.list_pending()]

    def approve(self, request_id: str, level: GrantLevel) -> None:
        try:
            self._engine.approve(request_id, level)
        except AccessManagerError as e:
            raise FilesystemAccessError(str(e)) from e

    def deny(self, request_id: str) -> None:
        try:
            self._engine.deny(request_id)
        except AccessManagerError as e:
            raise FilesystemAccessError(str(e)) from e


# Singleton, mismo patrón que tool_registry/audit_log/kernel/resource_broker.
filesystem_access_manager = FilesystemAccessManager()

"""
Adaptador de red sobre kernel/permissions/access_manager.py::AccessManager
— segundo adaptador real (el primero es
kernel/permissions/filesystem_access_manager.py). Decide si conectarse a
un dominio concreto, para una acción concreta (browse/download), se
auto-permite por política o requiere aprobación humana — con las
mismas 4 escalas de concesión persistentes y la misma auditoría que
filesystem, pero para red.

Antes de este módulo, un dominio no listado en
config.yaml (browser.allowed_domains / downloads.allowed_domains) se
rechazaba con un error inmediato, sin ningún camino de escalar a un
humano ni de recordar una concesión — el único hueco real entre los
tres mecanismos de permisos existentes (ver
kernel/permissions/permission_cascade.py y
kernel/permissions/filesystem_access_manager.py para los otros dos).

`resource_key` es el HOSTNAME (no la URL completa) — lo extrae el
llamador antes de invocar evaluate(), igual que filesystem usa una
ruta de archivo como resource_key.

Reusa la política YA existente (`downloads.allowed_domains` para la
acción DOWNLOAD) — sin ninguna config nueva.
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
from kernel.permissions.network_permissions import NetworkAction, NetworkScope
from kernel.permissions.network_safety import is_hostname_allowed
from utils.config import settings

NetworkAccessDecision = Literal["auto_allowed", "requires_approval"]
GrantLevel = Literal["once", "session", "project", "skill"]

_GRANTS_PATH = Path("data/keys/network_grants.json")


class NetworkAccessError(Exception):
    """El id pedido no corresponde a ninguna solicitud pendiente conocida."""


@dataclass
class PendingNetworkAccess:
    id: str
    skill_name: str
    scope: NetworkScope
    action: NetworkAction
    resource_key: str
    status: str = "pending_approval"

    @classmethod
    def _from_generic(cls, request: PendingAccessRequest) -> PendingNetworkAccess:
        return cls(
            id=request.id, skill_name=request.skill_name, scope=NetworkScope(request.scope),
            action=NetworkAction(request.action), resource_key=request.resource_key, status=request.status,
        )


def _is_policy_auto_allowed(_scope: str, action: str, resource_key: str) -> bool:
    """
    A diferencia de filesystem (que ignora resource_key: cualquier
    archivo en el mismo alcance corre la misma política), acá el
    dominio concreto (resource_key) SÍ importa — es exactamente lo que
    decide la allowlist. BROWSE reusa `browser.allowed_domains`
    (adoptado por tool_integration/adapters/browser.py::BrowserTool,
    que antes rechazaba con un error duro sin ningún camino de
    escalar); DOWNLOAD reusa `downloads.allowed_domains` — sin ninguna
    config nueva para ninguno de los dos.
    """
    if action == NetworkAction.DOWNLOAD.value:
        return is_hostname_allowed(resource_key, settings.downloads.allowed_domains)
    if action == NetworkAction.BROWSE.value:
        return is_hostname_allowed(resource_key, settings.browser.allowed_domains)
    return False


class NetworkAccessManager:
    def __init__(self, grants_path: Path | None = None):
        self._engine = AccessManager(
            resource_kind="network",
            grants_path=grants_path or _GRANTS_PATH,
            is_auto_allowed=_is_policy_auto_allowed,
            event_type_prefix="network_access",
        )

    def evaluate(
        self, skill_name: str, scope: NetworkScope, action: NetworkAction, resource_key: str
    ) -> NetworkAccessDecision:
        return self._engine.evaluate(skill_name, scope.value, action.value, resource_key)

    def create_pending_request(
        self, skill_name: str, scope: NetworkScope, action: NetworkAction, resource_key: str
    ) -> PendingNetworkAccess:
        request = self._engine.create_pending_request(skill_name, scope.value, action.value, resource_key)
        return PendingNetworkAccess._from_generic(request)

    def list_pending(self) -> list[PendingNetworkAccess]:
        return [PendingNetworkAccess._from_generic(p) for p in self._engine.list_pending()]

    def approve(self, request_id: str, level: GrantLevel) -> None:
        try:
            self._engine.approve(request_id, level)
        except AccessManagerError as e:
            raise NetworkAccessError(str(e)) from e

    def deny(self, request_id: str) -> None:
        try:
            self._engine.deny(request_id)
        except AccessManagerError as e:
            raise NetworkAccessError(str(e)) from e


# Singleton, mismo patrón que filesystem_access_manager/tool_registry/audit_log.
network_access_manager = NetworkAccessManager()

"""
Motor genérico de arbitraje de acceso a CUALQUIER recurso del Kernel —
política + escalamiento a un humano + concesiones persistentes (4
escalas) + auditoría. Decide si una Skill+Acción+Recurso concreto se
auto-permite o requiere aprobación humana, recuerda concesiones ya
otorgadas para no volver a preguntar, y audita cada decisión.

Deliberadamente NO sabe nada de filesystem ni de red: `scope`/`action`
son strings libres, y `is_auto_allowed` es un callback que el
ADAPTADOR concreto inyecta (nunca lee `utils.config` directamente acá)
— así, un adaptador nuevo (Terminal, Modelos, lo que sea) reusa este
motor tal cual, sin que este archivo necesite saber que existe.

`kernel/permissions/filesystem_access_manager.py` y
`kernel/permissions/network_access_manager.py` son los dos primeros
ADAPTADORES: cada uno le da su propio vocabulario tipado (enums de
Scope/Action) y su propia fuente de política, pero el motor de
decisión/concesión/auditoría es el MISMO objeto, no una copia.

ORTOGONAL a kernel/permissions/permission_cascade.py::PermissionCascade:
aquella decide "¿esta herramienta puede pedir esta CAPACIDAD en
absoluto?" (por nivel de confianza, ANTES de invocar cualquier
handler). Esta decide, para una herramienta que YA tiene esa
capacidad, "¿esta acción concreta sobre este recurso concreto se
auto-permite, o necesita que un humano la apruebe?".
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from audit.audit_log import AuditEvent, audit_log

AccessDecision = Literal["auto_allowed", "requires_approval"]
GrantLevel = Literal["once", "session", "project", "skill"]

IsAutoAllowed = Callable[[str, str, str], bool]  # (scope, action, resource_key) -> bool


class AccessManagerError(Exception):
    """El id pedido no corresponde a ninguna solicitud pendiente conocida."""


@dataclass
class PendingAccessRequest:
    id: str
    skill_name: str
    scope: str
    action: str
    resource_key: str
    status: str = "pending_approval"  # "pending_approval" | "approved" | "denied"


@dataclass
class _Grant:
    skill_name: str
    scope: str
    action: str
    resource_key: str | None  # None = aplica a cualquier resource_key de esta skill/scope/acción ("skill" level)


def _grant_matches(grant: _Grant, skill_name: str, scope: str, action: str, resource_key: str) -> bool:
    if grant.skill_name != skill_name or grant.scope != scope or grant.action != action:
        return False
    return grant.resource_key is None or grant.resource_key == resource_key


class AccessManager:
    def __init__(
        self,
        resource_kind: str,
        grants_path: Path,
        is_auto_allowed: IsAutoAllowed,
        event_type_prefix: str,
    ):
        self._resource_kind = resource_kind
        self._grants_path = grants_path
        self._is_auto_allowed = is_auto_allowed
        self._event_type_prefix = event_type_prefix
        self._session_grants: list[_Grant] = []
        self._pending: dict[str, PendingAccessRequest] = {}

    # --- Decisión ---

    def evaluate(self, skill_name: str, scope: str, action: str, resource_key: str) -> AccessDecision:
        """
        Fail-safe por diseño: si nada dice explícitamente que esto se
        auto-permite (política inyectada o una concesión previa),
        requiere aprobación — nunca al revés.
        """
        self._audit(
            "requested", f"'{skill_name}' pide {action} en alcance {scope} ({resource_key})",
            skill_name, scope, action, resource_key, outcome="pending",
        )

        if self._is_auto_allowed(scope, action, resource_key) or self._has_grant(skill_name, scope, action, resource_key):
            self._audit(
                "granted", f"'{skill_name}' autorizado automáticamente: {action} en {scope} ({resource_key})",
                skill_name, scope, action, resource_key, outcome="success",
            )
            return "auto_allowed"

        self._audit(
            "escalated", f"'{skill_name}' requiere aprobación humana: {action} en {scope} ({resource_key})",
            skill_name, scope, action, resource_key, outcome="escalated",
        )
        return "requires_approval"

    def _has_grant(self, skill_name: str, scope: str, action: str, resource_key: str) -> bool:
        grants = self._session_grants + self._load_persisted_grants()
        return any(_grant_matches(g, skill_name, scope, action, resource_key) for g in grants)

    # --- Solicitudes pendientes (cuando evaluate() da requires_approval) ---

    def create_pending_request(self, skill_name: str, scope: str, action: str, resource_key: str) -> PendingAccessRequest:
        pending = PendingAccessRequest(
            id=str(uuid4()), skill_name=skill_name, scope=scope, action=action, resource_key=resource_key
        )
        self._pending[pending.id] = pending
        return pending

    def list_pending(self) -> list[PendingAccessRequest]:
        return [p for p in self._pending.values() if p.status == "pending_approval"]

    def approve(self, request_id: str, level: GrantLevel) -> None:
        pending = self._get_pending(request_id)
        pending.status = "approved"
        self._grant(pending.skill_name, pending.scope, pending.action, pending.resource_key, level)
        self._audit(
            "granted",
            f"Aprobado por humano ({level}): '{pending.skill_name}' — {pending.action} en "
            f"{pending.scope} ({pending.resource_key})",
            pending.skill_name, pending.scope, pending.action, pending.resource_key, outcome="success",
        )

    def deny(self, request_id: str) -> None:
        pending = self._get_pending(request_id)
        pending.status = "denied"
        self._audit(
            "denied",
            f"Denegado por humano: '{pending.skill_name}' — {pending.action} en "
            f"{pending.scope} ({pending.resource_key})",
            pending.skill_name, pending.scope, pending.action, pending.resource_key, outcome="failure",
        )

    def _get_pending(self, request_id: str) -> PendingAccessRequest:
        pending = self._pending.get(request_id)
        if pending is None:
            raise AccessManagerError(
                f"No existe una solicitud de acceso a {self._resource_kind} pendiente con id '{request_id}'."
            )
        return pending

    # --- Concesiones (grants) ---

    def _grant(self, skill_name: str, scope: str, action: str, resource_key: str, level: GrantLevel) -> None:
        if level == "once":
            return  # nunca se recuerda — la próxima vez vuelve a preguntar
        if level == "session":
            self._session_grants.append(_Grant(skill_name, scope, action, resource_key))
            return
        if level == "project":
            self._persist_grant(_Grant(skill_name, scope, action, resource_key))
            return
        if level == "skill":
            self._persist_grant(_Grant(skill_name, scope, action, resource_key=None))
            return
        raise ValueError(f"Nivel de concesión desconocido: '{level}'")

    def _load_persisted_grants(self) -> list[_Grant]:
        if not self._grants_path.exists():
            return []
        raw = json.loads(self._grants_path.read_text(encoding="utf-8"))
        return [_Grant(**g) for g in raw]

    def _persist_grant(self, grant: _Grant) -> None:
        grants = self._load_persisted_grants()
        grants.append(grant)
        self._grants_path.parent.mkdir(parents=True, exist_ok=True)
        self._grants_path.write_text(json.dumps([g.__dict__ for g in grants], indent=2), encoding="utf-8")

    # --- Auditoría ---

    def _audit(
        self, event_suffix: str, summary: str, skill_name: str, scope: str, action: str,
        resource_key: str, outcome: str,
    ) -> None:
        audit_log.record(
            AuditEvent(
                event_type=f"{self._event_type_prefix}_{event_suffix}",  # type: ignore[arg-type]
                summary=summary,
                context={
                    "resource_kind": self._resource_kind, "skill_name": skill_name, "scope": scope,
                    "action": action, "resource_key": resource_key,
                },
                outcome=outcome,
            )
        )

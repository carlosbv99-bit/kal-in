"""
ToolRegistry: registro central de herramientas disponibles + pipeline
de validación para herramientas creadas dinámicamente por el agente.

Pipeline para una herramienta nueva propuesta por el agente (Fase 3):
  1. El agente propone código + ToolManifest (permisos declarados)
  2. Validación estática (code_analysis) sobre el código propuesto
  3. Ejecución de prueba en sandbox, SIN los permisos de red/fs que pida
     el manifiesto salvo que ya estén pre-aprobados (deny-by-default)
  4. Si requiere permisos sensibles (ver config.yaml:
     tool_integration.require_human_approval_for), se registra como
     "pending_approval" y NO se activa hasta revisión humana
  5. Solo si pasa (3) y no requiere (4), se registra como disponible

Ninguna herramienta se auto-promueve: register_dynamic_tool() es la
única puerta de entrada y siempre pasa por este pipeline completo.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from audit.audit_log import AuditEvent, audit_log
from code_analysis.ast_validator import validate_code
from kernel.lifecycle.executor import SandboxExecutor
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from sdk.permissions import Permission
from kernel.registry.signing import ToolSigner, tool_signer
from kernel.registry.versioning import ToolVersionStore, is_valid_tool_name, tool_version_store
from utils.config import settings
from utils.logger import get_logger

if TYPE_CHECKING:
    from kernel.registry.skills import SkillStatus

logger = get_logger(__name__)


@dataclass
class PendingTool:
    manifest: ToolManifest
    source_code: str
    status: str  # "pending_approval" | "rejected" | "active"
    reason: str = ""


def _manifest_to_dict(manifest: ToolManifest) -> dict:
    return {
        "name": manifest.name,
        "description": manifest.description,
        "requires_network": manifest.requires_network,
        "requires_filesystem_write": manifest.requires_filesystem_write,
        "allowed_domains": list(manifest.allowed_domains),
        "permissions": sorted(p.value for p in manifest.permissions),
        "created_by": manifest.created_by,
        "source_context": manifest.source_context,
    }


def _manifest_from_dict(data: dict) -> ToolManifest:
    return ToolManifest(
        name=data["name"],
        description=data["description"],
        requires_network=data.get("requires_network", False),
        requires_filesystem_write=data.get("requires_filesystem_write", False),
        allowed_domains=list(data.get("allowed_domains", [])),
        permissions=frozenset(Permission(p) for p in data.get("permissions", [])),
        created_by=data.get("created_by", "agent"),
        source_context=data.get("source_context", ""),
    )


class DynamicSandboxedTool(Tool):
    """
    Envoltorio que hace ejecutable una herramienta dinámica ya aprobada.

    Sin esto, "aprobar" una herramienta solo cambiaba un string de
    estado — no había forma de convertir el `source_code` (un string)
    en algo que `tool_registry.get(name).execute(...)` pudiera llamar.
    Cada ejecución corre `source_code` de nuevo en el sandbox (nunca en
    el proceso principal, ni siquiera para herramientas ya aprobadas),
    con los permisos que corresponda a lo que el manifiesto declaró y
    que ya pasó por el gate de aprobación humana si era necesario —
    calculados de nuevo en cada llamada a execute(), no una única vez
    al activar (scoping por ejecución, no un otorgamiento permanente).
    """

    def __init__(
        self,
        manifest: ToolManifest,
        source_code: str,
        sandbox: SandboxExecutor | None = None,
        version: int | None = None,
        signature: str = "",
    ):
        self.manifest = manifest
        self.source_code = source_code
        self.sandbox = sandbox or SandboxExecutor()
        self.version = version
        self.signature = signature

    def execute(self, **kwargs) -> Artifact:
        network_mode = "bridge" if Permission.NETWORK in self.manifest.permissions else None
        result = self.sandbox.execute(
            self.source_code,
            context={"tool_name": self.manifest.name, "dynamic": True, "version": self.version},
            network_mode=network_mode,
            granted_permissions=self.manifest.permissions,
        )
        return Artifact(
            modality="text",
            uri="",
            metadata={
                "tool_name": self.manifest.name,
                "status": result.status,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
            },
        )


class ToolRegistry:
    def __init__(
        self,
        sandbox: SandboxExecutor | None = None,
        signer: ToolSigner | None = None,
        version_store: ToolVersionStore | None = None,
    ):
        self._active_tools: dict[str, Tool] = {}
        self._pending: dict[str, PendingTool] = {}
        self.sandbox = sandbox or SandboxExecutor()
        self.signer = signer or tool_signer
        self.version_store = version_store or tool_version_store
        self._skill_statuses: list[SkillStatus] = []

    def load_skills(self, skills_dir: Path | None = None, image_builder=None) -> list[SkillStatus]:
        """
        Descubre y carga las skills instaladas bajo skills/ (o
        `skills_dir` si se pasa explícito, usado en tests). Import
        diferido para evitar el ciclo kernel.registry.skills <->
        kernel.registry.registry (skills.py necesita el tipo
        ToolRegistry, este método necesita load_skills real).

        `image_builder` inyectable (mismo motivo que `sandbox=` de este
        registry): solo hace falta para skills con `requirements`
        declarados, ver kernel/registry/skills.py::load_skills().
        """
        from kernel.registry.skills import DEFAULT_SKILLS_DIR, load_skills as _load_skills

        self._skill_statuses = _load_skills(
            self, skills_dir=skills_dir or DEFAULT_SKILLS_DIR, image_builder=image_builder
        )
        return self._skill_statuses

    def list_skills(self) -> list[dict]:
        return [
            {
                "skill_dir": s.skill_dir,
                "name": s.manifest.name if s.manifest else s.skill_dir,
                "description": s.manifest.description if s.manifest else "",
                "version": s.manifest.version if s.manifest else "",
                "status": s.status,
                "detail": s.detail,
                "permissions": s.manifest.permissions if s.manifest else [],
            }
            for s in self._skill_statuses
        ]

    def register_static_tool(self, tool: Tool) -> None:
        """Para herramientas predefinidas (adaptadores multimodales, etc.)."""
        self._active_tools[tool.manifest.name] = tool
        logger.info(f"Herramienta estática registrada: {tool.manifest.name}")

    def propose_dynamic_tool(self, manifest: ToolManifest, source_code: str) -> PendingTool:
        """
        Punto de entrada para que el agente proponga una herramienta nueva.
        Nunca activa la herramienta directamente — siempre pasa por el
        pipeline de validación completo.
        """
        # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
        # 2026-09-26), K-6: manifest.name (elegido por el LLM acá) se
        # usaba sin sanitizar más adelante en ToolVersionStore
        # (kernel/registry/versioning.py::_tool_dir(), llamado desde
        # _activate() -> save_version()) para armar una ruta de disco
        # — mismo patrón que K-1 en sandboxed_skill.py. Se rechaza acá,
        # PRIMERO, antes de gastar cómputo en validate_code()/una
        # corrida de sandbox de prueba que de todos modos terminaría
        # descartada al persistir.
        if not is_valid_tool_name(manifest.name):
            reason = (
                f"name '{manifest.name}' inválido — solo minúsculas, dígitos, '_' y '-', debe "
                "empezar con minúscula o dígito, máximo 64 caracteres."
            )
            pending = PendingTool(manifest, source_code, status="rejected", reason=reason)
            self._audit_tool_event("tool_created", manifest, "failure", reason)
            logger.warning(f"Herramienta '{manifest.name}' rechazada: {reason}")
            return pending

        validation = validate_code(source_code)
        if not validation.is_safe:
            reason = validation.syntax_error or "; ".join(validation.violations)
            pending = PendingTool(manifest, source_code, status="rejected", reason=reason)
            self._audit_tool_event("tool_created", manifest, "failure", reason)
            logger.warning(f"Herramienta '{manifest.name}' rechazada en validación estática: {reason}")
            return pending

        needs_approval = self._requires_human_approval(manifest)

        # Ejecución de prueba en sandbox: granted_permissions=frozenset()
        # a propósito, sin importar lo que pida el manifiesto — es una
        # corrida de prueba, no el otorgamiento real (eso solo ocurre
        # en DynamicSandboxedTool.execute(), tras activar).
        result = self.sandbox.execute(
            source_code, context={"tool_name": manifest.name}, granted_permissions=frozenset()
        )
        if result.status != "success":
            pending = PendingTool(manifest, source_code, status="rejected", reason=result.stderr)
            self._audit_tool_event("tool_created", manifest, "failure", result.stderr)
            return pending

        if needs_approval:
            pending = PendingTool(manifest, source_code, status="pending_approval")
            self._pending[manifest.name] = pending
            self._audit_tool_event("tool_created", manifest, "escalated", "Requiere aprobación humana")
            logger.info(f"Herramienta '{manifest.name}' pendiente de aprobación humana")
            return pending

        # Sin permisos sensibles y validación/prueba exitosas: se activa
        # de verdad (no solo se marca el status como "active").
        pending = PendingTool(manifest, source_code, status="active")
        version = self._activate(manifest, source_code)
        self._audit_tool_event(
            "tool_promoted", manifest, "success", f"Auto-aprobada, sin permisos sensibles (v{version})"
        )
        logger.info(f"Herramienta '{manifest.name}' activada automáticamente (v{version})")
        return pending

    def approve_pending_tool(self, name: str, approved_by: str) -> None:
        """Aprobación humana explícita para herramientas con permisos sensibles."""
        pending = self._pending.get(name)
        if pending is None:
            raise ValueError(f"No hay herramienta pendiente llamada {name}")
        if pending.status != "pending_approval":
            raise ValueError(f"La herramienta '{name}' no está pendiente de aprobación (status actual: {pending.status})")

        pending.status = "active"
        version = self._activate(pending.manifest, pending.source_code)
        del self._pending[name]  # ya no está pendiente, evita doble aprobación

        self._audit_tool_event(
            "tool_promoted", pending.manifest, "success", f"Aprobada por {approved_by} (v{version})"
        )
        logger.info(f"Herramienta '{name}' aprobada por {approved_by} y activada (v{version})")

    def _activate(self, manifest: ToolManifest, source_code: str) -> int:
        """
        Registra la herramienta dinámica como ejecutable de verdad:
        persiste esta versión en disco (kernel/registry/versioning.py),
        la firma (kernel/registry/signing.py) y solo entonces la deja
        invocable. Punto único usado tanto por la auto-aprobación como
        por approve_pending_tool(), para no duplicar la lógica.
        """
        version = self.version_store.next_version(manifest.name)
        signature = self.signer.sign(manifest.name, version, source_code)
        self.version_store.save_version(
            manifest.name, version, source_code, _manifest_to_dict(manifest), signature
        )
        self._active_tools[manifest.name] = DynamicSandboxedTool(
            manifest, source_code, sandbox=self.sandbox, version=version, signature=signature
        )
        return version

    def rollback_tool(self, name: str, to_version: int, approved_by: str) -> None:
        """
        Reactiva una versión anterior de una herramienta dinámica ya
        persistida. Verifica la firma de esa versión ANTES de
        reactivarla — si no coincide (el .py fue editado en disco fuera
        de este pipeline), rechaza el rollback en vez de correr código
        potencialmente alterado.
        """
        if name not in self._active_tools:
            raise ValueError(f"No hay herramienta activa llamada '{name}'")

        source_code, sidecar = self.version_store.read_version(name, to_version)
        signature = sidecar.get("signature", "")
        if not self.signer.verify(name, to_version, source_code, signature):
            audit_log.record(
                AuditEvent(
                    event_type="tool_tamper_detected",
                    summary=f"Versión {to_version} de '{name}' no pasa verificación de firma — rollback rechazado",
                    context={"tool_name": name, "version": to_version, "requested_by": approved_by},
                    outcome="failure",
                )
            )
            raise ValueError(
                f"La versión {to_version} de '{name}' no pasa la verificación de firma; rollback rechazado"
            )

        manifest = _manifest_from_dict(sidecar["manifest"])
        self._active_tools[name] = DynamicSandboxedTool(
            manifest, source_code, sandbox=self.sandbox, version=to_version, signature=signature
        )
        audit_log.record(
            AuditEvent(
                event_type="tool_rolled_back",
                summary=f"Herramienta '{name}' revertida a versión {to_version} por {approved_by}",
                context={"tool_name": name, "version": to_version, "approved_by": approved_by},
                outcome="success",
            )
        )
        logger.info(f"Herramienta '{name}' revertida a v{to_version} por {approved_by}")

    def verify_tool_integrity(self, name: str) -> bool:
        """
        Vuelve a leer del disco la versión actualmente activa de `name`
        y verifica su firma. A diferencia de rollback_tool (que verifica
        la versión DESTINO), esto detecta si la versión ACTIVA fue
        editada en disco después de activarse — herramientas estáticas
        (sin versión) siempre verifican True, no están bajo este esquema.
        """
        tool = self._active_tools.get(name)
        if not isinstance(tool, DynamicSandboxedTool) or tool.version is None:
            return True
        source_on_disk, sidecar = self.version_store.read_version(name, tool.version)
        return self.signer.verify(name, tool.version, source_on_disk, sidecar.get("signature", ""))

    def list_versions(self, name: str) -> list[int]:
        return self.version_store.list_versions(name)

    def _requires_human_approval(self, manifest: ToolManifest) -> bool:
        triggers = set(settings.tool_integration.require_human_approval_for)
        permission_values = {p.value for p in manifest.permissions}
        return bool(permission_values & triggers)

    def _audit_tool_event(self, event_type, manifest: ToolManifest, outcome: str, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type=event_type,
                summary=f"Herramienta '{manifest.name}': {detail}",
                context={
                    "tool_name": manifest.name,
                    "created_by": manifest.created_by,
                    "source_context": manifest.source_context,
                    "requires_network": manifest.requires_network,
                    "permissions": sorted(p.value for p in manifest.permissions),
                },
                outcome=outcome,
            )
        )

    def get(self, name: str) -> Tool | None:
        return self._active_tools.get(name)

    def active_tools(self) -> dict[str, Tool]:
        """
        Los objetos `Tool` reales (no el resumen de list_active()), para
        consumidores que necesitan invocarlos o leer su manifest
        completo (parameters_schema, permissions) — ver
        agent_core/llm/agent_loop.py, que arma el catálogo de
        herramientas del LLM a partir de esto.
        """
        return dict(self._active_tools)

    def list_active(self) -> list[dict]:
        """Para el dashboard del frontend — no expone el source_code completo."""
        return [
            {
                "name": name,
                "created_by": getattr(tool.manifest, "created_by", "system"),
                "description": tool.manifest.description,
                "permissions": sorted(p.value for p in tool.manifest.permissions),
                "version": getattr(tool, "version", None),
            }
            for name, tool in self._active_tools.items()
        ]

    def list_pending(self) -> list[dict]:
        return [
            {"name": name, "status": p.status, "created_by": p.manifest.created_by,
             "description": p.manifest.description, "requires_network": p.manifest.requires_network,
             "requires_filesystem_write": p.manifest.requires_filesystem_write,
             "permissions": sorted(perm.value for perm in p.manifest.permissions)}
            for name, p in self._pending.items()
        ]


tool_registry = ToolRegistry()


# El registro de las herramientas de primera parte (multimodales,
# navegador, etc.) vive en agent_core/default_tools.py, no acá — decidir
# QUÉ herramientas concretas existen por defecto es política de agente,
# no mecanismo de kernel. Ver ese archivo para el detalle; lo llama
# agent_core/orchestrator.py al arrancar. Esta clase (ToolRegistry) queda
# como mecanismo puro: no importa nada de tool_integration/agent_core.

"""
SandboxExecutor: punto de entrada único para ejecutar código no confiable.

Combina las capas de defensa en profundidad:
  1. Validación estática (code_analysis)  -> descarta lo obvio, barato
  2. Ejecución aislada (docker_runner)     -> contiene lo que se escape de (1)
  3. Registro de auditoría                 -> todo queda trazado, pase o falle

Ningún otro módulo del proyecto debe llamar a DockerSandboxRunner
directamente — siempre a través de esta clase, para garantizar que la
validación y la auditoría ocurren siempre, sin excepción ni olvido.
"""
from __future__ import annotations

from audit.audit_log import AuditEvent, audit_log
from code_analysis.ast_validator import validate_code
from kernel.lifecycle.docker_runner import DockerSandboxRunner, SandboxResult
from sdk.permissions import UNSUPPORTED_RUNTIME_PERMISSIONS, Permission
from utils.logger import get_logger

logger = get_logger(__name__)


class SandboxExecutor:
    def __init__(self, runner: DockerSandboxRunner | None = None):
        # Permite inyectar un runner falso en tests (evita requerir un
        # daemon de Docker real solo para probar la lógica de esta
        # clase, p.ej. el gate de permisos no soportados más abajo).
        self.runner = runner if runner is not None else DockerSandboxRunner()

    def execute(
        self,
        source_code: str,
        context: dict | None = None,
        network_mode: str | None = None,
        image: str | None = None,
        granted_permissions: frozenset[Permission] | None = None,
    ) -> SandboxResult:
        context = context or {}
        granted_permissions = granted_permissions or frozenset()

        unsupported = granted_permissions & UNSUPPORTED_RUNTIME_PERMISSIONS
        if unsupported:
            reason = (
                "Permiso(s) declarado(s) sin motor de aplicación real todavía: "
                + ", ".join(sorted(p.value for p in unsupported))
            )
            logger.warning(f"Ejecución rechazada — {reason}")
            audit_log.record(
                AuditEvent(
                    event_type="sandbox_execution",
                    summary="Ejecución rechazada: permiso sin motor de enforcement real",
                    context={**context, "unsupported_permissions": sorted(p.value for p in unsupported)},
                    outcome="failure",
                )
            )
            return SandboxResult(status="error", stdout="", stderr=reason, exit_code=None, resource_usage={})

        validation = validate_code(source_code)
        if not validation.is_safe:
            reason = validation.syntax_error or "; ".join(validation.violations)
            logger.warning(f"Código rechazado en validación estática: {reason}")
            audit_log.record(
                AuditEvent(
                    event_type="sandbox_execution",
                    summary="Código rechazado antes de ejecutar (validación estática)",
                    context={**context, "violations": validation.violations, "syntax_error": validation.syntax_error},
                    outcome="failure",
                )
            )
            return SandboxResult(
                status="error", stdout="", stderr=f"Validación estática falló: {reason}",
                exit_code=None, resource_usage={},
            )

        result = self.runner.run(source_code, image=image, network_mode=network_mode)

        audit_log.record(
            AuditEvent(
                event_type="sandbox_execution",
                summary="Ejecución de código en sandbox",
                context={**context, "exit_code": result.exit_code},
                outcome="success" if result.status == "success" else "failure",
            )
        )
        return result

    def execute_trusted(
        self,
        source_code: str,
        workspace_files: dict[str, str | bytes] | None = None,
        context: dict | None = None,
        network_mode: str | None = None,
        image: str | None = None,
        output_dir: str | None = None,
        granted_permissions: frozenset[Permission] | None = None,
        extra_mounts: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        memory_limit_mb: int | None = None,
        cpu_limit: float | None = None,
        pids_limit: int | None = None,
    ) -> SandboxResult:
        """
        Igual que execute() (siempre audita, siempre respeta
        UNSUPPORTED_RUNTIME_PERMISSIONS, el aislamiento real sigue siendo
        Docker) pero SIN pasar `source_code` por validate_code().

        Por qué es seguro saltarse el denylist SOLO acá: ese denylist
        existe para código NO CONFIABLE (propuesto por el LLM/el agente,
        ver registry.py::propose_dynamic_tool) — bloquea os/importlib/etc.
        pensando en ESE caso. `execute_trusted` es para código de PRIMERA
        PARTE, versionado en este repo (p.ej. kernel/lifecycle/skill_runner.py: el
        runner que ejecuta una skill ya habilitada por un humano dentro
        del contenedor) — ese script NECESITA os/importlib legítimamente
        para hacer su trabajo (importar el módulo de la skill, manejar
        rutas), igual que el propio docker_runner.py los usa sin pasar
        por su propio denylist. Pasar este script por validate_code() lo
        rechazaría por error de categoría: confundiría "confiable, con
        capacidades reales" con "no confiable, hay que restringir".
        Nunca usar este método con código que no sea 100% de este repo.
        """
        context = context or {}
        granted_permissions = granted_permissions or frozenset()

        unsupported = granted_permissions & UNSUPPORTED_RUNTIME_PERMISSIONS
        if unsupported:
            reason = (
                "Permiso(s) declarado(s) sin motor de aplicación real todavía: "
                + ", ".join(sorted(p.value for p in unsupported))
            )
            logger.warning(f"Ejecución de skill rechazada — {reason}")
            audit_log.record(
                AuditEvent(
                    event_type="sandbox_execution",
                    summary="Ejecución de skill rechazada: permiso sin motor de enforcement real",
                    context={**context, "unsupported_permissions": sorted(p.value for p in unsupported)},
                    outcome="failure",
                )
            )
            return SandboxResult(status="error", stdout="", stderr=reason, exit_code=None, resource_usage={})

        result = self.runner.run(
            source_code, workspace_files=workspace_files, image=image,
            network_mode=network_mode, output_dir=output_dir, extra_mounts=extra_mounts,
            timeout_seconds=timeout_seconds, memory_limit_mb=memory_limit_mb,
            cpu_limit=cpu_limit, pids_limit=pids_limit,
        )

        audit_log.record(
            AuditEvent(
                event_type="sandbox_execution",
                summary="Ejecución de skill en sandbox (script de confianza, sin validación AST)",
                context={**context, "exit_code": result.exit_code},
                outcome="success" if result.status == "success" else "failure",
            )
        )
        return result

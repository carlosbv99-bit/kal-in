"""
Tool que ejecuta una skill de terceros (skills/<nombre>/) de verdad
aislada — cada llamada a execute() corre DENTRO de un contenedor Docker
efímero (kernel/lifecycle/skill_runner.py), nunca en el proceso principal.

Antes de esto, kernel/registry/skills.py::load_skills() instanciaba la
clase real de la skill y la registraba tal cual — cualquier .execute()
posterior corría en el mismo proceso que el resto de kal, sin ningún
confinamiento (la única barrera era el enabled:false por defecto del
manifiesto, revisado por un humano antes de activarla). Este Tool
reemplaza esa instancia real: el registry nunca vuelve a tocar el
código de la skill directamente.

Reutiliza el mismo mapeo permiso->network_mode que
kernel/registry/registry.py::DynamicSandboxedTool, y el mismo
SandboxExecutor (kernel/lifecycle/executor.py) — pero vía execute_trusted(), no
execute(), porque el runner (kernel/lifecycle/skill_runner.py) es código de
primera parte que necesita os/importlib, y el denylist de
code_analysis/ está pensado para código de un tercero no confiable, no
para nuestra propia infraestructura de ejecución.

BUG REAL ENCONTRADO PROBANDO ESTO CON DOCKER DE VERDAD (2026-07-15,
antes de que existiera sdk/): toda skill (como skills/system_info/tool.py)
hace `from sdk.skill import Tool` / `from sdk.artifacts import Artifact`
— pero el contenedor solo tenía montado el código de la skill, nunca
el paquete que define esos tipos. El import fallaba con
`ModuleNotFoundError` en CUALQUIER skill real, no solo en casos raros.
Fix: se copia el paquete `sdk/` COMPLETO (100% stdlib, ver sdk/__init__.py)
como parte fija de `workspace_files` en cada ejecución, bajo
`/workspace/sdk/`. Es el único lugar de la skill que necesita esto: el
resto del código de la skill puede importar libremente `sdk.*`, nunca
`kernel.*` ni `agent_core.*`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from audit.audit_log import AuditEvent, audit_log
from kernel.api.bus import KernelServiceBus, kernel_service_bus as default_kernel_service_bus
from kernel.api.socket_server import KernelBusSocketServer
from kernel.lifecycle.docker_runner import SandboxResult
from kernel.lifecycle.executor import SandboxExecutor
from kernel.permissions.permission_cascade import permission_cascade
from kernel.registry.skill_signing import verify_skill_signature
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from tool_integration.malware_scan import MalwareScanError, scan_bytes
from sdk.permissions import Permission
from utils.correlation import get_correlation_id
from utils.logger import get_logger

logger = get_logger(__name__)

_RUNNER_PATH = Path(__file__).resolve().parent.parent / "lifecycle" / "skill_runner.py"
_SDK_DIR = Path(__file__).resolve().parent.parent.parent / "sdk"

_DEFAULT_ARTIFACTS_ROOT = Path("data") / "artifacts" / "skills"

# Nombre reservado: ver kernel/lifecycle/skill_runner.py — va DENTRO de output/
# para viajar de vuelta por el mismo mecanismo que un archivo real de
# la skill (SandboxResult.output_files), sin necesitar un canal aparte.
_RESULT_FILENAME = "_output.json"

# Debe coincidir con sdk/context.py::SOCKET_PATH
# (la mitad "container_path" del extra_mount de abajo).
_KERNEL_SOCKET_CONTAINER_DIR = "/workspace/.kal"

# BUG REAL ENCONTRADO EN USO: el timeout por defecto del sandbox
# (config.yaml: sandbox.timeout_seconds, 30s) alcanza de sobra para
# run_code, pero una skill que llama a un servicio del kernel (p.ej.
# "image.generate") puede tardar bastante más — el contenedor se mataba
# por timeout mientras esperaba la respuesta del socket, aunque el
# servicio nunca falló. Se sube el límite SOLO para ejecuciones que
# declaran kernel_services, nunca para el resto de las skills.
# Subido de 300 a 600 al agregar image.inpaint (modelo de difusión
# COMPLETO, no distilado — "del orden de minutos" él solo, ver
# tool_integration/adapters/image_editing.py) y las skills compuestas
# que encadenan dos llamadas de modelo en una misma ejecución
# (voice_roundtrip_via_kernel, image_inpaint_via_kernel).
_KERNEL_SERVICE_TIMEOUT_SECONDS = 600


def _kal_runtime_files() -> dict[str, str]:
    """
    El paquete `sdk/` tal como existe en ESTE repo (no una copia
    editable por la skill) — toda skill subclasea `Tool`/usa
    `Artifact`/`ToolManifest`/`Permission` de acá, así que el
    contenedor necesita este paquete disponible para poder ni siquiera
    importar el módulo de la skill. 100% stdlib (sin red, sin
    filesystem — `sdk/context.py` solo sabe hablar por el socket ya
    montado, nunca importa nada de `kernel`) — no es código de la
    skill, es infraestructura fija de kal.
    """
    return {
        f"sdk/{filename}": (_SDK_DIR / filename).read_text(encoding="utf-8")
        for filename in ("__init__.py", "skill.py", "artifacts.py", "permissions.py", "context.py")
    }


class SandboxedSkillTool(Tool):
    def __init__(
        self,
        manifest: ToolManifest,
        skill_dir: Path,
        entry_point: str,
        image: str,
        sandbox: SandboxExecutor | None = None,
        artifacts_root: Path | None = None,
        kernel_services: list[str] | None = None,
        kernel_bus_instance: KernelServiceBus | None = None,
    ):
        self.manifest = manifest
        self.skill_dir = Path(skill_dir)
        self.entry_point = entry_point
        self.image = image
        self.sandbox = sandbox or SandboxExecutor()
        # DEFENSA EN PROFUNDIDAD (K-1, auditoría externa Likay-OS
        # 2026-09-26): kernel/registry/skills.py::load_skills() ya
        # rechaza un manifest.name inválido ANTES de instanciar este
        # Tool (_validate_skill_name) — este chequeo es la segunda capa,
        # para cualquier otro llamador que construya SandboxedSkillTool
        # directamente (tests, un futuro path de carga distinto) sin
        # pasar por esa validación. containment_root.mkdir() va ANTES
        # del .resolve() de abajo a propósito: Path.resolve() en una
        # ruta que todavía no existe puede no normalizar symlinks
        # intermedios de la misma forma que una que sí existe — crear
        # la raíz primero garantiza que ambos .resolve() de la
        # comparación ven el mismo filesystem real.
        containment_root = artifacts_root or _DEFAULT_ARTIFACTS_ROOT
        containment_root.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = containment_root / manifest.name
        if not self.artifact_dir.resolve().is_relative_to(containment_root.resolve()):
            raise ValueError(
                f"manifest.name '{manifest.name}' resuelve fuera de la raíz de artefactos "
                f"permitida ({containment_root}) — rechazado (posible path traversal)."
            )
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        # Métodos del Kernel Service Bus que esta skill puede llamar
        # (p.ej. ["image.generate"]) — ver kernel/__init__.py.
        # kernel_bus_instance inyectable para tests (evita depender del
        # bus real/ImageService real, que carga un modelo pesado).
        self.kernel_services = kernel_services or []
        self.kernel_bus = kernel_bus_instance or default_kernel_service_bus

    def execute(self, **kwargs) -> Artifact:
        # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
        # 2026-09-26), K-4: la cascada de permisos (PermissionCascade,
        # "más restrictivo gana" entre denegados globales + techo por
        # tier) solo se invocaba en el LLAMADOR (agent_core/llm/
        # agent_loop.py), nunca acá dentro del kernel — network_mode
        # más abajo se derivaba de manifest.permissions SOLO, un dato
        # AUTODECLARADO por la propia skill, sin ningún techo real
        # aplicado por el kernel mismo. Cualquier otro consumidor de
        # este Tool (no agent_loop.py) heredaba cero cascada. Tier
        # hardcodeado a "skill" (nunca trust_tier_for(self), que
        # importaría este módulo desde permission_cascade.py y
        # reintroduciría el ciclo que ese módulo ya evita a propósito
        # con imports diferidos) — un SandboxedSkillTool ES,
        # estructuralmente, siempre tier "skill", no hace falta
        # preguntarle a trust_tier_for() lo que ya se sabe por
        # construcción.
        missing = permission_cascade.missing_permissions(self.manifest.permissions, "skill")
        if missing:
            detail = (
                f"Permisos {sorted(p.value for p in missing)} rechazados por la cascada del "
                f"kernel (tier 'skill') — el manifiesto de la skill los pide, pero ningún nivel "
                f"se los otorga a este tier de confianza."
            )
            self._audit_permission_denied(detail)
            return self._error(detail)

        # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
        # 2026-09-26), K-5: la firma solo se verificaba UNA VEZ, al
        # cargar (kernel/registry/skills.py::load_skills(), tiempo de
        # arranque) — pero _collect_skill_files() de abajo relee el
        # contenido de skill_dir DEL DISCO en CADA execute() posterior.
        # Quien pueda escribir en skills/<x>/ entre la carga y una
        # ejecución posterior (horas o días después, el proceso no
        # tiene por qué reiniciarse) corría código nunca verificado, y
        # la auditoría seguía reportando "signature_status: verified"
        # (auditado al cargar, no al ejecutar). Re-verifica ACÁ, fresco,
        # contra el contenido REAL de disco en este momento — mismo
        # criterio fail-closed que load_skills(): "unsigned" sigue
        # permitido (compatibilidad, nunca hubo firma que romper),
        # "tampered" rechaza SIEMPRE, sea la primera ejecución o la
        # numero mil.
        signature_status = verify_skill_signature(self.skill_dir)
        if signature_status == "tampered":
            detail = (
                "skill.sig ya no verifica contra el contenido ACTUAL de la carpeta — algo "
                "cambió en skills/ desde que se cargó (o desde la última ejecución)."
            )
            self._audit_signature_invalid(detail)
            return self._error(detail)

        logger.info(f"Ejecutando skill de terceros: '{self.manifest.name}'")
        workspace_files = _kal_runtime_files()
        workspace_files.update(self._collect_skill_files())
        workspace_files["_input.json"] = json.dumps({"entry_point": self.entry_point, "kwargs": kwargs})

        network_mode = "bridge" if Permission.NETWORK in self.manifest.permissions else None

        socket_server: KernelBusSocketServer | None = None
        socket_tempdir: str | None = None
        extra_mounts: dict[str, str] | None = None
        if self.kernel_services:
            socket_tempdir = tempfile.mkdtemp(prefix="kal-kernel-bus-")
            socket_server = KernelBusSocketServer(
                bus=self.kernel_bus,
                allowed_methods=self.kernel_services,
                socket_path=Path(socket_tempdir) / "kernel.sock",
                skill_name=self.manifest.name,
                # Capturado ACÁ (mismo thread que originó el pedido HTTP) y
                # pasado explícito — el socket sirve en un thread de
                # background propio (ver KernelBusSocketServer.start()) al
                # que un contextvar nunca cruza automáticamente.
                correlation_id=get_correlation_id(),
            )
            socket_server.start()
            extra_mounts = {socket_tempdir: _KERNEL_SOCKET_CONTAINER_DIR}

        try:
            result = self.sandbox.execute_trusted(
                _RUNNER_PATH.read_text(encoding="utf-8"),
                workspace_files=workspace_files,
                network_mode=network_mode,
                image=self.image,
                output_dir="output",
                context={"skill": self.manifest.name},
                granted_permissions=self.manifest.permissions,
                extra_mounts=extra_mounts,
                timeout_seconds=_KERNEL_SERVICE_TIMEOUT_SECONDS if self.kernel_services else None,
            )
        finally:
            # Pase lo que pase adentro del contenedor, el socket nunca
            # debe sobrevivir más allá de ESTA ejecución.
            if socket_server is not None:
                socket_server.stop()
            if socket_tempdir is not None:
                shutil.rmtree(socket_tempdir, ignore_errors=True)

        return self._to_artifact(result)

    def _collect_skill_files(self) -> dict[str, str | bytes]:
        """
        Copia todo el contenido de la carpeta de la skill (código +
        posibles archivos de datos que traiga empaquetados), excepto el
        manifiesto y artefactos de bytecode — el manifiesto no le sirve
        de nada al runner (ya se leyó en load_skills()) y __pycache__
        puede tener .pyc de una versión de Python distinta a la de la
        imagen del contenedor.
        """
        files: dict[str, str | bytes] = {}
        for path in self.skill_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "skill.yaml" or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(self.skill_dir).as_posix()
            try:
                files[f"skill/{rel}"] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                files[f"skill/{rel}"] = path.read_bytes()
        return files

    def _to_artifact(self, result: SandboxResult) -> Artifact:
        if result.status != "success":
            return self._error(
                result.stderr or f"la skill '{self.manifest.name}' falló en el sandbox (status={result.status})"
            )

        output_files = dict(result.output_files)
        raw_result = output_files.pop(_RESULT_FILENAME, None)
        if raw_result is None:
            return self._error(f"la skill '{self.manifest.name}' no devolvió resultado (falta {_RESULT_FILENAME})")

        try:
            payload = json.loads(raw_result.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return self._error(f"resultado de la skill '{self.manifest.name}' no es JSON válido: {e}")

        if not payload.get("ok"):
            return self._error(payload.get("error") or f"error desconocido en la skill '{self.manifest.name}'")

        modality = payload.get("modality", "text")
        uri = payload.get("uri", "")
        metadata = payload.get("metadata", {})

        # La skill devolvió un nombre de archivo relativo a su propia
        # carpeta de salida (KAL_SKILL_OUTPUT_DIR) — ese nombre solo
        # tiene sentido DENTRO del contenedor ya destruido; acá se
        # persiste el contenido real (ya viajó en output_files) a un
        # artefacto propio de esta skill, con una ruta de host real.
        #
        # Antes de escribir ESTOS bytes al filesystem real: son la
        # única fuente de datos arbitrarios de la confianza MÁS BAJA
        # (una skill de un tercero) que llegan sin re-codificar al
        # disco real, listos para que el usuario los abra después con
        # cualquier aplicación — se escanean con ClamAV
        # (tool_integration/malware_scan.py) primero. Fail-closed: si
        # no se puede escanear (ClamAV no instalado) o se detecta algo,
        # el artefacto nunca se escribe.
        if modality != "text" and uri and uri in output_files:
            data = output_files[uri]
            try:
                scan_bytes(data, suffix=Path(uri).suffix)
            except MalwareScanError as e:
                detail = f"Artefacto de la skill '{self.manifest.name}' bloqueado: {e}"
                self._audit_scan_blocked(detail)
                return self._error(detail)
            final_path = self.artifact_dir / f"{uuid.uuid4()}{Path(uri).suffix}"
            final_path.write_bytes(data)
            uri = str(final_path)
        elif uri.startswith("artifact://"):
            # La skill devolvió tal cual la referencia opaca que recibió
            # de un servicio del Kernel Service Bus (p.ej.
            # "artifact://image/<uuid>" de ImageService.generate(), ver
            # kernel/api/bus.py) — el archivo real ya existe en el host
            # (lo generó el servicio, no la skill), solo hace falta
            # resolver la referencia a la ruta real.
            resolved = self.kernel_bus.resolve_artifact(uri)
            if resolved is not None:
                uri = resolved

        return Artifact(modality=modality, uri=uri, metadata=metadata)

    @staticmethod
    def _error(message: str) -> Artifact:
        logger.warning(message)
        return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": message})

    def _audit_scan_blocked(self, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="artifact_scan_blocked",
                summary=detail,
                context={"skill": self.manifest.name},
                outcome="failure",
            )
        )

    def _audit_permission_denied(self, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="skill_permission_denied",
                summary=detail,
                context={"skill": self.manifest.name},
                outcome="failure",
            )
        )

    def _audit_signature_invalid(self, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="skill_execution_signature_invalid",
                summary=detail,
                context={"skill": self.manifest.name},
                outcome="failure",
            )
        )

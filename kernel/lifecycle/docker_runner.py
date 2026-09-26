"""
Ejecución de código no confiable en contenedores Docker efímeros.

Cada ejecución levanta un contenedor NUEVO desde una imagen mínima,
ejecuta con límites duros, captura el resultado y se destruye. Nunca
se reutiliza un contenedor entre ejecuciones distintas (evita
contaminación de estado).

Requiere el paquete `docker` y acceso al socket de Docker. En el
docker-compose.yml de este proyecto, SOLO el servicio `sandbox_runner`
tiene ese acceso — el agente principal lo invoca vía API interna, no
directamente, para reducir superficie de ataque.
"""
from __future__ import annotations

import concurrent.futures
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from requests.exceptions import RequestException

from utils.config import settings
from utils.correlation import get_correlation_id
from utils.logger import get_logger

logger = get_logger(__name__)

# Configurable vía entorno para poder apuntar a una imagen propia
# minimizada (ver Etapa 1, paso 3 del plan) sin tocar código.
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "python:3.11-slim")


@dataclass
class SandboxResult:
    status: str  # success | error | timeout | resource_limit_exceeded
    stdout: str
    stderr: str
    exit_code: int | None
    resource_usage: dict = field(default_factory=dict)
    # Archivos leídos de vuelta desde `output_dir` (ver run()) tras la
    # ejecución, ruta relativa a ese directorio -> contenido crudo. Vacío
    # si no se pidió `output_dir` o si el directorio no existía/estaba
    # vacío. Necesario para que una skill pueda devolver un archivo real
    # (imagen/audio/etc.), no solo texto por stdout.
    output_files: dict[str, bytes] = field(default_factory=dict)


class DockerSandboxRunner:
    def __init__(self):
        # BUG REAL ENCONTRADO EN USO (2026-09-12, validando Likay-OS):
        # conectar acá adentro (antes: docker.from_env() directo en
        # __init__, propagando DockerException si fallaba) hacía que
        # construir el Orchestrator singleton sin Docker disponible
        # tirara abajo TODA la app al importar agent_core.orchestrator
        # — confirmado en vivo apuntando DOCKER_HOST a algo inalcanzable:
        # ni siquiera llegaba a levantar /health. Mucho más grave que
        # "una herramienta falla": la app entera no arrancaba. La
        # conexión ahora es perezosa (ver la property `client` abajo) —
        # recién se intenta al primer uso real, dentro de run(), que ya
        # atrapa DockerException/APIError y devuelve un SandboxResult de
        # error en vez de propagar — la ausencia de Docker degrada ESA
        # llamada puntual, nunca el arranque de la aplicación.
        self._client: "docker.DockerClient | None" = None
        self.cfg = settings.sandbox

    @property
    def client(self) -> "docker.DockerClient":
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    @staticmethod
    def _prepare_workdir(
        workdir: Path, source_code: str, workspace_files: dict[str, str | bytes] | None
    ) -> None:
        """
        Escribe los archivos de trabajo. El contenedor corre con el
        mismo UID/GID que este proceso (ver run(): user=f"{os.getuid()}:
        {os.getgid()}"), así que el dueño real ya alcanza para que el
        contenedor lea/escriba — no hace falta abrir permisos a
        grupo/otros.

        Antes el contenedor corría con un UID hardcodeado (1000:1000)
        distinto del proceso host, y para compensar el mismatch se
        abría todo el árbol con chmod 0777 (rwx para CUALQUIER usuario
        del sistema). Hallazgo real de la revisión de seguridad
        2026-07-09: en un host compartido (no de un solo usuario), eso
        dejaba el código a ejecutar (y cualquier archivo de la skill)
        legible y ESCRIBIBLE por cualquier otro usuario local mientras
        el contenedor corría. Al hacer coincidir el UID del contenedor
        con el del proceso que lo lanza, ya no hay mismatch que
        compensar — los permisos por defecto (0700 de
        tempfile.TemporaryDirectory(), 0644/0755 de write_text/mkdir)
        alcanzan porque el dueño real y el usuario del contenedor son
        la misma cuenta.

        `workspace_files` acepta contenido `str` (texto, p.ej. código
        fuente) o `bytes` (binario, p.ej. un asset que una skill traiga
        empaquetado) y rutas con subcarpetas (p.ej. "skill/helpers/x.py")
        — necesario para copiar una skill completa, no un único archivo
        plano como antes.
        """
        script_path = workdir / "main.py"
        script_path.write_text(source_code, encoding="utf-8")

        for filename, content in (workspace_files or {}).items():
            file_path = workdir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(content, encoding="utf-8")

    def run(
        self,
        source_code: str,
        workspace_files: dict[str, str | bytes] | None = None,
        image: str | None = None,
        network_mode: str | None = None,
        output_dir: str | None = None,
        extra_mounts: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        memory_limit_mb: int | None = None,
        cpu_limit: float | None = None,
        pids_limit: int | None = None,
    ) -> SandboxResult:
        """
        Ejecuta `source_code` en un contenedor efímero aislado.

        workspace_files: archivos adicionales a montar en /workspace
        (p.ej. datos de entrada), nunca credenciales.
        image: sobrescribe SANDBOX_IMAGE solo para esta ejecución (útil
        para probar imágenes alternativas, p.ej. la minimizada de
        kernel/lifecycle/images/minimal/, sin cambiar el default global).
        network_mode: sobrescribe el "none" por defecto SOLO para esta
        ejecución. Usar con extremo cuidado — ver
        error_handling/strategies.py:ImportErrorStrategy para el único
        caso de uso legítimo actual (instalar un paquete faltante), que
        además queda auditado explícitamente como excepción de red.
        output_dir: subcarpeta de /workspace (relativa) que el código
        puede usar para escribir archivos de salida — se crea ANTES de
        ejecutar (escribible por el usuario del contenedor) y se lee de
        vuelta en `SandboxResult.output_files` (ruta relativa -> bytes)
        antes de que el directorio temporal se destruya. Sin esto, todo
        lo escrito en /workspace se pierde al salir del `with` de abajo
        — hasta ahora nunca hacía falta (run_code/DynamicSandboxedTool
        solo necesitan stdout), pero una skill que genera un archivo
        real (imagen/audio) sí lo necesita.
        extra_mounts: bind mounts adicionales, host_path -> container_path
        (además del /workspace principal). Caso de uso real: montar el
        directorio que contiene el socket Unix del Kernel Service Bus
        (ver kernel/api/socket_server.py,
        kernel/registry/sandboxed_skill.py) — ese socket vive en un
        directorio temporal APARTE del workdir de esta función (que no
        es accesible desde afuera hasta que ya se creó adentro de este
        método), así que no alcanza con workspace_files para esto.
        timeout_seconds: sobrescribe el timeout por defecto (30s) SOLO
        para esta ejecución. BUG REAL ENCONTRADO EN USO: una skill que
        llama a un servicio del Kernel Service Bus (p.ej.
        "image.generate") puede tardar bastante más que 30s en total
        (el servicio real hace generación de imagen de verdad) — sin
        esto, el contenedor se mataba por timeout ANTES de que el
        servicio terminara de responder por el socket, aunque el
        servicio en sí nunca falló. Ver
        kernel/registry/sandboxed_skill.py, que sube este valor
        específicamente cuando la skill declara kernel_services.
        memory_limit_mb/cpu_limit/pids_limit: sobrescriben los defaults
        del sandbox (512m/1.0/64) SOLO para esta ejecución. Caso de uso
        real: agent_core/self_modification.py corre el test suite
        COMPLETO (fastapi+chromadb+~1100 tests colectando) dentro de
        este sandbox — los límites pensados para un script chico de una
        skill se quedan cortos ahí.
        """
        target_image = image or SANDBOX_IMAGE
        target_network_mode = network_mode or self.cfg.network_mode
        target_timeout_seconds = timeout_seconds or self.cfg.timeout_seconds
        target_memory_limit_mb = memory_limit_mb or self.cfg.memory_limit_mb
        target_cpu_limit = cpu_limit or self.cfg.cpu_limit
        target_pids_limit = pids_limit or self.cfg.pids_limit
        with tempfile.TemporaryDirectory() as tmp_dir:
            workdir = Path(tmp_dir)
            self._prepare_workdir(workdir, source_code, workspace_files)

            output_path = None
            if output_dir:
                output_path = workdir / output_dir
                output_path.mkdir(parents=True, exist_ok=True)

            volumes = {str(workdir): {"bind": "/workspace", "mode": "rw"}}
            for host_path, container_path in (extra_mounts or {}).items():
                volumes[str(host_path)] = {"bind": container_path, "mode": "rw"}

            # Correlation ID (ver utils/correlation.py) del pedido HTTP que
            # originó esta ejecución, si lo hay — disponible DENTRO del
            # contenedor por si una skill quiere incluirlo en lo que
            # imprime, sin que eso sea obligatorio (una skill de terceros
            # puede ignorarlo tranquilamente).
            correlation_id = get_correlation_id()
            environment = {"KAL_CORRELATION_ID": correlation_id} if correlation_id else {}

            start = time.time()
            container = None
            run_kwargs = dict(
                image=target_image,
                command=["python", "/workspace/main.py"],
                volumes=volumes,
                environment=environment,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                working_dir="/workspace",
                network_mode=target_network_mode,          # "none" por defecto
                mem_limit=f"{target_memory_limit_mb}m",
                memswap_limit=f"{target_memory_limit_mb}m",  # sin swap extra
                nano_cpus=int(target_cpu_limit * 1e9),
                pids_limit=target_pids_limit,
                read_only=True,
                # Mismo UID/GID que este proceso, no un valor
                # hardcodeado — ver _prepare_workdir() para el motivo
                # (evita el mismatch que antes se compensaba abriendo
                # el bind mount a cualquier usuario del host).
                user=f"{os.getuid()}:{os.getgid()}",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                detach=True,
                remove=False,  # False para poder leer logs tras terminar; se limpia abajo
            )
            try:
                # BUG REAL ENCONTRADO EN USO (kal-in issue #4, 2026-09-21):
                # containers.run() dispara un pull IMPLÍCITO de la imagen
                # si no está cacheada — sin red (o con red caída a mitad
                # de pull), esa llamada puede colgar sin ningún timeout
                # propio. container.wait() en _wait_and_collect() de abajo
                # solo acota la ESPERA de un contenedor que YA arrancó, no
                # esta llamada. Envuelta en un thread con
                # future.result(timeout=...) para acotarla también acá —
                # mismo target_timeout_seconds que ya se confía para la
                # ejecución. No se espera a que el thread termine tras un
                # timeout (ver más abajo): no hay forma de cancelar una
                # llamada bloqueante de docker-py ya en curso, y esperarla
                # anularía el propio timeout que se acaba de aplicar.
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                future = pool.submit(self.client.containers.run, **run_kwargs)
                try:
                    container = future.result(timeout=target_timeout_seconds)
                except concurrent.futures.TimeoutError:
                    logger.error(
                        f"docker run/pull de '{target_image}' no respondió en "
                        f"{target_timeout_seconds}s (probable red caída o pull colgado)"
                    )
                    return SandboxResult(
                        "timeout", "", f"docker run/pull excedió {target_timeout_seconds}s sin responder", None
                    )
            except ImageNotFound:
                logger.error(f"Imagen de sandbox no encontrada: {target_image}")
                return SandboxResult("error", "", f"sandbox image not found: {target_image}", None)
            except (APIError, DockerException) as e:
                logger.error(f"Error de Docker al crear el contenedor: {e}")
                return SandboxResult("error", "", f"docker error: {e}", None)

            result = self._wait_and_collect(container, start, target_timeout_seconds)
            if output_path is not None and output_path.exists():
                result.output_files = self._collect_output_files(output_path)
            return result

    @staticmethod
    def _collect_output_files(output_path: Path) -> dict[str, bytes]:
        """
        VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
        2026-09-26), K-2: la versión anterior usaba `p.is_file()`
        (sigue symlinks) + `p.read_bytes()` sobre TODO lo que devolviera
        `rglob("*")` en el bind mount de salida (rw, escribible por el
        código NO CONFIABLE que corre dentro del contenedor). Ese
        código podía crear un symlink apuntando a cualquier archivo del
        HOST (p.ej. /proc/self/environ, una clave de firma, un token) —
        el proceso HOST (este, no el contenedor) lo seguía sin darse
        cuenta y cargaba ese contenido arbitrario en memoria: lectura
        arbitraria de archivos del host por un agente hostil.

        Fix en dos capas: `os.lstat` (nunca sigue symlinks) para
        descartar cualquier entrada que no sea un archivo REGULAR —
        cubre el symlink de archivo, el caso concreto de la
        vulnerabilidad — y además se descarta cualquier archivo cuya
        ruta REAL resuelva fuera de `output_path`, por si un directorio
        INTERMEDIO (no el archivo hoja) fuera el symlink.
        """
        collected: dict[str, bytes] = {}
        resolved_root = output_path.resolve()
        for p in output_path.rglob("*"):
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if not p.resolve().is_relative_to(resolved_root):
                continue
            collected[str(p.relative_to(output_path))] = p.read_bytes()
        return collected

    def _wait_and_collect(self, container, start: float, timeout_seconds: int) -> SandboxResult:
        status = "error"
        stdout, stderr, exit_code = "", "", None

        try:
            exit_status = container.wait(timeout=timeout_seconds)
            exit_code = exit_status.get("StatusCode")
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            if exit_code == 0:
                status = "success"
            elif exit_code == 137:
                # 137 = SIGKILL (128+9). En este contexto casi siempre es
                # el OOM killer del kernel por exceder mem_limit.
                status = "resource_limit_exceeded"
                stderr = stderr or "proceso terminado por límite de memoria (OOM)"
            else:
                status = "error"
        except RequestException as e:
            # NOTA: dependiendo de la versión de requests/urllib3, un
            # timeout del lado cliente puede llegar como ReadTimeout
            # directo o envuelto en un ConnectionError (visto en la
            # práctica con requests>=2.32 + urllib3>=2.x). Por eso se
            # captura la superclase RequestException y se distingue por
            # contenido del mensaje, en vez de depender del tipo exacto.
            if "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower():
                logger.warning(f"Sandbox excedió timeout de {timeout_seconds}s, forzando kill")
                status = "timeout"
                stderr = f"ejecución excedió el límite de {timeout_seconds}s"
            else:
                logger.error(f"Error de conexión con el daemon de Docker durante wait(): {e}")
                status = "error"
                stderr = f"docker connection error: {e}"
            self._safe_kill(container)
        except (APIError, DockerException) as e:
            logger.error(f"Error de Docker durante la espera del contenedor: {e}")
            status = "error"
            stderr = f"docker error: {e}"
            self._safe_kill(container)
        finally:
            elapsed = time.time() - start
            self._safe_remove(container)

        return SandboxResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            resource_usage={"elapsed_seconds": round(elapsed, 3)},
        )

    @staticmethod
    def _safe_kill(container) -> None:
        try:
            container.kill()
        except (APIError, DockerException) as e:
            # Si ya estaba detenido, kill() lanza 409 — no es un fallo
            # real, solo lo registramos por si acaso.
            logger.debug(f"kill() no aplicado (probablemente ya detenido): {e}")

    @staticmethod
    def _safe_remove(container) -> None:
        if container is None:
            return
        try:
            container.remove(force=True)
        except (APIError, DockerException) as e:
            logger.warning(f"No se pudo eliminar el contenedor {container.id}: {e}")

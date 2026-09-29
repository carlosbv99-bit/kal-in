"""
Pipeline real de self-modification.

Diferencia con kernel/registry/registry.py (creación de herramientas
dinámicas): aquella valida y ejecuta código NUEVO como una herramienta
standalone (un script que se corre y se observa su salida). Esto aquí
valida una MODIFICACIÓN a un archivo EXISTENTE del proyecto, y la
"prueba" real es correr el test suite completo contra una COPIA
aislada del proyecto con el cambio ya aplicado — nunca in-place, nunca
contra el proyecto real hasta que un humano aprueba explícitamente.

Flujo de propose():
  1. Si target_path es núcleo (agent_core/, error_handling/, kernel/):
     bloqueo inmediato, ni siquiera se valida ni se prueba. Ver
     CORE_PATHS_HARDCODED — deliberadamente independiente de
     config.yaml para que no se pueda desactivar alterando la config.
  2. Validación estática del código propuesto (mismo denylist AST que
     usa el sandbox para código no confiable) — primera línea de
     defensa, barata pero NO suficiente por sí sola (ver
     code_analysis/denylist.py).
  3. Copia completa del proyecto a un directorio temporal (dos copias:
     "baseline" sin el cambio, "candidate" con el cambio aplicado).
  4. Se corre el test suite completo en AMBAS copias, DENTRO del
     sandbox de Docker (ver _run_tests()) — nunca en el proceso host.
  5. Si el candidato tiene más fallos/errores o menos tests pasando que
     el baseline: regresión detectada, propuesta rechazada.
  6. Sin regresión: la propuesta queda "pending_human_approval". Nunca
     se aplica sola, ni siquiera para módulos periféricos — escribir a
     disco es la acción de mayor alcance de todo el proyecto.

BUG REAL ENCONTRADO EN REVISIÓN (2026-08-24, auditoría de un
colaborador — verificado contra el código real antes de aceptarlo, ver
docs/HISTORY.md): _run_tests() corría `pytest` como subproceso DIRECTO
en el proceso host, sin sandbox. pytest IMPORTA (ejecuta código de
nivel de módulo) todo lo que colecciona — con ~1100 tests cubriendo
casi todo el proyecto, es muy probable que algún test importe
transitivamente el archivo propuesto. La única barrera antes de esto
era el denylist AST (paso 2), que el propio code_analysis/denylist.py
documenta como insuficiente por sí solo ("el aislamiento real lo da el
sandbox") — pero ese aislamiento real nunca se aplicaba acá. Corregido:
_run_tests() ahora ejecuta pytest DENTRO de un contenedor Docker
efímero (mismo mecanismo que kernel/lifecycle/docker_runner.py usa
para cualquier otro código no confiable), vía
SandboxExecutor.execute_trusted() — el script que invoca a pytest es
código de PRIMERA PARTE y confiable (nunca cambia), lo que es no
confiable es el ÁRBOL DE ARCHIVOS que ese pytest recorre, montado
read-write en /project pero sin red (network_mode="none") y sin acceso
al socket de Docker del host.

apply() escribe el cambio al archivo real, guardando un backup
timestamped para poder revertir con rollback() si algo se detecta mal
después (p.ej. en producción, con datos reales, algo que los tests no
cubrieron).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from audit.audit_log import AuditEvent, audit_log
from code_analysis.ast_validator import validate_code
from kernel.lifecycle.executor import SandboxExecutor
from kernel.lifecycle.selfmod_test_image_builder import SelfModTestImageBuilder
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Nombres del reporte JUnit XML de pytest dentro del sandbox — ver
# _run_tests()/_build_test_runner_script(). output_dir es relativo a
# /workspace (docker_runner.py ya sabe leerlo de vuelta al host).
_REPORT_OUTPUT_DIR = "results"
_REPORT_FILENAME = "report.xml"

# Última barrera, independiente de config.yaml — igual criterio que en
# el resto del proyecto: aunque la config fuera corrompida por código
# generado dinámicamente, estas rutas nunca se auto-modifican.
# "sandbox/" (2026-07-20: renombrado a "kernel/" en la reestructuración
# a kernel/ + sdk/ — mismo alcance de protección, ninguna regla nueva).
CORE_PATHS_HARDCODED = ("agent_core/", "error_handling/", "kernel/")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directorios que no tiene sentido copiar al probar una modificación:
# entornos virtuales (pueden pesar GBs con torch instalado), datos
# generados, logs, y caches — ninguno afecta el resultado de los tests.
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".git", ".venv", "venv", "data", "logs", ".pytest_cache"
)


class PathTraversalError(Exception):
    """target_path intenta escapar del directorio del proyecto."""


class SelfModTestRunError(Exception):
    """El test suite no pudo correr dentro del sandbox (fallo de infraestructura, no de tests)."""


@dataclass
class TestRunResult:
    passed: int
    failed: int
    errors: int
    raw_output: str
    exit_code: int

    @property
    def is_clean(self) -> bool:
        return self.failed == 0 and self.errors == 0


@dataclass
class SelfModProposal:
    id: str
    target_path: str
    proposed_source: str
    justification: str
    # "disabled" | "blocked_core" | "rejected_unsafe" |
    # "regression_detected" | "pending_human_approval" | "applied" |
    # "rolled_back"
    status: str
    baseline_tests: TestRunResult | None = None
    candidate_tests: TestRunResult | None = None
    detail: str = ""
    backup_path: str | None = None


class SelfModificationManager:
    def __init__(
        self,
        project_root: Path | None = None,
        executor: SandboxExecutor | None = None,
        image_builder: SelfModTestImageBuilder | None = None,
    ):
        self.project_root = (project_root or PROJECT_ROOT).resolve()
        self._proposals: dict[str, SelfModProposal] = {}
        # Inyectables para tests (evita requerir un daemon de Docker
        # real solo para probar la lógica de propose()/apply()/
        # rollback() — ver tests/test_self_modification.py). En
        # producción, ambos se construyen recién al primer uso real
        # (_run_tests), no acá, para no exigir Docker con solo
        # importar este módulo.
        self._executor = executor
        self._image_builder = image_builder

    def is_core_path(self, target_path: str) -> bool:
        return target_path.startswith(CORE_PATHS_HARDCODED) or settings.self_modification.is_core_path(target_path)

    def propose(
        self,
        target_path: str,
        proposed_source: str,
        justification: str,
        test_args: list[str] | None = None,
    ) -> SelfModProposal:
        proposal_id = str(uuid.uuid4())
        test_args = test_args or ["-q", "--tb=short"]

        # Chequeo MÁS TEMPRANO que cualquier otro, antes incluso de dejar
        # el evento "pending" habitual: si self-modification está
        # deshabilitada por configuración, no corresponde ni empezar a
        # auditar una propuesta "en curso" — se resuelve en un solo
        # evento. BUG REAL encontrado en revisión (2026-07-11):
        # settings.self_modification.enabled existía en el esquema
        # (default False en el modelo) pero ningún código lo leía nunca
        # — config.yaml lo tenía en `true` sin que importara, la
        # funcionalidad quedaba SIEMPRE activa sin importar ese valor.
        # Default ahora en `false` en config.yaml: opt-in explícito.
        if not settings.self_modification.enabled:
            proposal = SelfModProposal(
                id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                justification=justification, status="disabled",
                detail="self-modification está deshabilitada por configuración (self_modification.enabled: false).",
            )
            self._proposals[proposal_id] = proposal
            self._audit(proposal, "failure", proposal.detail)
            return proposal

        audit_log.record(
            AuditEvent(
                event_type="self_modification_proposed",
                summary=f"Propuesta de modificación sobre {target_path}",
                context={"target_path": target_path, "justification": justification, "proposal_id": proposal_id},
                outcome="pending",
            )
        )

        if self.is_core_path(target_path):
            logger.warning(f"Propuesta sobre módulo NÚCLEO ({target_path}) bloqueada — ni se valida ni se prueba.")
            proposal = SelfModProposal(
                id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                justification=justification, status="blocked_core",
                detail="agent_core/, error_handling/ y sandbox/ nunca se prueban ni aplican automáticamente.",
            )
            self._proposals[proposal_id] = proposal
            # BUG REAL ENCONTRADO EN REVISIÓN: este era el único branch
            # que no dejaba un segundo evento de auditoría con el
            # desenlace — el evento "pending" inicial quedaba huérfano
            # para el caso más crítico de todos (bloqueo de núcleo).
            # Los demás branches (rejected_unsafe, regression_detected,
            # pending_human_approval) sí completan el ciclo intent+outcome.
            self._audit(proposal, "failure", proposal.detail)
            return proposal

        try:
            self._safe_join(self.project_root, target_path)
        except PathTraversalError as e:
            proposal = SelfModProposal(
                id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                justification=justification, status="rejected_unsafe", detail=str(e),
            )
            self._proposals[proposal_id] = proposal
            self._audit(proposal, "failure", str(e))
            return proposal

        validation = validate_code(proposed_source)
        if not validation.is_safe:
            reason = validation.syntax_error or "; ".join(validation.violations)
            proposal = SelfModProposal(
                id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                justification=justification, status="rejected_unsafe", detail=reason,
            )
            self._proposals[proposal_id] = proposal
            self._audit(proposal, "failure", f"Rechazada en validación estática: {reason}")
            return proposal

        with tempfile.TemporaryDirectory(prefix="kal_selfmod_") as tmp_dir:
            candidate_root = Path(tmp_dir) / "candidate"
            baseline_root = Path(tmp_dir) / "baseline"
            self._copy_project(candidate_root)
            self._copy_project(baseline_root)

            candidate_target = self._safe_join(candidate_root, target_path)
            if not candidate_target.exists():
                proposal = SelfModProposal(
                    id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                    justification=justification, status="rejected_unsafe",
                    detail=(
                        f"target_path '{target_path}' no existe en el proyecto actual — "
                        "self-modification solo modifica archivos existentes, no crea nuevos "
                        "(usar kernel/registry/registry.py para herramientas nuevas)."
                    ),
                )
                self._proposals[proposal_id] = proposal
                self._audit(proposal, "failure", proposal.detail)
                return proposal

            candidate_target.write_text(proposed_source, encoding="utf-8")

            logger.info("Corriendo test suite baseline (sin el cambio) para poder comparar regresiones...")
            baseline_result = self._run_tests(baseline_root, test_args)

            logger.info("Corriendo test suite candidato (con el cambio ya aplicado)...")
            candidate_result = self._run_tests(candidate_root, test_args)

            regression = (
                candidate_result.failed > baseline_result.failed
                or candidate_result.errors > baseline_result.errors
                or candidate_result.passed < baseline_result.passed
            )

            if regression:
                proposal = SelfModProposal(
                    id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                    justification=justification, status="regression_detected",
                    baseline_tests=baseline_result, candidate_tests=candidate_result,
                    detail=(
                        f"Regresión: baseline(passed={baseline_result.passed}, failed={baseline_result.failed}, "
                        f"errors={baseline_result.errors}) vs candidato(passed={candidate_result.passed}, "
                        f"failed={candidate_result.failed}, errors={candidate_result.errors})"
                    ),
                )
                self._proposals[proposal_id] = proposal
                self._audit(proposal, "failure", proposal.detail)
                return proposal

            proposal = SelfModProposal(
                id=proposal_id, target_path=target_path, proposed_source=proposed_source,
                justification=justification, status="pending_human_approval",
                baseline_tests=baseline_result, candidate_tests=candidate_result,
                detail="Sin regresiones detectadas. Requiere aprobación humana explícita para aplicarse a disco.",
            )
            self._proposals[proposal_id] = proposal
            self._audit(proposal, "escalated", "Sin regresiones — pendiente de aprobación humana")
            return proposal

    def reject(self, proposal_id: str, rejected_by: str = "", reason: str = "") -> SelfModProposal:
        """
        Rechazo humano de una propuesta que llegó a `pending_human_approval`.

        Los otros estados de descarte (`rejected_unsafe`, `regression_detected`,
        `blocked_core`) los decide el propio sistema ANTES de que un humano vea
        nada. Una vez que una propuesta llega a la aprobación humana, la única
        transición por API era `apply()`: un humano que revisaba y decidía "esto
        no" no tenía forma de registrar esa decisión, y la propuesta quedaba
        pendiente para siempre —y contando en `/status`— (revisión, 2026-09-29).

        Mismo patrón que `skill_creator.reject()`: valida que esté pendiente, deja
        el estado y el motivo en la propuesta, y lo audita (outcome `failure`, con
        quién y por qué).
        """
        proposal = self._require_proposal(proposal_id)
        if proposal.status != "pending_human_approval":
            raise ValueError(
                f"La propuesta {proposal_id} no está pendiente de aprobación (status: {proposal.status})"
            )

        proposal.status = "rejected_by_human"
        proposal.detail = reason or "Rechazada por un humano sin motivo especificado."
        self._audit(proposal, "failure", f"Rechazada por {rejected_by or 'un humano'}: {proposal.detail}")
        logger.info(f"Propuesta {proposal_id} rechazada por {rejected_by or 'un humano'}")
        return proposal

    def apply(self, proposal_id: str, approved_by: str) -> SelfModProposal:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != "pending_human_approval":
            raise ValueError(f"La propuesta {proposal_id} no está pendiente de aprobación (status: {proposal.status})")

        # El número de versión se calcula ANTES de nombrar el backup e
        # incluirse en el nombre del archivo: dos apply() dentro del
        # mismo segundo generarían el mismo nombre con solo
        # int(time.time()) (BUG REAL encontrado al escribir
        # rollback_to(): el segundo backup pisaba al primero,
        # rollback_to(v1) devolvía el contenido de v2).
        next_version = len(self._load_version_index(proposal.target_path)) + 1
        real_target = self._safe_join(self.project_root, proposal.target_path)
        backup_path = real_target.with_name(real_target.name + f".bak.v{next_version}.{int(time.time())}")
        shutil.copy2(real_target, backup_path)
        real_target.write_text(proposal.proposed_source, encoding="utf-8")

        proposal.status = "applied"
        proposal.backup_path = str(backup_path)

        version = self._append_version_entry(
            proposal.target_path,
            {"backup_path": str(backup_path), "proposal_id": proposal_id, "applied_at": time.time()},
        )

        audit_log.record(
            AuditEvent(
                event_type="self_modification_applied",
                summary=f"Modificación aplicada a {proposal.target_path} (aprobada por {approved_by}, v{version})",
                context={
                    "proposal_id": proposal_id, "target_path": proposal.target_path,
                    "approved_by": approved_by, "backup_path": proposal.backup_path, "version": version,
                },
                outcome="success",
            )
        )
        logger.info(f"Self-modification aplicada a {proposal.target_path}. Backup: {backup_path} (v{version})")
        return proposal

    def rollback(self, proposal_id: str, reason: str = "") -> SelfModProposal:
        proposal = self._require_proposal(proposal_id)
        if proposal.status != "applied":
            raise ValueError(f"La propuesta {proposal_id} no está aplicada, nada que revertir (status: {proposal.status})")
        if proposal.backup_path is None:
            raise RuntimeError(f"Propuesta {proposal_id} marcada 'applied' sin backup registrado — no se puede revertir con seguridad")

        real_target = self._safe_join(self.project_root, proposal.target_path)
        shutil.copy2(proposal.backup_path, real_target)
        proposal.status = "rolled_back"

        audit_log.record(
            AuditEvent(
                event_type="self_modification_rolled_back",
                summary=f"Modificación revertida en {proposal.target_path}",
                context={"proposal_id": proposal_id, "target_path": proposal.target_path, "reason": reason},
                outcome="success",
            )
        )
        logger.info(f"Self-modification revertida en {proposal.target_path}")
        return proposal

    def list_versions(self, target_path: str) -> list[dict]:
        """Historial de aplicaciones exitosas sobre target_path, más antigua primero."""
        return self._load_version_index(target_path)

    def rollback_to(self, target_path: str, version: int, reason: str = "") -> None:
        """
        Restaura target_path al contenido que tenía en una versión
        específica del historial (no necesariamente la última aplicada
        — para eso ya está rollback(proposal_id)). Sirve para volver
        varios pasos atrás sin tener que conocer el proposal_id exacto
        de cada apply() intermedio.
        """
        entries = self._load_version_index(target_path)
        entry = next((e for e in entries if e["version"] == version), None)
        if entry is None:
            raise ValueError(f"No existe la versión {version} para '{target_path}'")

        backup_path = Path(entry["backup_path"])
        if not backup_path.exists():
            raise RuntimeError(f"El backup de la versión {version} ya no existe en disco: {backup_path}")

        real_target = self._safe_join(self.project_root, target_path)
        shutil.copy2(backup_path, real_target)

        audit_log.record(
            AuditEvent(
                event_type="self_modification_rolled_back",
                summary=f"{target_path} revertido a versión {version}",
                context={"target_path": target_path, "version": version, "reason": reason},
                outcome="success",
            )
        )
        logger.info(f"{target_path} revertido a v{version} ({reason})")

    def _version_index_path(self, target_path: str) -> Path:
        """
        Índice de versiones por target_path (una entrada por cada
        apply() exitoso), para poder volver a una versión específica con
        rollback_to(), no solo a la última aplicada (eso ya lo cubre
        rollback()). Vive bajo self.project_root — no un directorio fijo
        del módulo — para que los tests que usan un `fake_project`
        aislado (ver tests/test_self_modification.py) no escriban en
        el data/ real de kal.
        """
        versions_dir = self.project_root / "data" / "selfmod_versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        safe_name = target_path.replace("/", "__")
        return versions_dir / f"{safe_name}.json"

    def _load_version_index(self, target_path: str) -> list[dict]:
        path = self._version_index_path(target_path)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def _append_version_entry(self, target_path: str, entry: dict) -> int:
        entries = self._load_version_index(target_path)
        version = len(entries) + 1
        entries.append({"version": version, **entry})
        self._version_index_path(target_path).write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return version

    def get(self, proposal_id: str) -> SelfModProposal | None:
        return self._proposals.get(proposal_id)

    def list_proposals(self) -> list[SelfModProposal]:
        """Para el dashboard del frontend — más recientes primero (orden de inserción)."""
        return list(self._proposals.values())[::-1]

    def _require_proposal(self, proposal_id: str) -> SelfModProposal:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ValueError(f"No existe una propuesta con id {proposal_id}")
        return proposal

    @staticmethod
    def _safe_join(base: Path, relative: str) -> Path:
        """
        Une `base` con `relative` y garantiza que el resultado no se
        sale de `base` (protección contra path traversal vía '../').
        Sin esto, un target_path como '../../etc/cron.d/evil' podría
        escribir fuera del directorio aislado de pruebas o, peor, fuera
        del proyecto real en apply().
        """
        candidate = (base / relative).resolve()
        base_resolved = base.resolve()
        if candidate != base_resolved and base_resolved not in candidate.parents:
            raise PathTraversalError(
                f"target_path '{relative}' se sale del directorio del proyecto (path traversal detectado)"
            )
        return candidate

    def _copy_project(self, dest: Path) -> None:
        shutil.copytree(self.project_root, dest, ignore=_COPY_IGNORE)

    def _run_tests(self, root: Path, test_args: list[str]) -> TestRunResult:
        """
        Corre `python -m pytest test_args` DENTRO de un contenedor
        Docker efímero (ver docstring del módulo para el porqué) — la
        copia completa del proyecto en `root` (que incluye el código
        PROPUESTO, potencialmente malicioso o alucinado) se monta
        read-write en /project, sin red, sin socket de Docker, con los
        mismos límites de recursos que cualquier otra ejecución no
        confiable, pero ampliados (ver docker_runner.py) porque acá
        corre el test suite completo, no un script chico.

        `test_args` nunca llega desde afuera del proceso (ver
        agent_core/routers/self_modification.py: SelfModProposeRequest
        no tiene ese campo) — siempre es el default interno
        `["-q", "--tb=short"]`, así que insertarlo tal cual en el
        script generado es seguro.
        """
        if self._executor is None:
            self._executor = SandboxExecutor()
        if self._image_builder is None:
            self._image_builder = SelfModTestImageBuilder()

        image = self._image_builder.build_or_get_image()
        script = self._build_test_runner_script(test_args)
        result = self._executor.execute_trusted(
            source_code=script,
            extra_mounts={str(root): "/project"},
            image=image,
            network_mode="none",
            timeout_seconds=600,
            memory_limit_mb=2048,
            cpu_limit=2.0,
            pids_limit=256,
            output_dir=_REPORT_OUTPUT_DIR,
            context={"self_modification_test_run": True},
        )
        if result.status != "success":
            # Falla de INFRAESTRUCTURA (Docker no responde, timeout del
            # contenedor, límite de recursos excedido) — nunca "0 tests
            # corridos, 0 fallaron" en silencio. Sin esto, propose()
            # interpretaría un sandbox roto como "sin regresión" (0==0
            # en ambas copias) y dejaría pasar la propuesta a
            # pending_human_approval sin haber corrido un solo test de
            # verdad.
            raise SelfModTestRunError(
                f"El test suite no pudo correr dentro del sandbox (status={result.status}): {result.stderr}"
            )
        report_xml = result.output_files.get(_REPORT_FILENAME)
        if report_xml is None:
            # pytest en sí no llegó a escribir el reporte (crash antes de
            # terminar, conflicto de plugins, etc.) — mismo criterio que
            # arriba: nunca tratar "no hay reporte" como "0 passed, 0
            # failed" en silencio.
            raise SelfModTestRunError(
                f"pytest no generó el reporte JUnit XML esperado dentro del sandbox. stdout/stderr:\n{result.stdout}\n{result.stderr}"
            )
        passed, failed, errors = self._parse_junit_summary(report_xml)
        return TestRunResult(
            passed=passed, failed=failed, errors=errors,
            raw_output=result.stdout + result.stderr, exit_code=result.exit_code or 0,
        )

    @staticmethod
    def _build_test_runner_script(test_args: list[str]) -> str:
        # --junit-xml (recomendación de una auditoría externa, 2026-08-24,
        # ver docs/HISTORY.md): reemplaza el parseo por regex del resumen
        # en texto de pytest (frágil ante cambios de formato entre
        # versiones) por un reporte estructurado — ver _parse_junit_summary().
        # Se escribe DIRECTO en /workspace/{_REPORT_OUTPUT_DIR}, que
        # docker_runner.py ya lee de vuelta al host vía output_files.
        return (
            "import subprocess\n"
            "import sys\n"
            "\n"
            "result = subprocess.run(\n"
            f"    [sys.executable, '-m', 'pytest', *{test_args!r},\n"
            f"     '--junit-xml=/workspace/{_REPORT_OUTPUT_DIR}/{_REPORT_FILENAME}'],\n"
            "    cwd='/project', capture_output=True, text=True,\n"
            ")\n"
            "sys.stdout.write(result.stdout)\n"
            "sys.stderr.write(result.stderr)\n"
            "sys.exit(result.returncode)\n"
        )

    @staticmethod
    def _parse_junit_summary(report_xml: bytes) -> tuple[int, int, int]:
        """
        Cuenta passed/failed/errors desde el reporte JUnit XML de pytest
        (atributos `tests`/`failures`/`errors`/`skipped` del elemento
        <testsuite>, sumados si hay varios) — reemplaza un parseo previo
        por regex sobre el texto legible del resumen ("3 failed, 12
        passed..."), frágil ante cualquier cambio de formato entre
        versiones de pytest. pytest no reporta "passed" como atributo
        propio: se deriva restando failures/errors/skipped del total.
        """
        root = ET.fromstring(report_xml)
        suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
        total = failures = errors = skipped = 0
        for suite in suites:
            total += int(suite.get("tests", 0))
            failures += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        passed = total - failures - errors - skipped
        return passed, failures, errors

    def _audit(self, proposal: SelfModProposal, outcome: str, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="self_modification_proposed",
                summary=f"Propuesta {proposal.id} sobre {proposal.target_path}: {detail}",
                context={"proposal_id": proposal.id, "target_path": proposal.target_path, "status": proposal.status},
                outcome=outcome,
            )
        )


self_modification_manager = SelfModificationManager()

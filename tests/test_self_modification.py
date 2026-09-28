"""
Tests de agent_core/self_modification.py.

Usan un proyecto SINTÉTICO minúsculo (fixture `fake_project`), no el
propio kal/ — de lo contrario cada test tendría que copiar el proyecto
completo y correr su test suite real (con Docker, modelos de ML, etc.)
DOS veces (baseline + candidato), lo cual sería lentísimo y frágil.
Aquí se prueba la LÓGICA del pipeline (bloqueo de núcleo, detección de
regresión, aplicación, rollback), no el contenido real de kal/.

Desde el fix de 2026-08-24 (ver docstring de self_modification.py),
_run_tests() corre pytest DENTRO de un sandbox Docker real — la
mayoría de estos tests inyectan `_FakeTestExecutor` (corre pytest
directo contra el proyecto SINTÉTICO, sin Docker) para poder probar la
LÓGICA de propose()/apply()/rollback() rápido y sin requerir un daemon
de Docker. La verificación de que el sandbox REAL efectivamente
contiene código malicioso vive en la clase
TestRealSandboxContainsMaliciousTopLevelCode más abajo, marcada
requires_docker.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_core.self_modification import SelfModificationManager, SelfModTestRunError
from tests.conftest import requires_docker
from utils.config import settings


@dataclass
class _FakeSandboxResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    status: str = "success"
    resource_usage: dict = field(default_factory=dict)
    output_files: dict = field(default_factory=dict)


class _FakeTestExecutor:
    """
    Test double de SandboxExecutor: en vez de levantar un contenedor
    Docker real, corre pytest directo en el host contra el path que
    _run_tests() montaría en /project — el proyecto es siempre
    SINTÉTICO (fixture `fake_project`), nunca código real no confiable,
    así que esto no reintroduce el riesgo que motivó el fix. Genera un
    reporte JUnit XML real (mismo formato que produciría el contenedor
    real) para poder ejercitar _parse_junit_summary() de punta a punta.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def execute_trusted(self, source_code, extra_mounts=None, **kwargs):
        self.calls.append({"source_code": source_code, "extra_mounts": extra_mounts, **kwargs})
        (host_path,) = extra_mounts.keys()
        with tempfile.TemporaryDirectory() as report_dir:
            report_path = Path(report_dir) / "report.xml"
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junit-xml={report_path}"],
                cwd=host_path, capture_output=True, text=True, timeout=60, check=False,
            )
            output_files = {"report.xml": report_path.read_bytes()} if report_path.exists() else {}
        return _FakeSandboxResult(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode, output_files=output_files,
        )


class _FakeImageBuilder:
    def build_or_get_image(self) -> str:
        return "fake-image:unused"


@pytest.fixture
def fake_project(tmp_path):
    """
    Proyecto sintético mínimo:
        mymodule.py       — una función simple
        tests/test_mymodule.py — un test que la ejerce
    """
    (tmp_path / "mymodule.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_mymodule.py").write_text(
        "from mymodule import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def manager(fake_project):
    return SelfModificationManager(
        project_root=fake_project, executor=_FakeTestExecutor(), image_builder=_FakeImageBuilder(),
    )


@pytest.fixture(autouse=True)
def _enabled_by_default(monkeypatch):
    """
    Todo el resto de este archivo prueba la lógica del pipeline
    asumiendo que la funcionalidad está habilitada — el gate de
    enabled=false (ver más abajo) es un caso aparte, que lo desactiva
    explícitamente donde corresponde.
    """
    monkeypatch.setattr(settings.self_modification, "enabled", True)


# --- self_modification.enabled: false (bug real corregido 2026-07-11:
# este flag existía en el esquema pero nada lo leía, quedaba siempre
# activo sin importar su valor) ---


def test_propose_is_rejected_when_disabled_in_config(manager, monkeypatch):
    monkeypatch.setattr(settings.self_modification, "enabled", False)

    proposal = manager.propose(
        target_path="mymodule.py", proposed_source="def add(a, b):\n    return a + b\n", justification="x",
    )

    assert proposal.status == "disabled"
    assert proposal.baseline_tests is None  # nunca llegó a correr tests


def test_disabled_check_short_circuits_before_core_path_check(manager, monkeypatch):
    """
    El gate de enabled=false es el PRIMER chequeo de propose(), antes
    incluso del bloqueo de rutas núcleo — si no fuera así, este target
    (núcleo) devolvería 'blocked_core', no 'disabled'.
    """
    monkeypatch.setattr(settings.self_modification, "enabled", False)

    proposal = manager.propose(
        target_path="agent_core/orchestrator.py", proposed_source="# cualquier cosa", justification="x",
    )

    assert proposal.status == "disabled"


# --- Casos rápidos: no llegan a _run_tests() ---

def test_core_path_is_blocked_immediately(manager):
    proposal = manager.propose(
        target_path="agent_core/orchestrator.py",
        proposed_source="# cualquier cosa",
        justification="intento de modificar el núcleo",
    )
    assert proposal.status == "blocked_core"
    assert proposal.baseline_tests is None  # nunca llegó a correr tests


def test_error_handling_path_is_also_blocked(manager):
    proposal = manager.propose(
        target_path="error_handling/strategies.py",
        proposed_source="# cualquier cosa",
        justification="intento",
    )
    assert proposal.status == "blocked_core"


def test_sandbox_path_is_also_blocked(manager):
    proposal = manager.propose(
        target_path="kernel/lifecycle/docker_runner.py",
        proposed_source="# cualquier cosa",
        justification="intento",
    )
    assert proposal.status == "blocked_core"


def test_unsafe_code_is_rejected_without_running_tests(manager):
    proposal = manager.propose(
        target_path="mymodule.py",
        proposed_source="import os\nos.system('ls')\ndef add(a, b):\n    return a + b\n",
        justification="código inseguro",
    )
    assert proposal.status == "rejected_unsafe"
    assert proposal.baseline_tests is None


def test_path_traversal_relative_is_rejected(manager):
    proposal = manager.propose(
        target_path="../../etc/cron.d/evil",
        proposed_source="algo",
        justification="intento de escape",
    )
    assert proposal.status == "rejected_unsafe"
    assert "traversal" in proposal.detail.lower() or "sale del directorio" in proposal.detail.lower()


def test_path_traversal_absolute_is_rejected(manager):
    proposal = manager.propose(
        target_path="/etc/passwd",
        proposed_source="algo",
        justification="intento de escape absoluto",
    )
    assert proposal.status == "rejected_unsafe"


def test_nonexistent_target_is_rejected(manager):
    proposal = manager.propose(
        target_path="no_existe.py",
        proposed_source="print('hola')",
        justification="archivo que no existe",
    )
    assert proposal.status == "rejected_unsafe"
    assert "no existe" in proposal.detail.lower()


def test_get_unknown_proposal_returns_none(manager):
    assert manager.get("id-inventado") is None


def test_cannot_apply_a_blocked_proposal(manager):
    proposal = manager.propose(
        target_path="agent_core/orchestrator.py", proposed_source="x", justification="x"
    )
    with pytest.raises(ValueError):
        manager.apply(proposal.id, approved_by="kalin")


def test_cannot_rollback_a_proposal_never_applied(manager):
    proposal = manager.propose(
        target_path="agent_core/orchestrator.py", proposed_source="x", justification="x"
    )
    with pytest.raises(ValueError):
        manager.rollback(proposal.id, reason="no aplica")


# --- Casos que sí corren pytest real como subproceso (más lentos) ---

def test_safe_change_without_regression_is_pending_approval(manager):
    new_source = (
        "def add(a, b):\n"
        "    \"\"\"Suma dos números.\"\"\"\n"
        "    return a + b\n"
    )
    proposal = manager.propose(
        target_path="mymodule.py", proposed_source=new_source, justification="agregar docstring"
    )

    assert proposal.status == "pending_human_approval"
    assert proposal.baseline_tests is not None
    assert proposal.candidate_tests is not None
    assert proposal.candidate_tests.is_clean


def test_change_that_breaks_existing_test_is_detected_as_regression(manager):
    broken_source = "def add(a, b):\n    return a - b\n"  # rompe test_add
    proposal = manager.propose(
        target_path="mymodule.py", proposed_source=broken_source, justification="cambio que rompe todo"
    )

    assert proposal.status == "regression_detected"
    assert proposal.candidate_tests.failed > proposal.baseline_tests.failed


def test_apply_writes_change_and_creates_backup(manager, fake_project):
    new_source = "def add(a, b):\n    \"\"\"Suma.\"\"\"\n    return a + b\n"
    proposal = manager.propose(target_path="mymodule.py", proposed_source=new_source, justification="doc")
    assert proposal.status == "pending_human_approval"

    manager.apply(proposal.id, approved_by="kalin")

    real_file = fake_project / "mymodule.py"
    assert real_file.read_text(encoding="utf-8") == new_source
    assert proposal.backup_path is not None
    from pathlib import Path
    assert Path(proposal.backup_path).exists()


def test_rollback_restores_original_content(manager, fake_project):
    original_content = (fake_project / "mymodule.py").read_text(encoding="utf-8")
    new_source = "def add(a, b):\n    \"\"\"Suma.\"\"\"\n    return a + b\n"

    proposal = manager.propose(target_path="mymodule.py", proposed_source=new_source, justification="doc")
    manager.apply(proposal.id, approved_by="kalin")
    manager.rollback(proposal.id, reason="prueba de rollback")

    real_file = fake_project / "mymodule.py"
    assert real_file.read_text(encoding="utf-8") == original_content
    assert proposal.status == "rolled_back"


def test_cannot_apply_same_proposal_twice(manager):
    new_source = "def add(a, b):\n    \"\"\"Suma.\"\"\"\n    return a + b\n"
    proposal = manager.propose(target_path="mymodule.py", proposed_source=new_source, justification="doc")
    manager.apply(proposal.id, approved_by="kalin")

    with pytest.raises(ValueError):
        manager.apply(proposal.id, approved_by="otro")


# --- _run_tests() ahora va DENTRO del sandbox, nunca en el proceso host
# (ver docstring del módulo, fix del 2026-08-24) ---


def test_run_tests_goes_through_the_sandbox_executor_not_a_bare_subprocess(manager, fake_project):
    """
    Confirma que propose() de verdad pasa por SandboxExecutor.execute_trusted()
    (vía el _FakeTestExecutor inyectado) en vez de correr pytest directo
    en el host — si _run_tests() alguna vez volviera a llamar
    subprocess.run() a mano, este test seguiría pasando "de casualidad"
    salvo por esta aserción explícita sobre las llamadas registradas.
    """
    manager.propose(target_path="mymodule.py", proposed_source="def add(a, b):\n    return a + b\n", justification="x")

    assert len(manager._executor.calls) == 2  # baseline + candidato
    call = manager._executor.calls[0]
    assert call["network_mode"] == "none"
    # El path montado es una COPIA temporal de fake_project (ver
    # SelfModificationManager._copy_project), nunca fake_project mismo
    # — solo confirmamos que se montó ALGO como /project, no un valor
    # específico.
    (mounted_container_path,) = call["extra_mounts"].values()
    assert mounted_container_path == "/project"


def test_run_tests_raises_on_sandbox_infrastructure_failure(manager):
    """
    BUG REAL ENCONTRADO EN REVISIÓN: sin este chequeo, un sandbox roto
    (Docker caído, build de imagen fallido) se interpretaría como
    "0 passed, 0 failed" — result.is_clean == True — y propose() dejaría
    pasar la propuesta a pending_human_approval sin haber corrido un
    solo test real.
    """
    class _BrokenExecutor:
        def execute_trusted(self, **kwargs):
            return _FakeSandboxResult(stdout="", stderr="daemon de Docker no responde", exit_code=None, status="error")

    manager._executor = _BrokenExecutor()

    with pytest.raises(SelfModTestRunError):
        manager.propose(
            target_path="mymodule.py", proposed_source="def add(a, b):\n    return a + b\n", justification="x",
        )


def test_run_tests_raises_when_junit_report_is_missing(manager):
    """
    status="success" pero sin report.xml (p.ej. pytest colapsó antes de
    terminar de escribirlo) — mismo criterio que el test anterior: nunca
    tratar la ausencia del reporte como "0 passed, 0 failed" en silencio.
    """
    class _NoReportExecutor:
        def execute_trusted(self, **kwargs):
            return _FakeSandboxResult(stdout="algo raro pasó", stderr="", exit_code=1, status="success", output_files={})

    manager._executor = _NoReportExecutor()

    with pytest.raises(SelfModTestRunError):
        manager.propose(
            target_path="mymodule.py", proposed_source="def add(a, b):\n    return a + b\n", justification="x",
        )


@requires_docker
class TestRealSandboxContainsMaliciousTopLevelCode:
    """
    _run_tests() con Docker REAL (construye/reusa la imagen del test
    runner la primera vez) — asumiendo el PEOR CASO, igual que
    tests/test_sandbox_escape_resistance.py: código de nivel de módulo
    que de alguna forma llegó a este punto sin haber sido detenido por
    el denylist AST (que su propio docstring admite que no puede ser
    exhaustivo contra un adversario que construye el AST para evadirlo).
    Por eso acá se llama a manager._run_tests() directo, sin pasar por
    propose()/validate_code() — la pregunta que responde no es "¿el
    denylist lo detecta?" sino "¿aunque no lo detecte, el sandbox de
    self-modification sigue conteniendo el daño?".
    """

    @staticmethod
    def _project_with_top_level_code(tmp_path, malicious_code: str):
        (tmp_path / "malicious.py").write_text(malicious_code, encoding="utf-8")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "__init__.py").write_text("", encoding="utf-8")
        (tests_dir / "test_malicious.py").write_text(
            "import malicious\n\n\ndef test_noop():\n    assert True\n", encoding="utf-8",
        )
        return tmp_path

    def test_top_level_network_access_is_blocked(self, tmp_path):
        project = self._project_with_top_level_code(
            tmp_path,
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
            "    print('RED_ALCANZADA')\n"
            "except OSError as e:\n"
            "    print('BLOQUEADO:', e)\n",
        )
        manager = SelfModificationManager(project_root=project)

        result = manager._run_tests(project, ["-q", "-s", "--tb=short"])

        assert "RED_ALCANZADA" not in result.raw_output
        assert "BLOQUEADO" in result.raw_output

    def test_top_level_code_cannot_read_files_outside_the_mounted_project(self, tmp_path):
        project = self._project_with_top_level_code(
            tmp_path,
            "try:\n"
            "    content = open('/etc/shadow').read()\n"
            "    print('LECTURA_EXITOSA')\n"
            "except PermissionError as e:\n"
            "    print('BLOQUEADO:', e)\n"
            "except FileNotFoundError:\n"
            "    print('BLOQUEADO: archivo no existe en esta imagen')\n",
        )
        manager = SelfModificationManager(project_root=project)

        result = manager._run_tests(project, ["-q", "-s", "--tb=short"])

        assert "LECTURA_EXITOSA" not in result.raw_output
        assert "BLOQUEADO" in result.raw_output

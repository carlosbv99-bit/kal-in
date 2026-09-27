"""
Tests de kernel/registry/sandboxed_skill.py::SandboxedSkillTool — el
Tool que reemplaza a una skill real: cada execute() corre DENTRO de un
contenedor Docker efímero (kernel/lifecycle/skill_runner.py), nunca en este
proceso.

Los tests con SandboxExecutor falso prueban la LÓGICA de
SandboxedSkillTool (armado de workspace_files, mapeo de permisos a
network_mode, manejo de _output.json/errores) sin Docker real — mismo
patrón que tests/test_tool_registry.py::FakeSandboxExecutor. Los
`requires_docker` prueban el pipeline COMPLETO de verdad.
"""
from __future__ import annotations

import json

import pytest

from kernel.lifecycle.docker_runner import DockerSandboxRunner, SandboxResult
from kernel.lifecycle.executor import SandboxExecutor
from kernel.registry.sandboxed_skill import SandboxedSkillTool
from sdk.skill import ToolManifest
from tests.conftest import requires_docker


class FakeSandboxExecutor:
    """Doble de prueba: devuelve un SandboxResult fijo, sin Docker real."""

    def __init__(self, result: SandboxResult | None = None):
        self.result = result or SandboxResult(status="success", stdout="", stderr="", exit_code=0)
        self.calls: list[dict] = []

    def execute_trusted(self, source_code, workspace_files=None, context=None, network_mode=None,
                         image=None, output_dir=None, granted_permissions=None, extra_mounts=None,
                         timeout_seconds=None):
        self.calls.append({
            "source_code": source_code, "workspace_files": workspace_files, "context": context,
            "network_mode": network_mode, "image": image, "output_dir": output_dir,
            "granted_permissions": granted_permissions, "extra_mounts": extra_mounts,
            "timeout_seconds": timeout_seconds,
        })
        return self.result


def _ok_result(modality="text", uri="", metadata=None, output_files=None) -> SandboxResult:
    payload = {"ok": True, "modality": modality, "uri": uri, "metadata": metadata or {}}
    files = {"_output.json": json.dumps(payload).encode("utf-8")}
    files.update(output_files or {})
    return SandboxResult(status="success", stdout="", stderr="", exit_code=0, output_files=files)


@pytest.fixture(autouse=True)
def _clean_scan_by_default(monkeypatch):
    """
    tool_integration/malware_scan.py::scan_bytes() es fail-closed de
    verdad (bloquea si ClamAV no está instalado) — el resto de este
    archivo prueba la lógica de SandboxedSkillTool, no el escaneo en
    sí (eso vive en tests/test_malware_scan.py), así que por defecto
    se simula "limpio" sin depender de tener ClamAV real instalado.
    Los tests puntuales del escaneo lo pisan explícitamente.
    """
    monkeypatch.setattr("kernel.registry.sandboxed_skill.scan_bytes", lambda data, suffix="": None)


@pytest.fixture
def manifest() -> ToolManifest:
    return ToolManifest(name="greet", description="saluda", created_by="system")


@pytest.fixture
def tool(tmp_path, manifest):
    skill_dir = tmp_path / "greeter"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("# skill de prueba\n", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("name: greeter\n", encoding="utf-8")

    fake = FakeSandboxExecutor(_ok_result(metadata={"summary": "hola"}))
    return SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:GreetTool",
        image="kal-sandbox-minimal:latest", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    ), fake


def test_execute_returns_text_artifact_on_success(tool):
    skill_tool, fake = tool
    artifact = skill_tool.execute(name="Kalin")

    assert artifact.modality == "text"
    assert artifact.metadata == {"summary": "hola"}
    assert fake.calls[0]["context"] == {"skill": "greet"}


def test_execute_passes_entry_point_and_kwargs_via_input_json(tool):
    skill_tool, fake = tool
    skill_tool.execute(name="Kalin")

    input_json = json.loads(fake.calls[0]["workspace_files"]["_input.json"])
    assert input_json == {"entry_point": "tool:GreetTool", "kwargs": {"name": "Kalin"}}


def test_execute_collects_skill_files_but_not_manifest(tool):
    skill_tool, fake = tool
    skill_tool.execute()

    files = fake.calls[0]["workspace_files"]
    assert "skill/tool.py" in files
    assert not any(k.endswith("skill.yaml") for k in files)


def test_network_permission_maps_to_bridge_network_mode(tmp_path):
    skill_dir = tmp_path / "networked"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    manifest = ToolManifest(name="networked", description="d", created_by="system", requires_network=True)
    fake = FakeSandboxExecutor(_ok_result())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    skill_tool.execute()

    assert fake.calls[0]["network_mode"] == "bridge"


def test_no_network_permission_means_no_network_mode_override(tool):
    skill_tool, fake = tool
    skill_tool.execute()
    assert fake.calls[0]["network_mode"] is None


def test_sandbox_failure_becomes_error_artifact(tmp_path, manifest):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(SandboxResult(status="error", stdout="", stderr="boom", exit_code=1))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "boom" in artifact.metadata["stderr"]


def test_missing_output_json_becomes_error_artifact(tmp_path, manifest):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(SandboxResult(status="success", stdout="", stderr="", exit_code=0, output_files={}))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "_output.json" in artifact.metadata["stderr"]


def test_skill_reported_failure_becomes_error_artifact(tmp_path, manifest):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    failure = {"ok": False, "error": "ValueError: algo salió mal dentro de la skill"}
    fake = FakeSandboxExecutor(SandboxResult(
        status="success", stdout="", stderr="", exit_code=0,
        output_files={"_output.json": json.dumps(failure).encode("utf-8")},
    ))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "algo salió mal dentro de la skill" in artifact.metadata["stderr"]


def test_file_artifact_is_persisted_to_artifacts_root(tmp_path, manifest):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    artifacts_root = tmp_path / "artifacts"
    fake = FakeSandboxExecutor(_ok_result(
        modality="image", uri="qr.png", output_files={"qr.png": b"\x89PNG-fake-bytes"},
    ))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=artifacts_root,
    )

    artifact = skill_tool.execute()

    assert artifact.modality == "image"
    assert artifact.uri != "qr.png"  # reescrito a una ruta real
    from pathlib import Path

    saved = Path(artifact.uri)
    assert saved.exists()
    assert saved.read_bytes() == b"\x89PNG-fake-bytes"
    assert saved.parent == artifacts_root / manifest.name


# --- Escaneo de malware (fail-closed) sobre artefactos de skills ---


def test_file_artifact_blocked_when_scan_detects_malware(tmp_path, manifest, monkeypatch):
    from tool_integration.malware_scan import MalwareScanError

    def _boom(data, suffix=""):
        raise MalwareScanError("ClamAV detectó contenido malicioso: FAKE.TEST-SIGNATURE")

    monkeypatch.setattr("kernel.registry.sandboxed_skill.scan_bytes", _boom)

    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    artifacts_root = tmp_path / "artifacts"
    fake = FakeSandboxExecutor(_ok_result(
        modality="image", uri="qr.png", output_files={"qr.png": b"contenido cualquiera"},
    ))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=artifacts_root,
    )

    artifact = skill_tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "bloqueado" in artifact.metadata["stderr"]
    assert list(artifacts_root.rglob("*.png")) == []  # nunca se escribió al host


def test_file_artifact_blocked_when_clamav_unavailable(tmp_path, manifest, monkeypatch):
    from tool_integration.malware_scan import MalwareScanError

    def _unavailable(data, suffix=""):
        raise MalwareScanError("ClamAV no está instalado — no se puede garantizar que este archivo sea seguro")

    monkeypatch.setattr("kernel.registry.sandboxed_skill.scan_bytes", _unavailable)

    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(_ok_result(
        modality="image", uri="qr.png", output_files={"qr.png": b"contenido cualquiera"},
    ))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute()

    assert artifact.metadata["status"] == "error"
    assert "no está instalado" in artifact.metadata["stderr"]


def test_scan_blocked_artifact_is_audited(tmp_path, manifest, monkeypatch):
    from audit.audit_log import audit_log
    from tool_integration.malware_scan import MalwareScanError

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    def _boom(data, suffix=""):
        raise MalwareScanError("detectado")

    monkeypatch.setattr("kernel.registry.sandboxed_skill.scan_bytes", _boom)

    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(_ok_result(
        modality="image", uri="qr.png", output_files={"qr.png": b"x"},
    ))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    skill_tool.execute()

    entries = audit_log.tail(1)
    assert entries[0]["event_type"] == "artifact_scan_blocked"
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["context"] == {"skill": "greet"}


def test_text_artifacts_are_never_scanned(tmp_path, manifest, monkeypatch):
    """system_info/qr_code (metadata pura) no producen un archivo — no
    hay nada que escanear, y no debería llamarse a scan_bytes."""
    calls = []
    monkeypatch.setattr("kernel.registry.sandboxed_skill.scan_bytes", lambda data, suffix="": calls.append(data))

    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(_ok_result(modality="text", metadata={"summary": "hola"}))
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
    )

    skill_tool.execute()

    assert calls == []


# --- Pipeline completo con Docker real ---


@requires_docker
def test_end_to_end_text_artifact_with_real_docker(tmp_path):
    skill_dir = tmp_path / "greeter"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "from sdk.skill import Tool, ToolManifest\nfrom sdk.artifacts import Artifact\n\n\n"
        "class GreetTool(Tool):\n"
        "    manifest = ToolManifest(name='greet', description='saluda')\n\n"
        "    def execute(self, **kwargs):\n"
        "        return Artifact(modality='text', uri='', metadata={'summary': f\"hola {kwargs.get('name', '')}\"})\n",
        encoding="utf-8",
    )

    manifest = ToolManifest(name="greet", description="saluda", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:GreetTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute(name="Kalin")

    assert artifact.modality == "text"
    assert artifact.metadata == {"summary": "hola Kalin"}


@requires_docker
def test_end_to_end_file_artifact_with_real_docker(tmp_path):
    """
    Valida la convención KAL_SKILL_OUTPUT_DIR de punta a punta: la
    skill escribe un archivo real dentro del contenedor, y debe
    terminar existiendo como archivo real en el host, con contenido
    idéntico.
    """
    skill_dir = tmp_path / "writer"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "import os\n"
        "from sdk.skill import Tool, ToolManifest\nfrom sdk.artifacts import Artifact\n\n\n"
        "class WriterTool(Tool):\n"
        "    manifest = ToolManifest(name='writer', description='escribe un archivo')\n\n"
        "    def execute(self, **kwargs):\n"
        "        out_dir = os.environ['KAL_SKILL_OUTPUT_DIR']\n"
        "        with open(os.path.join(out_dir, 'saludo.txt'), 'w') as f:\n"
        "            f.write('contenido real generado dentro del sandbox')\n"
        "        return Artifact(modality='document', uri='saludo.txt', metadata={})\n",
        encoding="utf-8",
    )

    manifest = ToolManifest(name="writer", description="escribe un archivo", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    artifacts_root = tmp_path / "artifacts"
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:WriterTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=artifacts_root,
    )

    artifact = skill_tool.execute()

    assert artifact.modality == "document"
    from pathlib import Path

    saved = Path(artifact.uri)
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "contenido real generado dentro del sandbox"


@requires_docker
def test_skill_without_network_permission_cannot_reach_internet(tmp_path):
    """
    Garantía de seguridad, no solo funcional: una skill que NO declaró
    el permiso NETWORK debe fallar al intentar conectarse a internet,
    igual que ya garantiza test_sandbox_integration.py para run_code —
    el aislamiento es el mismo mecanismo (network_mode="none" salvo que
    el manifiesto declare requires_network=True).
    """
    skill_dir = tmp_path / "curioso"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "import socket\n"
        "from sdk.skill import Tool, ToolManifest\nfrom sdk.artifacts import Artifact\n\n\n"
        "class CuriousTool(Tool):\n"
        "    manifest = ToolManifest(name='curioso', description='intenta conectarse a internet')\n\n"
        "    def execute(self, **kwargs):\n"
        "        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "        s.settimeout(3)\n"
        "        s.connect(('8.8.8.8', 53))\n"
        "        return Artifact(modality='text', uri='', metadata={'summary': 'CONEXION_EXITOSA'})\n",
        encoding="utf-8",
    )

    # requires_network=False (default): sin el permiso, no debería
    # poder alcanzar internet.
    manifest = ToolManifest(name="curioso", description="intenta conectarse a internet", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:CuriousTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
    )

    artifact = skill_tool.execute()

    assert artifact.metadata.get("summary") != "CONEXION_EXITOSA"
    assert artifact.metadata.get("status") == "error"


# --- Kernel Service Bus (kernel/__init__.py) ---


def test_kernel_services_declared_means_extra_mounts_is_passed(tmp_path, manifest):
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text("", encoding="utf-8")
    fake = FakeSandboxExecutor(_ok_result())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:X",
        image="img", sandbox=fake, artifacts_root=tmp_path / "artifacts",
        kernel_services=["image.generate"],
    )

    skill_tool.execute()

    assert fake.calls[0]["extra_mounts"] is not None
    assert list(fake.calls[0]["extra_mounts"].values()) == ["/workspace/.kal"]


def test_no_kernel_services_means_no_extra_mounts(tool):
    skill_tool, fake = tool
    skill_tool.execute()
    assert fake.calls[0]["extra_mounts"] is None


@requires_docker
def test_end_to_end_kernel_call_with_fake_service(tmp_path):
    """
    Valida la plomería COMPLETA del Kernel Service Bus con Docker real
    (socket montado, sdk/context.py copiado dentro
    del contenedor, permiso declarado respetado) — con un servicio
    FALSO (sin tocar SDXL-Turbo real, rápido).
    """
    from kernel.api.bus import KernelServiceBus

    class FakeEchoService:
        ALLOWED_ACTIONS = frozenset({"echo"})

        def echo(self, text):
            return {"echoed": text}

    fake_bus = KernelServiceBus()
    fake_bus.register("test", FakeEchoService())

    skill_dir = tmp_path / "llamador"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "from sdk.skill import Tool\nfrom sdk.artifacts import Artifact\n"
        "from sdk.context import call as kernel_call\n\n\n"
        "class LlamadorTool(Tool):\n"
        "    def execute(self, **kwargs):\n"
        "        result = kernel_call('test.echo', text='hola desde el contenedor')\n"
        "        return Artifact(modality='text', uri='', metadata={'echoed': result['echoed']})\n",
        encoding="utf-8",
    )

    manifest = ToolManifest(name="llamador", description="llama al bus", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:LlamadorTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=["test.echo"], kernel_bus_instance=fake_bus,
    )

    artifact = skill_tool.execute()

    assert artifact.metadata == {"echoed": "hola desde el contenedor"}


@requires_docker
def test_end_to_end_kernel_denies_method_not_declared_in_kernel_services(tmp_path):
    """
    La skill declara kernel_services=["test.echo"] pero su CÓDIGO
    intenta llamar a "test.otro_metodo" (no declarado) — el servidor
    debe rechazarlo ANTES de tocar el servicio real, sin importar que
    "test.otro_metodo" exista de verdad en el bus.
    """
    from kernel.api.bus import KernelServiceBus

    class FakeEchoService:
        # "otro_metodo" SÍ está en ALLOWED_ACTIONS (es una acción real y
        # permitida del bus) — lo que este test verifica es el rechazo
        # de la capa de arriba (allowed_methods, ver kernel_services más
        # abajo), independiente de la allowlist del propio servicio.
        ALLOWED_ACTIONS = frozenset({"echo", "otro_metodo"})

        def echo(self, text):
            return {"echoed": text}

        def otro_metodo(self):
            return {"no_deberia_llegar_aca": True}

    fake_bus = KernelServiceBus()
    fake_bus.register("test", FakeEchoService())

    skill_dir = tmp_path / "curioso_del_kernel"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "from sdk.skill import Tool\nfrom sdk.artifacts import Artifact\n"
        "from sdk.context import call, KernelError\n\n\n"
        "class CuriosoTool(Tool):\n"
        "    def execute(self, **kwargs):\n"
        "        try:\n"
        "            call('test.otro_metodo')\n"
        "            return Artifact(modality='text', uri='', metadata={'summary': 'LLAMADA_EXITOSA'})\n"
        "        except KernelError as e:\n"
        "            return Artifact(modality='text', uri='', metadata={'summary': f'RECHAZADO: {e}'})\n",
        encoding="utf-8",
    )

    manifest = ToolManifest(name="curioso_del_kernel", description="d", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:CuriosoTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=["test.echo"],  # NO incluye "test.otro_metodo"
        kernel_bus_instance=fake_bus,
    )

    artifact = skill_tool.execute()

    assert "RECHAZADO" in artifact.metadata["summary"]


@requires_docker
def test_end_to_end_kernel_resolves_artifact_passed_between_two_calls(tmp_path):
    """
    Plomería COMPLETA (Docker real + socket real) para el caso que
    motivó la resolución de artefactos de ENTRADA en
    kernel/api/bus.py::KernelServiceBus._resolve_input_artifacts()
    (agregado junto con audio.synthesize/stt.transcribe/image.inpaint):
    una skill llama a una acción que PRODUCE un artefacto, y pasa esa
    misma referencia "artifact://..." como entrada de una segunda
    llamada — con servicios FALSOS (instantáneos), para validar el
    protocolo en sí, no un modelo real.
    """
    from kernel.api.bus import KernelServiceBus

    class FakeProducerConsumerService:
        ALLOWED_ACTIONS = frozenset({"produce", "consume"})

        def produce(self):
            return {"artifact": "artifact://fake/1", "path": "/no/existe/pero/no/importa.bin", "metadata": {}}

        def consume(self, path):
            return {"received_path": path}

    fake_bus = KernelServiceBus()
    fake_bus.register("test", FakeProducerConsumerService())

    skill_dir = tmp_path / "encadenador"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(
        "from sdk.skill import Tool\nfrom sdk.artifacts import Artifact\n"
        "from sdk.context import call as kernel_call\n\n\n"
        "class EncadenadorTool(Tool):\n"
        "    def execute(self, **kwargs):\n"
        "        produced = kernel_call('test.produce')\n"
        "        consumed = kernel_call('test.consume', path=produced['artifact'])\n"
        "        return Artifact(modality='text', uri='', metadata=consumed)\n",
        encoding="utf-8",
    )

    manifest = ToolManifest(name="encadenador", description="encadena dos llamadas del bus", created_by="system")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=skill_dir, entry_point="tool:EncadenadorTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=["test.produce", "test.consume"], kernel_bus_instance=fake_bus,
    )

    artifact = skill_tool.execute()

    # La skill nunca vio la ruta real — solo pasó la referencia opaca
    # que le devolvió la primera llamada. El kernel la resolvió antes
    # de invocar la segunda acción.
    assert artifact.metadata == {"received_path": "/no/existe/pero/no/importa.bin"}

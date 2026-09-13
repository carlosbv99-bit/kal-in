"""
Test de integración REAL del Kernel Service Bus + DownloadService — sin
dobles de prueba en la sandbox: Docker real, socket real, ClamAV real,
validación real de imagen. Corre skills/download_via_kernel/, la skill
de referencia SIN Permission.NETWORK (network_mode=None dentro del
contenedor — ver kernel/registry/sandboxed_skill.py), de punta a punta:
confirma que una Skill sin ningún permiso de red puede igual bajar un
archivo real pidiéndoselo al kernel.

La única pieza no-real acá es la conexión HTTP en sí (`get_fn`
inyectado en el DownloadManager de este servicio, mismo criterio que
tests/test_download_manager.py) — evita depender de Internet real en
la suite; todo lo demás (dominio permitido vía
NetworkAccessManager real, escaneo ClamAV real, validación real de
PNG, Docker real) corre sin ningún doble.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from tests.conftest import requires_docker
from kernel.api.bus import KernelServiceBus
from kernel.lifecycle.docker_runner import DockerSandboxRunner
from kernel.lifecycle.executor import SandboxExecutor
from kernel.permissions.network_access_manager import NetworkAccessManager
from kernel.registry.sandboxed_skill import SandboxedSkillTool
from tool_integration.services import DownloadService
from sdk.skill import ToolManifest
from tool_integration.download_manager import DownloadManager

pytestmark = requires_docker

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "download_via_kernel"


class _FakeResponse:
    def __init__(self, content: bytes):
        self._content = content
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), 1024):
            yield self._content[i : i + 1024]


def _real_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(buf, format="PNG")
    return buf.getvalue()


def test_download_via_kernel_skill_gets_a_real_downloaded_artifact_without_any_network_permission(tmp_path, monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings.downloads, "allowed_domains", ["pexels.com"])
    monkeypatch.setattr(settings.downloads, "artifact_dir", str(tmp_path / "download_artifacts"))

    fake_download_manager = DownloadManager(
        get_fn=lambda *a, **kw: _FakeResponse(_real_png_bytes()),
        resolve_fn=lambda host: ["1.2.3.4"],  # IP pública cualquiera, no privada/reservada
    )
    # NetworkAccessManager real (no un doble) — solo con sus grants en
    # tmp_path para no tocar el archivo real compartido entre corridas.
    real_network_access_manager = NetworkAccessManager(grants_path=tmp_path / "network_grants.json")

    bus = KernelServiceBus()
    bus.register(
        "download",
        DownloadService(download_manager=fake_download_manager, network_access_manager=real_network_access_manager),
    )

    manifest = ToolManifest(
        name="download_via_kernel", description="descarga un recurso vía el kernel", created_by="skill",
    )
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=SKILL_DIR, entry_point="tool:DownloadViaKernelTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=["download.fetch"], kernel_bus_instance=bus,
    )

    artifact = skill_tool.execute(url="https://pexels.com/foo.png", expected_type="image")

    assert artifact.modality == "image"
    path = Path(artifact.uri)
    assert path.exists()
    assert path.read_bytes() == _real_png_bytes()
    assert artifact.metadata["mime"] == "image/png"

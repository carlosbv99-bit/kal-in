"""
Tests de tool_integration/services.py::DownloadService — el Kernel
Download Service (2026-07-24): para que una Skill de terceros pueda
bajar un archivo real de Internet SIN necesitar Permission.NETWORK
(que le daría red cruda sin validar nada, ver
kernel/registry/sandboxed_skill.py). Con un DownloadManager y un
NetworkAccessManager falsos — la validación real (IP-safety/ClamAV/
tamaño) ya está cubierta en tests/test_download_manager.py, acá se
prueba la lógica PROPIA de este servicio: el gate de permiso por-skill
y el empaquetado como artefacto.
"""
from __future__ import annotations

import pytest

from tool_integration.services import DownloadService, KernelServiceError
from tool_integration.download_manager import DownloadedResource, DownloadValidationError


class FakeDownloadManager:
    def __init__(self, resource=None, error=None):
        self.resource = resource
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def download_and_validate(self, url, expected_type):
        self.calls.append((url, expected_type))
        if self.error is not None:
            raise self.error
        return self.resource


class FakeNetworkAccessManager:
    def __init__(self, decision="auto_allowed"):
        self.decision = decision
        self.evaluate_calls: list[dict] = []
        self.pending_calls: list[dict] = []

    def evaluate(self, skill_name, scope, action, resource_key):
        self.evaluate_calls.append(
            {"skill_name": skill_name, "scope": scope, "action": action, "resource_key": resource_key}
        )
        return self.decision

    def create_pending_request(self, skill_name, scope, action, resource_key):
        self.pending_calls.append(
            {"skill_name": skill_name, "scope": scope, "action": action, "resource_key": resource_key}
        )
        return type("PendingStub", (), {"id": "req-123"})()


def _resource() -> DownloadedResource:
    return DownloadedResource(content=b"fake-png-bytes", sha256="deadbeef", mime="image/png", size_bytes=14)


def test_fetch_succeeds_when_domain_is_auto_allowed(tmp_path):
    dm = FakeDownloadManager(resource=_resource())
    nam = FakeNetworkAccessManager(decision="auto_allowed")
    cfg = type("Cfg", (), {"artifact_dir": str(tmp_path)})()
    service = DownloadService(cfg=cfg, download_manager=dm, network_access_manager=nam)

    result = service.fetch(url="https://pexels.com/foo.png", expected_type="image", skill_name="download_via_kernel")

    assert result["artifact"].startswith("artifact://download/")
    assert result["metadata"]["sha256"] == "deadbeef"
    assert result["metadata"]["mime"] == "image/png"
    assert dm.calls == [("https://pexels.com/foo.png", "image")]
    # El archivo se guardó de verdad en artifact_dir, con los bytes reales.
    from pathlib import Path

    saved = Path(result["path"])
    assert saved.exists()
    assert saved.read_bytes() == b"fake-png-bytes"


def test_fetch_evaluates_permission_with_the_real_hostname_and_calling_skill(tmp_path):
    dm = FakeDownloadManager(resource=_resource())
    nam = FakeNetworkAccessManager(decision="auto_allowed")
    cfg = type("Cfg", (), {"artifact_dir": str(tmp_path)})()
    service = DownloadService(cfg=cfg, download_manager=dm, network_access_manager=nam)

    service.fetch(url="https://pexels.com/foo.png", expected_type="image", skill_name="download_via_kernel")

    assert nam.evaluate_calls[0]["skill_name"] == "download_via_kernel"
    assert nam.evaluate_calls[0]["resource_key"] == "pexels.com"


def test_fetch_raises_and_never_downloads_when_domain_requires_approval(tmp_path):
    dm = FakeDownloadManager(resource=_resource())
    nam = FakeNetworkAccessManager(decision="requires_approval")
    cfg = type("Cfg", (), {"artifact_dir": str(tmp_path)})()
    service = DownloadService(cfg=cfg, download_manager=dm, network_access_manager=nam)

    with pytest.raises(KernelServiceError, match="aprobación humana"):
        service.fetch(url="https://dominio-no-permitido.com/foo.png", expected_type="image", skill_name="alguna_skill")

    assert dm.calls == []  # nunca se intenta la descarga sin permiso
    assert len(nam.pending_calls) == 1  # queda registrado un pedido pendiente real


def test_fetch_wraps_a_download_validation_error_as_a_kernel_service_error(tmp_path):
    dm = FakeDownloadManager(error=DownloadValidationError("el contenido no es una imagen válida"))
    nam = FakeNetworkAccessManager(decision="auto_allowed")
    cfg = type("Cfg", (), {"artifact_dir": str(tmp_path)})()
    service = DownloadService(cfg=cfg, download_manager=dm, network_access_manager=nam)

    with pytest.raises(KernelServiceError, match="no es una imagen válida"):
        service.fetch(url="https://pexels.com/foo.png", expected_type="image", skill_name="download_via_kernel")

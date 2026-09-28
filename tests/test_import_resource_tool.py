"""
Tests de tool_integration/adapters/vscode_files.py::ImportResourceTool
— Artifact Service Fase 1 (descarga real de imágenes). download_manager
y filesystem_access_manager mockeados: lo que se prueba acá es la
lógica de la Tool (validación de path, manejo de errores, forma del
Artifact devuelto, auditoría), no la descarga real en sí (eso ya lo
cubre tests/test_download_manager.py).
"""
from __future__ import annotations

import base64

import pytest

from tool_integration.adapters.vscode_files import (
    ImportResourceTool,
    ProjectFilesRejectedError,
)
from tool_integration.download_manager import (
    DownloadedResource,
    DownloadValidationError,
)


def _fake_resource(content=b"fake-image-bytes"):
    return DownloadedResource(content=content, sha256="abc123", mime="image/png", size_bytes=len(content))


@pytest.fixture(autouse=True)
def _redirect_audit_log(tmp_path, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")


def test_rejects_an_absolute_destination_path():
    tool = ImportResourceTool()
    with pytest.raises(ProjectFilesRejectedError, match="absoluta"):
        tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="/etc/passwd")


def test_rejects_a_destination_path_that_escapes_with_dotdot():
    tool = ImportResourceTool()
    with pytest.raises(ProjectFilesRejectedError, match="\\.\\."):
        tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="../fuera.jpg")


def test_download_validation_error_becomes_a_clear_text_artifact(monkeypatch):
    """URL de un dominio YA permitido (pasa el gate de red) — el error
    simulado acá es de OTRA causa (malware/tamaño/no-es-una-imagen-real),
    no de dominio (eso ahora lo intercepta el gate de red ANTES, ver
    test_domain_not_allowed_requires_network_approval_instead_of_downloading)."""
    import tool_integration.adapters.vscode_files as module

    def _raise(*a, **kw):
        raise DownloadValidationError("el contenido descargado no es una imagen válida")

    monkeypatch.setattr(module.download_manager, "download_and_validate", _raise)
    tool = ImportResourceTool()

    artifact = tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="assets/x.jpg")

    assert artifact.modality == "text"
    assert artifact.metadata["status"] == "error"
    assert "no es una imagen válida" in artifact.metadata["stderr"]


def test_domain_not_allowed_requires_network_approval_instead_of_downloading(monkeypatch):
    """
    BUG REAL ENCONTRADO EN USO: antes de existir el Access Manager,
    un dominio no permitido rechazaba con un error inmediato, sin
    ningún camino de escalar a un humano. Ahora crea una solicitud
    pendiente real (ver GET /network-access) y NUNCA llega a intentar
    la descarga.
    """
    import tool_integration.adapters.vscode_files as module

    download_calls = []
    monkeypatch.setattr(
        module.download_manager, "download_and_validate",
        lambda *a, **kw: download_calls.append((a, kw)) or _fake_resource(),
    )
    tool = ImportResourceTool()

    artifact = tool.execute(type="image", url="https://evil.com/x.jpg", destination_path="assets/x.jpg")

    assert artifact.modality == "text"
    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.metadata["resource_kind"] == "network"
    assert artifact.metadata["request_id"]
    assert download_calls == []  # nunca se intentó descargar nada


def test_fails_closed_when_access_requires_approval(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.download_manager, "download_and_validate", lambda *a, **kw: _fake_resource())
    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "requires_approval")
    tool = ImportResourceTool()

    artifact = tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="assets/x.jpg")

    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.metadata["files"] == []


def test_proposes_the_file_with_base64_encoding_when_auto_allowed(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    content = b"\x89PNG-fake-bytes"
    monkeypatch.setattr(module.download_manager, "download_and_validate", lambda *a, **kw: _fake_resource(content))
    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ImportResourceTool()

    artifact = tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="assets/foto.jpg")

    assert artifact.modality == "project_files"
    assert artifact.metadata["status"] == "proposed"
    assert artifact.metadata["request_id"]
    files = artifact.metadata["files"]
    assert len(files) == 1
    assert files[0]["path"] == "assets/foto.jpg"
    assert files[0]["encoding"] == "base64"
    assert base64.b64decode(files[0]["content"]) == content


def test_calls_the_permission_manager_with_the_expected_arguments(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.download_manager, "download_and_validate", lambda *a, **kw: _fake_resource())
    calls = []
    monkeypatch.setattr(
        module.filesystem_access_manager, "evaluate", lambda **kwargs: calls.append(kwargs) or "auto_allowed"
    )
    tool = ImportResourceTool()

    tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="assets/x.jpg")

    assert calls[0]["skill_name"] == "vscode_integration"
    assert calls[0]["scope"].value == "workspace"
    assert calls[0]["action"].value == "create"


def test_records_an_artifact_imported_audit_event(monkeypatch):
    import tool_integration.adapters.vscode_files as module
    from audit.audit_log import audit_log

    monkeypatch.setattr(module.download_manager, "download_and_validate", lambda *a, **kw: _fake_resource())
    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ImportResourceTool()

    tool.execute(type="image", url="https://unsplash.com/x.jpg", destination_path="assets/x.jpg")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "artifact_imported"
    assert entries[0]["context"]["url"] == "https://unsplash.com/x.jpg"
    assert entries[0]["context"]["sha256"] == "abc123"

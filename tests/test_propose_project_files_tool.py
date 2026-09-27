"""
Tests de tool_integration/adapters/vscode_files.py::ProposeProjectFilesTool
— nunca escribe nada a disco (eso es responsabilidad de la extensión de
VS Code, el backend no conoce el workspace real), solo valida rutas y
consulta al Permission Manager del Kernel.
"""
from __future__ import annotations

import pytest

from tool_integration.adapters.vscode_files import (
    ProjectFilesRejectedError,
    ProposeProjectFilesTool,
)


def test_proposes_files_when_access_is_auto_allowed(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ProposeProjectFilesTool()

    artifact = tool.execute(files=[{"path": "index.html", "content": "<html></html>"}])

    assert artifact.modality == "project_files"
    assert artifact.metadata["status"] == "proposed"
    assert artifact.metadata["files"] == [{"path": "index.html", "content": "<html></html>"}]
    assert artifact.metadata["request_id"]


def test_calls_the_permission_manager_with_the_expected_arguments(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    calls = []
    monkeypatch.setattr(
        module.filesystem_access_manager, "evaluate",
        lambda **kwargs: calls.append(kwargs) or "auto_allowed",
    )
    tool = ProposeProjectFilesTool()

    tool.execute(files=[{"path": "a.txt", "content": "x"}])

    assert calls[0]["skill_name"] == "vscode_integration"
    assert calls[0]["scope"].value == "workspace"
    assert calls[0]["action"].value == "create"


def test_fails_closed_when_access_requires_approval(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "requires_approval")
    tool = ProposeProjectFilesTool()

    artifact = tool.execute(files=[{"path": "index.html", "content": "<html></html>"}])

    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.metadata["files"] == []


def test_rejects_an_empty_files_list():
    tool = ProposeProjectFilesTool()
    with pytest.raises(ProjectFilesRejectedError, match="'files'"):
        tool.execute(files=[])


def test_rejects_an_absolute_path():
    tool = ProposeProjectFilesTool()
    with pytest.raises(ProjectFilesRejectedError, match="absoluta"):
        tool.execute(files=[{"path": "/etc/passwd", "content": "x"}])


def test_rejects_a_path_that_escapes_with_dotdot():
    tool = ProposeProjectFilesTool()
    with pytest.raises(ProjectFilesRejectedError, match="\\.\\."):
        tool.execute(files=[{"path": "../../etc/passwd", "content": "x"}])


def test_rejects_an_empty_path():
    tool = ProposeProjectFilesTool()
    with pytest.raises(ProjectFilesRejectedError, match="vacía"):
        tool.execute(files=[{"path": "", "content": "x"}])


def test_accepts_a_relative_path_with_subfolders(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ProposeProjectFilesTool()

    artifact = tool.execute(files=[{"path": "css/estilos.css", "content": "body {}"}])

    assert artifact.metadata["files"][0]["path"] == "css/estilos.css"


# --- Extensiones binarias reales (imagen/audio/video/fuente) — nunca texto ---
#
# BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): pedido de agregar
# fotos a una página de menú — el modelo, sin llamar a browser ni a
# import_resource, propuso "plato1.jpg"/"plato2.jpg" cuyo "contenido"
# era el texto literal "https://example.com/path/to/plato1.jpg" — ni
# una imagen real ni una descarga real, un archivo de texto con
# extensión de imagen (ícono roto en la vista previa del usuario).


@pytest.mark.parametrize(
    "path", ["fotos/plato1.jpg", "audio.mp3", "video.mp4", "fuente.woff2", "documento.pdf", "PLATO1.JPG"]
)
def test_rejects_a_binary_media_extension(path):
    tool = ProposeProjectFilesTool()
    with pytest.raises(ProjectFilesRejectedError, match="binario"):
        tool.execute(files=[{"path": path, "content": "https://example.com/path/to/plato1.jpg"}])


def test_rejects_the_whole_proposal_if_any_single_file_is_a_binary_extension(monkeypatch):
    """Todo o nada: un HTML legítimo junto a una imagen fabricada
    rechaza la propuesta ENTERA, no solo el archivo problemático — el
    usuario nunca debería ver una vista previa a medio armar."""
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ProposeProjectFilesTool()

    with pytest.raises(ProjectFilesRejectedError, match="binario"):
        tool.execute(files=[
            {"path": "menu.html", "content": "<html></html>"},
            {"path": "assets/plato1.jpg", "content": "https://example.com/path/to/plato1.jpg"},
        ])


def test_accepts_svg_since_it_is_plain_text_xml(monkeypatch):
    """.svg queda afuera del bloqueo a propósito: es XML de texto
    plano, un ícono vectorial escrito a mano es un uso legítimo."""
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ProposeProjectFilesTool()

    artifact = tool.execute(files=[{"path": "icono.svg", "content": "<svg></svg>"}])

    assert artifact.metadata["files"][0]["path"] == "icono.svg"

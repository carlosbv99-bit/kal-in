"""
Tests de tool_integration/adapters/vscode_files.py::ReadWorkspaceFileTool
— pieza mínima de "Editor Context Provider" (2026-07-20). Nunca lee
nada del disco real ella misma (mismo límite arquitectónico que
ProposeProjectFilesTool/ImportResourceTool): devuelve un Artifact
"pending" con la ruta pedida, la extensión de VS Code resuelve la
lectura real y encadena la respuesta.
"""
from __future__ import annotations

import pytest

from tool_integration.adapters.vscode_files import (
    ProjectFilesRejectedError,
    ReadWorkspaceFileTool,
)


def test_rejects_an_absolute_path():
    tool = ReadWorkspaceFileTool()
    with pytest.raises(ProjectFilesRejectedError, match="absoluta"):
        tool.execute(path="/etc/passwd")


def test_rejects_a_path_that_escapes_with_dotdot():
    tool = ReadWorkspaceFileTool()
    with pytest.raises(ProjectFilesRejectedError, match="\\.\\."):
        tool.execute(path="../fuera.txt")


def test_rejects_an_empty_path():
    tool = ReadWorkspaceFileTool()
    with pytest.raises(ProjectFilesRejectedError, match="vacía"):
        tool.execute(path="")


def test_returns_a_pending_workspace_file_request_when_auto_allowed(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    tool = ReadWorkspaceFileTool()

    artifact = tool.execute(path="restaurante-web/index.html")

    assert artifact.modality == "workspace_file_request"
    assert artifact.metadata["status"] == "pending"
    assert artifact.metadata["path"] == "restaurante-web/index.html"
    assert artifact.metadata["request_id"]


def test_calls_the_permission_manager_with_read_action(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    calls = []
    monkeypatch.setattr(
        module.filesystem_access_manager, "evaluate", lambda **kwargs: calls.append(kwargs) or "auto_allowed"
    )
    tool = ReadWorkspaceFileTool()

    tool.execute(path="index.html")

    assert calls[0]["skill_name"] == "vscode_integration"
    assert calls[0]["scope"].value == "workspace"
    assert calls[0]["action"].value == "read"


def test_fails_closed_when_access_requires_approval(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "requires_approval")
    monkeypatch.setattr(
        module.filesystem_access_manager, "create_pending_request",
        lambda **kwargs: type("P", (), {"id": "pending-id-123"})(),
    )
    tool = ReadWorkspaceFileTool()

    artifact = tool.execute(path="index.html")

    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.metadata["resource_kind"] == "filesystem"
    assert artifact.metadata["request_id"] == "pending-id-123"


def test_does_not_create_a_pending_request_when_auto_allowed(monkeypatch):
    import tool_integration.adapters.vscode_files as module

    monkeypatch.setattr(module.filesystem_access_manager, "evaluate", lambda **kwargs: "auto_allowed")
    calls = []
    monkeypatch.setattr(
        module.filesystem_access_manager, "create_pending_request",
        lambda **kwargs: calls.append(kwargs),
    )
    tool = ReadWorkspaceFileTool()

    tool.execute(path="index.html")

    assert calls == []

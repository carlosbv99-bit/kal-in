"""
Integración con VS Code: instala la extensión de kal en el VS Code
del usuario (compilar + empaquetar + `code --install-extension`),
reusando la misma lógica ya probada a mano en scripts/setup_all.sh.

Deliberadamente NO instala VS Code mismo (se asume que el usuario ya
lo tiene) ni introduce un protocolo de handshake nuevo — la extensión
ya habla HTTP simple contra este mismo backend (ver
vscode-extension/README.md). Ver docs/HISTORY.md para la discusión
completa de por qué se escopó así (v1, no el "Integration Manager"
completo que se evaluó y se dejó documentado como visión a futuro).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from audit.audit_log import AuditEvent, audit_log

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXTENSION_DIR = _REPO_ROOT / "vscode-extension"
_EXTENSION_ID = "undefined_publisher.kal-vscode"
_STEP_TIMEOUT_SECONDS = 300


class VSCodeIntegrationError(Exception):
    """Algún paso de la instalación de la extensión falló."""


def is_code_cli_available() -> bool:
    return shutil.which("code") is not None


def is_extension_installed() -> bool:
    if not is_code_cli_available():
        return False
    result = subprocess.run(
        ["code", "--list-extensions"], capture_output=True, text=True, timeout=30, check=False,
    )
    return _EXTENSION_ID.lower() in result.stdout.lower()


def get_status() -> dict:
    return {
        "code_cli_available": is_code_cli_available(),
        "installed": is_extension_installed(),
    }


def install_extension() -> str:
    """
    Compila, empaqueta e instala la extensión de kal en VS Code.
    Levanta VSCodeIntegrationError (con el detalle del paso que falló)
    si algo sale mal. Cada intento queda auditado, éxito o fracaso.
    """
    try:
        message = _install_extension_unaudited()
    except VSCodeIntegrationError as e:
        audit_log.record(
            AuditEvent(
                event_type="vscode_extension_installed",
                summary=f"Instalación de la extensión de VS Code falló: {e}",
                outcome="failure",
            )
        )
        raise
    audit_log.record(
        AuditEvent(
            event_type="vscode_extension_installed",
            summary="Extensión de kal instalada en VS Code",
            outcome="success",
        )
    )
    return message


def _install_extension_unaudited() -> str:
    if not is_code_cli_available():
        raise VSCodeIntegrationError(
            "El comando 'code' no está en el PATH. En VS Code: Ctrl+Shift+P → "
            "'Shell Command: Install code command in PATH', y reintentá."
        )
    if shutil.which("npm") is None:
        raise VSCodeIntegrationError("npm no está instalado — necesario para compilar la extensión.")

    _run(["npm", "install"], step="npm install")
    _run(["npm", "run", "compile"], step="npm run compile")

    vsix_path = _EXTENSION_DIR / "kal-vscode.vsix"
    try:
        _run(
            [
                "npx", "--yes", "@vscode/vsce", "package", "--no-dependencies",
                "--allow-missing-repository", "--out", str(vsix_path),
            ],
            step="vsce package",
        )
        _run(["code", "--install-extension", str(vsix_path), "--force"], step="code --install-extension")
    finally:
        vsix_path.unlink(missing_ok=True)

    return "Extensión de kal instalada en VS Code."


def _run(cmd: list[str], step: str) -> None:
    result = subprocess.run(
        cmd, cwd=str(_EXTENSION_DIR), capture_output=True, text=True, timeout=_STEP_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0:
        raise VSCodeIntegrationError(f"Falló '{step}': {(result.stderr or result.stdout).strip()}")

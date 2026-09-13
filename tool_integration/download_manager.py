"""
Artifact Service (Fase 1: descarga real de recursos, hoy solo
imágenes) — descarga bytes reales desde una URL, con rechazo de IPs
privadas/reservadas (DNS rebinding, ver kernel/permissions/network_safety.py),
y además específico de descargar un ARCHIVO (no navegar
interactivamente): tope de tamaño real (nunca se descarga "todo
primero y se mide después") y escaneo de malware con ClamAV
(tool_integration/malware_scan.py, ya construido para artefactos de
skills, reusado tal cual acá).

BUG REAL ENCONTRADO EN USO: este módulo tenía SU PROPIO chequeo de
allowlist de dominios (`is_domain_allowed` contra
`settings.downloads.allowed_domains`, estático) — una SEGUNDA fuente
de verdad, separada de kernel/permissions/network_access_manager.py (la
oficial desde que existe, que combina esa misma política estática CON
concesiones dinámicas otorgadas tras una aprobación humana). Cuando un
humano aprobaba un dominio nuevo vía el Access Manager, la descarga
SEGUÍA fallando acá — este chequeo no sabía nada de esa concesión. Se
quitó a propósito: el gate de dominio es responsabilidad exclusiva de
quien llama a este módulo (ver
tool_integration/adapters/vscode_files.py::ImportResourceTool, que
llama a `network_access_manager.evaluate()` ANTES de esta función) —
un único lugar de verdad para "¿este dominio está permitido?", en vez
de dos que puedan divergir. El resto de las validaciones (IP seguna,
tamaño, malware, contenido real) siguen acá, sin cambios.

Usado por tool_integration/adapters/vscode_files.py::ImportResourceTool
(agente de VS Code, proceso host) Y por
tool_integration/services.py::DownloadService (2026-07-24, Kernel
Download Service — expuesto vía el Kernel Service Bus para que una
Skill de terceros pueda bajar un archivo real SIN necesitar
Permission.NETWORK, que le daría red cruda sin ninguna de estas
validaciones, ver kernel/registry/sandboxed_skill.py). Cada consumidor
gatea el dominio a SU manera (ImportResourceTool con su propia UX de
aprobación interactiva + gate de filesystem; DownloadService con el
skill_name real de quien llama) antes de llegar acá — el mecanismo es
genérico a propósito (`expected_type` es un parámetro, no algo
hardcodeado a "image"), pero solo "image" tiene un validador real
implementado — cualquier otro valor falla cerrado con un mensaje
claro, nunca acepta un binario sin poder confirmar de verdad qué es.
"""
from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass
from typing import Any, Callable

import requests

from tool_integration.malware_scan import MalwareScanError, scan_bytes
from kernel.permissions.network_safety import is_unsafe_ip
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

GetFn = Callable[..., Any]
ResolveFn = Callable[[str], list[str]]

_CHUNK_SIZE = 64 * 1024


class DownloadValidationError(Exception):
    """La URL/dominio/tamaño/contenido no pasó alguna validación — nunca se devuelve nada parcial."""


@dataclass
class DownloadedResource:
    content: bytes
    sha256: str
    mime: str
    size_bytes: int


def _default_resolve(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise DownloadValidationError(f"No se pudo resolver el host '{hostname}': {e}") from e
    return [info[4][0] for info in infos]


def _validate_image_bytes(content: bytes) -> str:
    """Devuelve el mime real si `content` decodifica como una imagen de
    verdad; levanta DownloadValidationError si no (bytes basura con
    extensión de imagen, contenido truncado, etc.)."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
            fmt = (img.format or "").lower()
    except Exception as e:
        raise DownloadValidationError(f"El contenido descargado no es una imagen válida: {e}") from e
    return f"image/{fmt}" if fmt else "image/unknown"


# Un validador real por tipo soportado — cualquier `expected_type` que
# no esté acá se rechaza explícitamente, nunca se acepta un binario
# sin poder confirmar de verdad qué es (ver docstring del módulo).
_VALIDATORS: dict[str, Callable[[bytes], str]] = {
    "image": _validate_image_bytes,
}


class DownloadManager:
    def __init__(self, get_fn: GetFn | None = None, resolve_fn: ResolveFn | None = None):
        self._get = get_fn or requests.get
        self._resolve = resolve_fn or _default_resolve

    def download_and_validate(self, url: str, expected_type: str) -> DownloadedResource:
        from urllib.parse import urlparse

        if expected_type not in _VALIDATORS:
            raise DownloadValidationError(
                f"Tipo de recurso '{expected_type}' no soportado todavía — hoy solo 'image'."
            )

        scheme = urlparse(url).scheme.lower()
        if scheme == "http" and not settings.downloads.allow_http:
            raise DownloadValidationError(
                f"'{url}' usa http (no https) — downloads.allow_http está deshabilitado por default "
                "(una respuesta http puede alterarse en tránsito)."
            )
        if scheme not in ("http", "https"):
            raise DownloadValidationError(f"'{url}' no es una URL http/https real.")

        hostname = urlparse(url).hostname or ""
        resolved_ips = self._resolve(hostname)
        if not resolved_ips or any(is_unsafe_ip(ip) for ip in resolved_ips):
            raise DownloadValidationError(
                f"'{hostname}' resolvió a una dirección IP privada/reservada/no determinable "
                "— posible DNS rebinding, no se descarga nada."
            )

        content = self._stream_download(url)

        try:
            scan_bytes(content, suffix=f".{expected_type}")
        except MalwareScanError as e:
            raise DownloadValidationError(f"Escaneo de seguridad rechazó el contenido descargado: {e}") from e

        mime = _VALIDATORS[expected_type](content)

        return DownloadedResource(
            content=content, sha256=hashlib.sha256(content).hexdigest(), mime=mime, size_bytes=len(content),
        )

    def _stream_download(self, url: str) -> bytes:
        max_bytes = settings.downloads.max_size_mb * 1024 * 1024
        try:
            response = self._get(url, stream=True, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise DownloadValidationError(f"No se pudo descargar '{url}': {e}") from e

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise DownloadValidationError(
                    f"'{url}' supera el tamaño máximo permitido ({settings.downloads.max_size_mb}MB) — "
                    "se aborta la descarga, nada se guarda."
                )
            chunks.append(chunk)
        return b"".join(chunks)


download_manager = DownloadManager()

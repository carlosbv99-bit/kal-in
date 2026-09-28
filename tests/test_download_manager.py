"""
Tests de tool_integration/download_manager.py — Artifact Service, Fase
1 (descarga real de imágenes). Sin red real: `get_fn`/`resolve_fn`
inyectados (mismo patrón DI que el resto del proyecto,
ver agent_core/llm/ollama_client.py::OllamaClient(post_fn=...)).

El chequeo de allowlist de dominios YA NO vive acá (se quitó — ver el
docstring del módulo) — ese caso ahora se prueba en
tests/test_network_access_manager.py y
tests/test_import_resource_tool.py, contra la única fuente de verdad
real (kernel/permissions/network_access_manager.py).
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from tool_integration.download_manager import DownloadManager, DownloadValidationError
from tool_integration.malware_scan import MalwareScanError


def _real_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buf, format="PNG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200, chunk_size: int = 1024, headers: dict | None = None):
        self._content = content
        self.status_code = status_code
        self._chunk_size = chunk_size
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), self._chunk_size):
            yield self._content[i : i + self._chunk_size]


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings.downloads, "allow_http", False)
    monkeypatch.setattr(settings.downloads, "allowed_domains", ["unsplash.com"])
    monkeypatch.setattr(settings.downloads, "max_size_mb", 1)


def _manager(get_fn=None, resolve_fn=None):
    return DownloadManager(get_fn=get_fn, resolve_fn=resolve_fn or (lambda host: ["93.184.216.34"]))


def test_rejects_an_unsupported_resource_type():
    manager = _manager()
    with pytest.raises(DownloadValidationError, match="no soportado"):
        manager.download_and_validate("https://unsplash.com/x.pdf", expected_type="pdf")


def test_rejects_http_when_allow_http_is_disabled():
    manager = _manager()
    with pytest.raises(DownloadValidationError, match="allow_http"):
        manager.download_and_validate("http://unsplash.com/x.jpg", expected_type="image")


def test_rejects_when_hostname_resolves_to_an_unsafe_ip():
    manager = _manager(resolve_fn=lambda host: ["127.0.0.1"])
    with pytest.raises(DownloadValidationError, match="rebinding"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_rejects_content_exceeding_the_max_size(monkeypatch):
    too_big = b"x" * (2 * 1024 * 1024)  # 2MB > el límite de 1MB fijado en el fixture
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(too_big))
    with pytest.raises(DownloadValidationError, match="tamaño máximo"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_rejects_when_malware_scan_flags_the_content(monkeypatch):
    import tool_integration.download_manager as module

    monkeypatch.setattr(
        module, "scan_bytes", lambda data, suffix="": (_ for _ in ()).throw(MalwareScanError("detectado"))
    )
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(_real_png_bytes()))
    with pytest.raises(DownloadValidationError, match="Escaneo de seguridad"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_rejects_content_that_is_not_a_real_image(monkeypatch):
    import tool_integration.download_manager as module

    monkeypatch.setattr(module, "scan_bytes", lambda data, suffix="": None)  # ClamAV no es lo que se prueba acá
    garbage = b"esto no es una imagen de verdad, son bytes cualquiera"
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(garbage))
    with pytest.raises(DownloadValidationError, match="no es una imagen válida"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_succeeds_with_real_image_bytes_and_returns_correct_metadata(monkeypatch):
    import hashlib

    import tool_integration.download_manager as module

    monkeypatch.setattr(module, "scan_bytes", lambda data, suffix="": None)
    png_bytes = _real_png_bytes()
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(png_bytes))

    result = manager.download_and_validate("https://unsplash.com/x.png", expected_type="image")

    assert result.content == png_bytes
    assert result.sha256 == hashlib.sha256(png_bytes).hexdigest()
    assert result.mime == "image/png"
    assert result.size_bytes == len(png_bytes)


def test_raises_a_clear_error_when_the_http_request_itself_fails():
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(b"", status_code=404))
    with pytest.raises(DownloadValidationError, match="No se pudo descargar"):
        manager.download_and_validate("https://unsplash.com/no-existe.jpg", expected_type="image")


# --- M-4 (auditoría externa Likay-OS, 2026-09-26): SSRF vía redirección ---


def test_redirect_to_an_unsafe_ip_is_rejected_before_following_it(monkeypatch):
    """
    Antes: allow_redirects=True (default de requests) seguía un 302
    hacia CUALQUIER destino sin volver a validar su IP — solo el
    hostname ORIGINAL pasaba por is_unsafe_ip(). Un servidor bajo
    control del atacante en un dominio permitido podía redirigir hacia
    localhost/169.254.169.254/una IP interna.
    """

    def fake_resolve(host):
        if host == "interno.local":
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(b"", status_code=302, headers={"Location": "https://interno.local/secreto"})
        pytest.fail("nunca debería seguir la redirección hacia un destino inseguro")

    manager = DownloadManager(get_fn=fake_get, resolve_fn=fake_resolve)
    with pytest.raises(DownloadValidationError, match="rebinding"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_redirect_to_a_safe_destination_is_followed_and_succeeds(monkeypatch):
    import tool_integration.download_manager as module

    monkeypatch.setattr(module, "scan_bytes", lambda data, suffix="": None)
    png_bytes = _real_png_bytes()
    responses = [
        FakeResponse(b"", status_code=302, headers={"Location": "https://unsplash.com/final.png"}),
        FakeResponse(png_bytes),
    ]

    def fake_get(url, **kwargs):
        return responses.pop(0)

    manager = _manager(get_fn=fake_get)
    result = manager.download_and_validate("https://unsplash.com/x.png", expected_type="image")

    assert result.content == png_bytes


def test_too_many_redirects_is_rejected():
    def fake_get(url, **kwargs):
        return FakeResponse(b"", status_code=302, headers={"Location": "https://unsplash.com/otra"})

    manager = _manager(get_fn=fake_get)
    with pytest.raises(DownloadValidationError, match="demasiadas redirecciones"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_redirect_without_location_header_is_rejected():
    def fake_get(url, **kwargs):
        return FakeResponse(b"", status_code=302, headers={})

    manager = _manager(get_fn=fake_get)
    with pytest.raises(DownloadValidationError, match="sin header Location"):
        manager.download_and_validate("https://unsplash.com/x.jpg", expected_type="image")


def test_succeeds_with_real_clamav_scan_not_mocked():
    """
    A diferencia de los tests de arriba (scan_bytes mockeado, para
    aislar qué se está probando), este ejercita el escaneo REAL —
    mismo criterio que tests/test_malware_scan.py: se salta con un
    mensaje claro si ClamAV no está instalado en este entorno, en vez
    de mockearlo también acá.
    """
    from tool_integration.malware_scan import is_clamav_available

    if not is_clamav_available():
        pytest.skip("ClamAV no está instalado en este entorno")

    png_bytes = _real_png_bytes()
    manager = _manager(get_fn=lambda *a, **kw: FakeResponse(png_bytes))

    result = manager.download_and_validate("https://unsplash.com/x.png", expected_type="image")

    assert result.mime == "image/png"

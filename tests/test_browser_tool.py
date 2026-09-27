"""
Tests de tool_integration/adapters/browser.py::BrowserTool.

Usa un FakeBrowserDriver inyectado (mismo patrón que FakeSandboxExecutor
en test_tool_registry.py) para no requerir Playwright ni Chromium reales
instalados — lo que se prueba es la lógica de BrowserTool (allowlist de
dominios, despacho por acción, manejo de errores, auditoría), no
Playwright en sí.
"""
from __future__ import annotations

import pytest

from audit.audit_log import audit_log
from tool_integration.adapters.browser import BrowserTool
from utils.config import settings

_DEFAULT_FAKE_PUBLIC_IP = "93.184.216.34"  # IP pública real (example.com) — nunca privada/reservada


class FakeBrowserDriver:
    def __init__(
        self, links=None, images=None, text="contenido de la página", raise_on=None, final_url=None,
        remote_ip=_DEFAULT_FAKE_PUBLIC_IP,
    ):
        self.links = links if links is not None else ["https://example.com/a", "https://example.com/b"]
        self.images = images if images is not None else ["https://example.com/foto1.jpg", "https://example.com/foto2.jpg"]
        self.text = text
        self.raise_on = raise_on  # nombre de método que debe lanzar excepción
        # None = sin redirect (la URL final es la misma que se pidió). Fijar
        # esto simula que Playwright siguió un redirect hacia otro destino
        # (ver "BUG REAL ENCONTRADO EN HARDENING" en browser.py).
        self.final_url = final_url
        # IP a la que "Chromium se conectó" — pública por defecto. Fijar a
        # una IP privada/loopback/None simula DNS rebinding (ver "HALLAZGO
        # DE SEGURIDAD (Fase E6)" en browser.py).
        self.remote_ip = remote_ip
        self.calls: list[tuple] = []

    def _final(self, url):
        return self.final_url or url

    def extract_text(self, url, selector=None):
        self.calls.append(("extract_text", url, selector))
        if self.raise_on == "extract_text":
            raise RuntimeError("fallo simulado de red")
        return self.text, self._final(url), self.remote_ip

    def extract_links(self, url):
        self.calls.append(("extract_links", url))
        if self.raise_on == "extract_links":
            raise RuntimeError("fallo simulado de red")
        return self.links, self._final(url), self.remote_ip

    def extract_images(self, url):
        self.calls.append(("extract_images", url))
        if self.raise_on == "extract_images":
            raise RuntimeError("fallo simulado de red")
        return self.images, self._final(url), self.remote_ip

    def screenshot(self, url, path):
        self.calls.append(("screenshot", url, path))
        if self.raise_on == "screenshot":
            raise RuntimeError("fallo simulado de red")
        path.write_bytes(b"fake-png-bytes")
        return self._final(url), self.remote_ip


@pytest.fixture
def allow_example_domain(monkeypatch):
    monkeypatch.setattr(settings.browser, "allowed_domains", ["example.com"])


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    return BrowserTool(driver=FakeBrowserDriver())


# --- Esquema de la URL (solo http/https, nunca file:// u otros) ---


def test_file_scheme_is_rejected_regardless_of_allowlist(tool, allow_example_domain):
    artifact = tool.execute(url="file:///data/artifacts/images/tigre.png", action="screenshot")

    assert artifact.metadata["status"] == "error"
    assert "no es una URL http/https real" in artifact.metadata["stderr"]


def test_file_scheme_never_touches_the_driver(tool, allow_example_domain):
    tool.execute(url="file:///etc/passwd")

    assert tool.driver.calls == []


def test_data_scheme_is_rejected(tool, monkeypatch):
    monkeypatch.setattr(settings.browser, "allowed_domains", [])  # ni hace falta: el esquema se rechaza antes

    artifact = tool.execute(url="data:text/plain;base64,aGVsbG8=")

    assert artifact.metadata["status"] == "error"
    assert "no es una URL http/https real" in artifact.metadata["stderr"]


# --- Allowlist de dominios (deny-by-default) ---


def test_empty_allowlist_denies_everything_by_default(tool, monkeypatch):
    """
    BUG REAL CORREGIDO: antes rechazaba con "status": "error" y ningún
    camino de recuperación. Ahora escala a aprobación humana real (ver
    kernel/permissions/network_access_manager.py) — mismo mecanismo que
    ya adoptó ImportResourceTool para descargas.
    """
    monkeypatch.setattr(settings.browser, "allowed_domains", [])

    artifact = tool.execute(url="https://example.com/")

    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.metadata["resource_kind"] == "network"
    assert artifact.metadata["request_id"]
    assert "no permitido" in artifact.metadata["stderr"]


def test_domain_not_in_allowlist_is_denied(tool, allow_example_domain):
    artifact = tool.execute(url="https://otrositio.com/")

    assert artifact.metadata["status"] == "requires_approval"
    assert "otrositio.com" in artifact.metadata["stderr"]


def test_domain_not_in_allowlist_creates_a_real_pending_network_request(tool, allow_example_domain):
    from kernel.permissions.network_access_manager import network_access_manager

    artifact = tool.execute(url="https://otrositio.com/")

    pending = network_access_manager.list_pending()
    assert any(p.id == artifact.metadata["request_id"] and p.resource_key == "otrositio.com" for p in pending)


def test_rejection_message_names_the_currently_allowed_domains(tool, monkeypatch):
    """
    BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): pedido de "www.google.com" no
    permitido devolvía un mensaje que solo decía "agregalo a config.yaml" — sin
    nombrar los dominios YA permitidos, el modelo le dijo al usuario "no tengo
    acceso a Internet en absoluto" en vez de reintentar con uno real (p.ej.
    unsplash.com). El mensaje ahora los nombra explícitamente.
    """
    monkeypatch.setattr(settings.browser, "allowed_domains", ["unsplash.com", "pexels.com"])

    artifact = tool.execute(url="https://www.google.com/")

    assert "unsplash.com" in artifact.metadata["stderr"]
    assert "pexels.com" in artifact.metadata["stderr"]
    assert "NO significa que no haya acceso a" in artifact.metadata["stderr"]


def test_domain_approved_via_access_manager_is_allowed_on_retry(tool, allow_example_domain, tmp_path, monkeypatch):
    """
    Tras aprobar (level="project"), un reintento del MISMO dominio ya
    no escala. Instancia propia con grants_path aislado — el singleton
    real (kernel.permissions.network_access_manager.network_access_manager)
    persiste a un archivo real compartido entre corridas, no apto para
    un test que necesita partir de cero cada vez.
    """
    import tool_integration.adapters.browser as browser_module
    from kernel.permissions.network_access_manager import NetworkAccessManager

    isolated_manager = NetworkAccessManager(grants_path=tmp_path / "network_grants.json")
    monkeypatch.setattr(browser_module, "network_access_manager", isolated_manager)

    first = tool.execute(url="https://recien-aprobado.com/")
    isolated_manager.approve(first.metadata["request_id"], level="project")

    second = tool.execute(url="https://recien-aprobado.com/")

    assert second.metadata.get("status") != "requires_approval"


def test_exact_domain_match_is_allowed(tool, allow_example_domain):
    artifact = tool.execute(url="https://example.com/pagina")

    assert artifact.metadata.get("status") != "error"


def test_subdomain_of_allowed_domain_is_allowed(tool, allow_example_domain):
    artifact = tool.execute(url="https://docs.example.com/pagina")

    assert artifact.metadata.get("status") != "error"


def test_denied_domain_never_touches_the_driver(tool, monkeypatch):
    monkeypatch.setattr(settings.browser, "allowed_domains", [])

    tool.execute(url="https://example.com/")

    assert tool.driver.calls == []


# --- Despacho por acción ---


def test_action_text_returns_summary_in_metadata(tool, allow_example_domain):
    artifact = tool.execute(url="https://example.com/", action="text")

    assert artifact.modality == "text"
    assert artifact.metadata["summary"] == "contenido de la página"


def test_action_text_passes_selector_through(tool, allow_example_domain):
    tool.execute(url="https://example.com/", action="text", selector="#contenido")

    assert tool.driver.calls[0] == ("extract_text", "https://example.com/", "#contenido")


def test_action_links_joins_links_in_summary(tool, allow_example_domain):
    artifact = tool.execute(url="https://example.com/", action="links")

    assert "https://example.com/a" in artifact.metadata["summary"]
    assert "https://example.com/b" in artifact.metadata["summary"]


def test_action_links_with_no_links_reports_it(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(links=[]))

    artifact = tool.execute(url="https://example.com/", action="links")

    assert artifact.metadata["summary"] == "(sin enlaces encontrados)"


def test_action_images_joins_image_urls_in_summary(tool, allow_example_domain):
    """
    BUG REAL: el modelo necesita URLs de imagen REALES (no inventadas)
    antes de llamar import_resource — esta acción existe para eso.
    """
    artifact = tool.execute(url="https://example.com/", action="images")

    assert "https://example.com/foto1.jpg" in artifact.metadata["summary"]
    assert "https://example.com/foto2.jpg" in artifact.metadata["summary"]


def test_action_images_with_no_images_reports_it(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(images=[]))

    artifact = tool.execute(url="https://example.com/", action="images")

    assert artifact.metadata["summary"] == "(sin imágenes encontradas)"


def test_action_screenshot_writes_file_and_returns_image_artifact(tool, allow_example_domain):
    artifact = tool.execute(url="https://example.com/", action="screenshot")

    assert artifact.modality == "image"
    assert artifact.uri.endswith(".png")
    from pathlib import Path
    assert Path(artifact.uri).exists()


# --- Manejo de errores del driver ---


def test_driver_exception_is_caught_and_reported_as_error(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(raise_on="extract_text"))

    artifact = tool.execute(url="https://example.com/")

    assert artifact.metadata["status"] == "error"
    assert "fallo simulado de red" in artifact.metadata["stderr"]


# --- Auditoría ---


def test_successful_navigation_is_audited(tool, allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    tool.execute(url="https://example.com/")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "browser_navigation"
    assert entries[0]["outcome"] == "success"


def test_blocked_domain_is_audited_as_failure(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "allowed_domains", [])
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    tool.execute(url="https://example.com/")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "browser_navigation"
    assert entries[0]["outcome"] == "failure"


def test_manifest_declares_browser_permission_and_network():
    from sdk.permissions import Permission

    assert Permission.BROWSER in BrowserTool.manifest.permissions
    assert Permission.NETWORK in BrowserTool.manifest.permissions


# --- Redirect a un dominio no permitido (bug real encontrado en hardening F5) ---


def test_redirect_to_disallowed_domain_is_rejected_for_text(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://evil.com/pagina"))

    artifact = tool.execute(url="https://example.com/redirect", action="text")

    assert artifact.metadata["status"] == "requires_approval"
    assert "redirect" in artifact.metadata["stderr"]
    assert "evil.com" in artifact.metadata["stderr"]
    # El contenido nunca debe filtrarse al agente, aunque el driver lo haya extraído.
    assert "contenido de la página" not in artifact.metadata["stderr"]


def test_redirect_to_disallowed_domain_is_rejected_for_links(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://evil.com/pagina"))

    artifact = tool.execute(url="https://example.com/redirect", action="links")

    assert artifact.metadata["status"] == "requires_approval"
    assert "example.com/a" not in artifact.metadata.get("summary", "")


def test_redirect_to_disallowed_domain_is_rejected_for_screenshot(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://evil.com/pagina"))

    artifact = tool.execute(url="https://example.com/redirect", action="screenshot")

    assert artifact.metadata["status"] == "requires_approval"
    assert artifact.modality != "image"


def test_redirect_to_disallowed_domain_deletes_partial_screenshot(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://evil.com/pagina"))

    tool.execute(url="https://example.com/redirect", action="screenshot")

    # El driver fake sí "escribió" el archivo (simulando que Playwright ya
    # había capturado la pantalla antes de que se detectara el redirect) —
    # confirma que no queda huérfano en disco.
    assert list(tmp_path.glob("*.png")) == []


def test_redirect_within_allowed_domains_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "allowed_domains", ["example.com", "docs.example.com"])
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://docs.example.com/destino"))

    artifact = tool.execute(url="https://example.com/redirect", action="text")

    # Redirect legítimo entre dos dominios YA aprobados: no debe rechazarse.
    assert artifact.metadata.get("status") != "error"
    assert artifact.metadata["summary"] == "contenido de la página"


def test_redirect_rejection_is_audited_as_failure(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    tool = BrowserTool(driver=FakeBrowserDriver(final_url="https://evil.com/pagina"))

    tool.execute(url="https://example.com/redirect", action="text")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "browser_navigation"
    assert entries[0]["outcome"] == "failure"


# --- hostname vs netloc (bug de corrección encontrado junto con el de arriba) ---


def test_url_with_userinfo_is_allowed_when_hostname_matches(tool, allow_example_domain):
    # Antes: se comparaba contra netloc completo ("x@example.com"), que
    # nunca calzaba con "example.com" — se negaba por accidente. El
    # destino de red real es example.com (x es solo un usuario de auth
    # básica), así que debe permitirse.
    artifact = tool.execute(url="https://x@example.com/pagina")

    assert artifact.metadata.get("status") != "error"


def test_url_with_non_standard_port_is_allowed_when_hostname_matches(tool, allow_example_domain):
    artifact = tool.execute(url="https://example.com:8443/pagina")

    assert artifact.metadata.get("status") != "error"


# --- DNS rebinding: dominio permitido que resuelve a una IP privada/reservada (Fase E6) ---


@pytest.mark.parametrize(
    "unsafe_ip",
    ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "0.0.0.0", "::1", None],
)
def test_domain_allowed_but_private_ip_is_rejected_for_text(allow_example_domain, tmp_path, monkeypatch, unsafe_ip):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(remote_ip=unsafe_ip))

    artifact = tool.execute(url="https://example.com/", action="text")

    assert artifact.metadata["status"] == "error"
    assert "IP" in artifact.metadata["stderr"]
    # El contenido nunca debe filtrarse al agente, aunque el driver lo haya extraído.
    assert "contenido de la página" not in artifact.metadata["stderr"]


def test_domain_allowed_but_private_ip_is_rejected_for_screenshot(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(remote_ip="127.0.0.1"))

    artifact = tool.execute(url="https://example.com/", action="screenshot")

    assert artifact.metadata["status"] == "error"
    assert artifact.modality != "image"
    assert list(tmp_path.glob("*.png")) == []  # no queda huérfana en disco


def test_domain_allowed_but_private_ip_is_rejected_for_links(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    tool = BrowserTool(driver=FakeBrowserDriver(remote_ip="127.0.0.1"))

    artifact = tool.execute(url="https://example.com/", action="links")

    assert artifact.metadata["status"] == "error"
    assert "example.com/a" not in artifact.metadata.get("summary", "")


def test_public_ip_is_allowed(tool, allow_example_domain):
    """El caso normal (default de FakeBrowserDriver) sigue funcionando."""
    artifact = tool.execute(url="https://example.com/")

    assert artifact.metadata.get("status") != "error"


def test_private_ip_rejection_is_audited_as_failure(allow_example_domain, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.browser, "artifact_dir", str(tmp_path))
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    tool = BrowserTool(driver=FakeBrowserDriver(remote_ip="127.0.0.1"))

    tool.execute(url="https://example.com/")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "browser_navigation"
    assert entries[0]["outcome"] == "failure"


# is_unsafe_ip()/is_domain_allowed() se extrajeron a
# kernel/permissions/network_safety.py (download_manager.py las necesita
# igual) — su cobertura vive en tests/test_network_safety.py, no acá.

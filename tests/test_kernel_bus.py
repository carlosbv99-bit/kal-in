"""
Tests de kernel/api/bus.py::KernelServiceBus — registro de servicios +
despacho por nombre de método. Con un servicio falso (sin tocar
ImageService/SDXL-Turbo real).
"""
from __future__ import annotations

import pytest

from kernel.api.bus import (
    ActionNotFoundError,
    ArtifactNotFoundError,
    KernelServiceBus,
    ServiceNotFoundError,
)


class FakeEchoService:
    ALLOWED_ACTIONS = frozenset({"echo", "boom", "with_artifact", "describe", "whoami"})

    def echo(self, text):
        return {"echoed": text}

    def boom(self):
        raise RuntimeError("boom interno")

    def with_artifact(self):
        return {"artifact": "artifact://fake/1", "path": "/host/real/path.png", "metadata": {}}

    def describe(self, path):
        return {"received_path": path}

    def whoami(self, skill_name):
        """Único método de este fake que declara `skill_name` — igual
        que DownloadService.fetch() (ver tool_integration/services.py),
        el único servicio real que hoy lo necesita."""
        return {"skill_name": skill_name}

    def not_an_action(self):
        """Público y callable, pero deliberadamente fuera de ALLOWED_ACTIONS."""
        return {"esto": "no debería ser invocable vía el bus"}


@pytest.fixture
def bus():
    b = KernelServiceBus()
    b.register("test", FakeEchoService())
    return b


def test_dispatch_calls_the_right_action(bus):
    result = bus.dispatch("test.echo", {"text": "hola"})
    assert result == {"echoed": "hola"}


def test_dispatch_unknown_service_raises(bus):
    with pytest.raises(ServiceNotFoundError):
        bus.dispatch("noexiste.echo", {})


def test_dispatch_unknown_action_raises(bus):
    with pytest.raises(ActionNotFoundError):
        bus.dispatch("test.no_existe", {})


def test_dispatch_rejects_a_public_method_not_in_allowed_actions(bus):
    """
    Hallazgo de la revisión de seguridad 2026-07-09: antes, dispatch()
    resolvía la acción con getattr genérico — CUALQUIER método público
    del servicio era invocable, no solo los pensados como "acciones"
    del bus. ALLOWED_ACTIONS es la lista explícita; un método público
    fuera de esa lista debe rechazarse igual que uno inexistente.
    """
    bus.dispatch("test.echo", {"text": "hola"})  # sanity: "echo" sí es acción
    with pytest.raises(ActionNotFoundError):
        # "not_an_action" no está en ALLOWED_ACTIONS aunque exista como
        # atributo público en la clase (agregado más abajo en este archivo).
        bus.dispatch("test.not_an_action", {})


def test_dispatch_lets_action_exceptions_propagate(bus):
    with pytest.raises(RuntimeError, match="boom interno"):
        bus.dispatch("test.boom", {})


def test_dispatch_strips_path_and_registers_it_for_resolution(bus):
    result = bus.dispatch("test.with_artifact", {})

    assert "path" not in result  # nunca cruza al otro lado del socket
    assert result["artifact"] == "artifact://fake/1"
    assert bus.resolve_artifact("artifact://fake/1") == "/host/real/path.png"


def test_resolve_artifact_returns_none_for_unknown_uri(bus):
    assert bus.resolve_artifact("artifact://no/existe") is None


def test_register_replaces_existing_service(bus):
    bus.register("test", FakeEchoService())  # segunda instancia, mismo nombre
    result = bus.dispatch("test.echo", {"text": "de nuevo"})
    assert result == {"echoed": "de nuevo"}


# --- Resolución de artefactos de ENTRADA (para stt.transcribe/image.inpaint,
# que necesitan un archivo ya existente, a diferencia de image.generate/
# audio.synthesize que solo producen) ---


def test_dispatch_resolves_known_artifact_uri_before_calling_action(bus):
    bus.dispatch("test.with_artifact", {})  # registra "artifact://fake/1" -> "/host/real/path.png"

    result = bus.dispatch("test.describe", {"path": "artifact://fake/1"})

    assert result == {"received_path": "/host/real/path.png"}


def test_dispatch_raises_for_unknown_artifact_uri(bus):
    with pytest.raises(ArtifactNotFoundError):
        bus.dispatch("test.describe", {"path": "artifact://no/existe"})


def test_dispatch_leaves_non_artifact_strings_untouched(bus):
    result = bus.dispatch("test.describe", {"path": "/ya/es/una/ruta/real.png"})
    assert result == {"received_path": "/ya/es/una/ruta/real.png"}


# --- B-7 (auditoría externa de kal, 2026-09-27, portado acá vía
# scripts/check_kernel_drift.py): artifact_paths scopeado por skill —
# antes era un único dict GLOBAL, sin scoping: una skill que obtuviera
# el URI de un artefacto de OTRA skill podía resolverlo igual. Acá SÍ
# es explotable de verdad (a diferencia de kal, kernel puro sin
# servicios reales registrados): kal-in registra
# ImageService/AudioService/STTService/DownloadService de verdad ---


def test_a_skill_cannot_resolve_an_artifact_registered_by_a_different_skill(bus):
    bus.dispatch("test.with_artifact", {}, skill_name="skill_a")  # registra artifact://fake/1

    with pytest.raises(ArtifactNotFoundError):
        bus.dispatch("test.describe", {"path": "artifact://fake/1"}, skill_name="skill_b")


def test_resolve_artifact_is_scoped_per_skill_name(bus):
    bus.dispatch("test.with_artifact", {}, skill_name="skill_a")

    assert bus.resolve_artifact("artifact://fake/1", skill_name="skill_a") == "/host/real/path.png"
    assert bus.resolve_artifact("artifact://fake/1", skill_name="skill_b") is None
    assert bus.resolve_artifact("artifact://fake/1") is None  # sin skill_name: scope "" propio, tampoco lo ve


def test_a_skill_can_still_resolve_its_own_artifact(bus):
    """El caso legítimo (SandboxedSkillTool._to_artifact) sigue funcionando:
    una skill resolviendo un artefacto que ELLA MISMA registró."""
    bus.dispatch("test.with_artifact", {}, skill_name="skill_a")

    result = bus.dispatch("test.describe", {"path": "artifact://fake/1"}, skill_name="skill_a")

    assert result == {"received_path": "/host/real/path.png"}


def test_artifact_registration_is_capped_per_skill(bus):
    """FIFO: al llegar al máximo, la entrada más vieja de ESA skill se
    descarta antes de agregar la nueva — sin límite, un proceso de
    larga vida acumula esto sin cota."""
    cap = KernelServiceBus._MAX_ARTIFACTS_PER_SKILL

    class _NumberedArtifactService:
        ALLOWED_ACTIONS = frozenset({"make"})

        def make(self, n):
            return {"artifact": f"artifact://numbered/{n}", "path": f"/host/{n}.png", "metadata": {}}

    bus.register("numbered", _NumberedArtifactService())
    for i in range(cap + 5):
        bus.dispatch("numbered.make", {"n": i}, skill_name="skill_a")

    # Las primeras 5 (las más viejas) ya no están; las últimas `cap` sí.
    for i in range(5):
        assert bus.resolve_artifact(f"artifact://numbered/{i}", skill_name="skill_a") is None
    for i in range(5, cap + 5):
        assert bus.resolve_artifact(f"artifact://numbered/{i}", skill_name="skill_a") == f"/host/{i}.png"


# --- Inyección de skill_name (agregado para DownloadService, ver
# tool_integration/services.py — el permiso de red es por-skill, el
# único servicio que hoy necesita saber quién lo llama) ---


def test_dispatch_injects_skill_name_into_actions_that_declare_it(bus):
    result = bus.dispatch("test.whoami", {}, skill_name="download_via_kernel")
    assert result == {"skill_name": "download_via_kernel"}


def test_dispatch_does_not_inject_skill_name_into_actions_that_do_not_declare_it(bus):
    """Regresión: image.generate/audio.synthesize/etc. no esperan
    skill_name — pasarlo igual rompería con un TypeError si se
    inyectara sin condición."""
    result = bus.dispatch("test.echo", {"text": "hola"}, skill_name="cualquier_skill")
    assert result == {"echoed": "hola"}


def test_dispatch_without_skill_name_still_works_for_actions_that_do_not_need_it(bus):
    result = bus.dispatch("test.echo", {"text": "hola"})
    assert result == {"echoed": "hola"}

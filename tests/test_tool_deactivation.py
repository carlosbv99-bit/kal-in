"""
Apagar una herramienta dinámica, y auditar la manipulación detectada.

Dos huecos de la misma revisión (2026-09-29), y el segundo es el más serio de la ronda:

1. **`_active_tools` solo se escribía.** Había registro, activación y rollback, pero
   ninguna forma de APAGAR una herramienta: la única manera de sacarla de circulación era
   reemplazarla por otra versión. Para una herramienta comprometida, "no se puede retirar"
   es un hueco de verdad. Las Skills sí tenían el par completo
   (`set_skill_enabled()` + `--disable`).

2. **`verify_tool_integrity()` no auditaba nada.** Devolvía True/False en silencio,
   mientras que `rollback_tool()` sí audita `tool_tamper_detected` cuando una versión no
   verifica. Era la ÚNICA detección de manipulación del sistema que no dejaba rastro, y
   encima es on-demand (`GET /tools/{name}/verify`): nadie la sondea periódicamente, así
   que una edición en disco de la versión activa no se veía salvo que alguien pensara en
   preguntar a mano.

Se prueba con una `DynamicSandboxedTool` real registrada a mano (no por
`propose_dynamic_tool()`, que correría una ejecución de prueba en sandbox): lo que se
prueba es la retirada y la auditoría, no cómo se llegó a activa.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.orchestrator import _ADMIN_TOKEN, app, orchestrator
from kernel.registry.registry import DynamicSandboxedTool
from sdk.skill import ToolManifest

client = TestClient(app, base_url="http://localhost")
_TOKEN = {"X-Kal-Admin-Token": _ADMIN_TOKEN}
_NOMBRE = "herramienta_dinamica_de_prueba"


@pytest.fixture
def dinamica_activa():
    """Una tool dinámica activa de verdad, con versión y firma."""
    tool = DynamicSandboxedTool(
        ToolManifest(name=_NOMBRE, description="de prueba", created_by="system"),
        "x = 1",
        sandbox=orchestrator.tools.sandbox,
        version=1,
        signature="firma-de-prueba",
    )
    orchestrator.tools._active_tools[_NOMBRE] = tool
    try:
        yield tool
    finally:
        orchestrator.tools._active_tools.pop(_NOMBRE, None)


# --- 1. retirar de circulación ----------------------------------------------


def test_desactivar_saca_la_herramienta_del_registro(dinamica_activa, monkeypatch):
    """
    Borrar la entrada es lo que la apaga de verdad: `get()` y `active_tools()`
    resuelven contra ese dict, y el catálogo de herramientas del LLM se arma por
    turno desde ahí.
    """
    from kernel.registry import registry as modulo

    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))
    assert orchestrator.tools.get(_NOMBRE) is dinamica_activa

    orchestrator.tools.deactivate_tool(_NOMBRE, deactivated_by="kalin", reason="quedó obsoleta")

    assert orchestrator.tools.get(_NOMBRE) is None
    assert _NOMBRE not in orchestrator.tools.active_tools()
    evento = next(e for e in eventos if e.event_type == "tool_deactivated")
    assert evento.context["tool_name"] == _NOMBRE
    assert evento.context["deactivated_by"] == "kalin"
    assert "quedó obsoleta" in evento.summary


def test_no_se_puede_desactivar_una_herramienta_estatica(dinamica_activa):
    """Las del sistema no se retiran por acá: el rechazo tiene que ser explícito."""
    estatica = type(
        "Tool", (), {"manifest": ToolManifest(name="run_code", description="de prueba", created_by="system")}
    )()
    orchestrator.tools._active_tools["run_code"] = estatica
    try:
        with pytest.raises(ValueError, match="estática"):
            orchestrator.tools.deactivate_tool("run_code")
    finally:
        orchestrator.tools._active_tools.pop("run_code", None)


def test_no_se_puede_desactivar_algo_que_no_esta_activo(dinamica_activa):
    with pytest.raises(ValueError, match="No hay herramienta activa"):
        orchestrator.tools.deactivate_tool("no-existe")


def test_el_endpoint_exige_token_y_desactiva(dinamica_activa):
    assert client.post(f"/tools/{_NOMBRE}/deactivate", json={}).status_code == 401
    assert orchestrator.tools.get(_NOMBRE) is not None, "sin token no se toca nada"

    respuesta = client.post(
        f"/tools/{_NOMBRE}/deactivate", headers=_TOKEN, json={"deactivated_by": "kalin"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"name": _NOMBRE, "status": "deactivated"}
    assert orchestrator.tools.get(_NOMBRE) is None


def test_el_endpoint_da_400_de_negocio_con_un_nombre_inexistente(dinamica_activa):
    respuesta = client.post("/tools/no-existe/deactivate", headers=_TOKEN, json={})

    assert respuesta.status_code == 400
    assert "No hay herramienta activa" in respuesta.json()["detail"]


# --- 2. auditar la manipulación detectada -----------------------------------


class _SignerQueRechaza:
    def verify(self, *args, **kwargs) -> bool:
        return False


class _SignerQueAcepta:
    def verify(self, *args, **kwargs) -> bool:
        return True


def test_detectar_manipulacion_queda_auditado(dinamica_activa, monkeypatch):
    """
    El hueco más serio de la ronda: la detección existía y no dejaba rastro. Mismo
    evento que usa `rollback_tool` cuando rechaza una versión que no verifica.
    """
    from kernel.registry import registry as modulo

    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))
    monkeypatch.setattr(orchestrator.tools, "signer", _SignerQueRechaza())
    # Doble del almacén: la versión existe para el registro (que es lo que se prueba
    # acá), pero no hace falta escribirla en disco.
    monkeypatch.setattr(
        orchestrator.tools.version_store,
        "read_version",
        lambda name, version: ("x = 1", {"signature": "firma-de-prueba", "manifest": {}}),
    )

    assert orchestrator.tools.verify_tool_integrity(_NOMBRE) is False

    evento = next(e for e in eventos if e.event_type == "tool_tamper_detected")
    assert evento.context["tool_name"] == _NOMBRE
    assert evento.context["version"] == 1
    assert evento.outcome == "failure"


def test_el_camino_feliz_no_ensucia_el_log(dinamica_activa, monkeypatch):
    """
    Cuando la firma verifica no hay nada que reportar: un log lleno de "todo bien"
    es la forma de que nadie lo mire.
    """
    from kernel.registry import registry as modulo

    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))
    monkeypatch.setattr(orchestrator.tools, "signer", _SignerQueAcepta())
    monkeypatch.setattr(
        orchestrator.tools.version_store,
        "read_version",
        lambda name, version: ("x = 1", {"signature": "firma-de-prueba", "manifest": {}}),
    )

    assert orchestrator.tools.verify_tool_integrity(_NOMBRE) is True
    assert eventos == []

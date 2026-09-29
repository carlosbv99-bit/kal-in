"""
Los caminos de DENEGACIÓN humana que faltaban (revisión, 2026-09-29).

Misma clase de hueco que el circuit breaker: el sistema escala algo a un humano, lo
cuenta en `/status` y lo audita, pero la interfaz era asimétrica — había un verbo para
decir "sí" y ninguno para decir "no". En los dos casos, "no hacer nada" logra el efecto
práctico (la herramienta nunca se activa, el cambio nunca se aplica), pero **la decisión
no queda registrada y la entrada se queda en la lista para siempre**.

De los tres lugares con escalación humana, permisos (filesystem/network) ya estaba
completo: tiene GET + approve + deny. Estos son los otros dos:

- herramientas dinámicas pendientes: había `approve_pending_tool()`, no `deny_pending_tool()`;
- propuestas de self-modification: la única transición desde `pending_human_approval` era
  `apply()`.

Los tests inyectan el estado pendiente en vez de correr el pipeline completo (ejecución
de prueba en sandbox para una herramienta; copiar el proyecto y correr pytest para una
propuesta) porque lo que se prueba acá es la TRANSICIÓN de la decisión humana, no cómo
se llegó a pendiente — y esa llegada ya tiene sus propios tests.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.orchestrator import _ADMIN_TOKEN, app, orchestrator
from agent_core.self_modification import SelfModProposal
from kernel.registry.registry import PendingTool
from sdk.skill import ToolManifest

client = TestClient(app, base_url="http://localhost")
_TOKEN = {"X-Kal-Admin-Token": _ADMIN_TOKEN}
_NOMBRE = "herramienta_pendiente_de_prueba"


@pytest.fixture
def herramienta_pendiente(monkeypatch):
    """Un pendiente real en el registro, con la ejecución de prueba salteada."""
    monkeypatch.setattr(orchestrator.tools, "_requires_human_approval", lambda manifest: True)
    manifest = ToolManifest(name=_NOMBRE, description="de prueba", created_by="system")
    orchestrator.tools._pending[_NOMBRE] = PendingTool(
        manifest=manifest, source_code="x = 1", status="pending_approval"
    )
    try:
        yield _NOMBRE
    finally:
        orchestrator.tools._pending.pop(_NOMBRE, None)


@pytest.fixture
def propuesta_pendiente():
    """Una propuesta en `pending_human_approval`, sin correr el pipeline de tests."""
    propuesta = SelfModProposal(
        id="prop-de-prueba",
        target_path="mymodule.py",
        proposed_source="x = 1",
        justification="prueba del rechazo humano",
        status="pending_human_approval",
    )
    orchestrator.self_modification._proposals[propuesta.id] = propuesta
    try:
        yield propuesta
    finally:
        orchestrator.self_modification._proposals.pop(propuesta.id, None)


# --- herramientas pendientes -------------------------------------------------


def test_el_rechazo_de_una_herramienta_exige_token(herramienta_pendiente):
    assert client.post(f"/tools/{herramienta_pendiente}/deny", json={}).status_code == 401
    # Y sin token la herramienta sigue pendiente: el gate no es decorativo.
    assert herramienta_pendiente in orchestrator.tools._pending


def test_una_herramienta_inexistente_da_400_de_negocio_no_401(herramienta_pendiente):
    respuesta = client.post("/tools/no-existe-esta-herramienta/deny", headers=_TOKEN, json={})

    assert respuesta.status_code == 400
    assert "No hay herramienta pendiente" in respuesta.json()["detail"]


def test_rechazar_una_herramienta_la_saca_de_la_lista_y_del_contador(herramienta_pendiente, monkeypatch):
    """
    El punto del hallazgo: ignorarla lograba el efecto práctico, pero quedaba en
    `list_pending()` y contando en `/status` para siempre. Ahora la decisión sale de
    en medio Y queda auditada.
    """
    from kernel.registry import registry as modulo_registry

    eventos = []
    monkeypatch.setattr(modulo_registry.audit_log, "record", lambda evento: eventos.append(evento))
    assert any(p["name"] == herramienta_pendiente for p in orchestrator.tools.list_pending())
    assert client.get("/status").json()["pending_tool_approvals"] == 1

    respuesta = client.post(
        f"/tools/{herramienta_pendiente}/deny", headers=_TOKEN, json={"denied_by": "kalin"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"name": herramienta_pendiente, "status": "denied"}
    assert herramienta_pendiente not in orchestrator.tools._pending
    assert not any(p["name"] == herramienta_pendiente for p in orchestrator.tools.list_pending())
    assert client.get("/status").json()["pending_tool_approvals"] == 0
    evento = next(e for e in eventos if e.event_type == "tool_denied")
    assert evento.context["tool_name"] == herramienta_pendiente
    assert "kalin" in evento.summary


def test_el_registro_rechaza_lo_que_no_esta_pendiente():
    """Mismos guardas que la aprobación: no se "rechaza" algo que ya no espera decisión."""
    with pytest.raises(ValueError, match="No hay herramienta pendiente"):
        orchestrator.tools.deny_pending_tool("no-existe")


# --- propuestas de self-modification ----------------------------------------


def test_el_rechazo_de_una_propuesta_exige_token(propuesta_pendiente):
    respuesta = client.post("/self-modification/reject", json={"proposal_id": propuesta_pendiente.id})

    assert respuesta.status_code == 401
    assert propuesta_pendiente.status == "pending_human_approval"


def test_rechazar_una_propuesta_la_deja_rechazada_y_auditada(propuesta_pendiente, monkeypatch):
    from agent_core import self_modification as modulo

    eventos = []
    monkeypatch.setattr(modulo.audit_log, "record", lambda evento: eventos.append(evento))
    assert client.get("/status").json()["pending_self_modification_approvals"] == 1

    respuesta = client.post(
        "/self-modification/reject",
        headers=_TOKEN,
        json={"proposal_id": propuesta_pendiente.id, "rejected_by": "kalin", "reason": "toca el core"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["status"] == "rejected_by_human"
    assert propuesta_pendiente.status == "rejected_by_human"
    assert "toca el core" in propuesta_pendiente.detail
    # Ya no espera decisión humana: sale del contador de /status.
    assert client.get("/status").json()["pending_self_modification_approvals"] == 0
    evento = next(e for e in eventos if e.outcome == "failure" and "toca el core" in e.summary)
    assert evento.context["proposal_id"] == propuesta_pendiente.id


def test_no_se_puede_rechazar_una_propuesta_que_no_esta_pendiente(propuesta_pendiente):
    """
    Un guarda que importa: si se pudiera rechazar después de aplicar, el estado
    mentiría sobre lo que pasó de verdad con el archivo.
    """
    propuesta_pendiente.status = "applied"

    respuesta = client.post(
        "/self-modification/reject", headers=_TOKEN, json={"proposal_id": propuesta_pendiente.id}
    )

    assert respuesta.status_code == 400
    assert "no está pendiente" in respuesta.json()["detail"]
    assert propuesta_pendiente.status == "applied", "el rechazo no puede pisar un estado real"

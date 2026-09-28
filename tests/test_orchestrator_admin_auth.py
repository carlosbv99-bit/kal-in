"""
Verifica el gate de token administrativo (ver utils/admin_token.py,
agent_core/orchestrator.py) sobre los endpoints que hacen de facto de
"aprobación humana": self-modification (propose/apply), aprobación y
rollback de herramientas, y autorreparación.

HALLAZGO REAL que motiva estos tests (revisión de seguridad,
2026-07-09): antes de esto, `approved_by` era un string que el propio
cliente HTTP elegía sin verificar identidad alguna — cualquiera que
alcanzara el puerto del agente podía aprobar/aplicar cambios. Estos
tests no vuelven a probar la lógica interna de self_modification/tools
(ya cubierta en sus propios archivos de test) — solo que el gate
bloquea sin token válido y deja pasar con uno correcto.

No se ejecuta el pipeline completo de self-modification (copiar el
proyecto + correr pytest en subproceso) a propósito: usar un
target_path/proposal_id/nombre de herramienta inexistente alcanza para
confirmar que la request llegó a la lógica real (un error 400/404 de
negocio, nunca 401) sin pagar el costo de una corrida real.

`/integrations/vscode/install` no tiene un identificador inexistente
que la haga fallar barato (no depende de ningún id) — en vez de
correrla de verdad (instalaría la extensión posta), el módulo
`agent_core.vscode_integration.install_extension` se mockea acá mismo
para el test de "token correcto llega a la lógica real".

`/settings/llm` tampoco depende de un id — sin mockear
`update_llm_settings`, escribiría de verdad en el config.yaml/.env
reales del proyecto solo para probar el gate, así que se mockea igual
que install_extension. `/settings/llm/ollama/pull` igual: sin mockear
`pull_ollama_model`, intentaría descargar un modelo real de varios GB.
`/settings/llm/activate-profile` NO necesita mockearse — activar un
perfil "groq" inexistente ya falla barato (400 de negocio, nunca
escribe nada), mismo criterio que target_path/proposal_id inexistente
en self-modification. `/filesystem-access/{id}/approve` y `/deny`
tampoco necesitan mockearse, mismo motivo: un id inexistente falla
barato.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent_core.orchestrator import _ADMIN_TOKEN, app
from agent_core.routers import llm_settings, vscode_integration

client = TestClient(app, base_url="http://localhost")

_GATED_REQUESTS = [
    ("POST", "/tools/no-existe-esta-herramienta/approve", {"approved_by": "alguien"}),
    ("POST", "/tools/no-existe-esta-herramienta/rollback", {"to_version": 1, "approved_by": "alguien"}),
    (
        "POST",
        "/self-modification/propose",
        {"target_path": "no_existe.py", "proposed_source": "x = 1", "justification": "test"},
    ),
    ("POST", "/self-modification/apply", {"proposal_id": "no-existe", "approved_by": "alguien"}),
    ("POST", "/diagnostics/invariante_inexistente/self-repair", {}),
    ("POST", "/integrations/vscode/install", {}),
    ("POST", "/settings/llm", {"provider": "ollama"}),
    ("POST", "/settings/llm/ollama/pull", {"model": "qwen2.5-coder:14b"}),
    ("POST", "/settings/llm/activate-profile", {"name": "groq"}),
    ("POST", "/filesystem-access/no-existe/approve", {"level": "once"}),
    ("POST", "/filesystem-access/no-existe/deny", {}),
    ("POST", "/network-access/no-existe/approve", {"level": "once"}),
    ("POST", "/network-access/no-existe/deny", {}),
    ("POST", "/skill-proposals/no-existe/approve", {"approved_by": "alguien"}),
    ("POST", "/skill-proposals/no-existe/reject", {}),
]


def test_gated_endpoints_reject_missing_token():
    for method, path, body in _GATED_REQUESTS:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} debería rechazar sin token"


def test_gated_endpoints_reject_wrong_token():
    for method, path, body in _GATED_REQUESTS:
        response = client.request(method, path, json=body, headers={"X-Kal-Admin-Token": "token-incorrecto"})
        assert response.status_code == 401, f"{method} {path} debería rechazar con token incorrecto"


def test_gated_endpoints_accept_correct_token_and_reach_real_logic(monkeypatch):
    # Sin esto, /integrations/vscode/install correría de verdad (compilaría
    # y trataría de instalar la extensión) solo para confirmar el gate.
    monkeypatch.setattr(vscode_integration, "install_extension", lambda: "ok (mockeado)")
    # Sin esto, /settings/llm escribiría de verdad en config.yaml/.env
    # reales del proyecto solo para confirmar el gate.
    monkeypatch.setattr(llm_settings, "update_llm_settings", lambda **kwargs: None)
    monkeypatch.setattr(llm_settings, "pull_ollama_model", lambda model: None)

    headers = {"X-Kal-Admin-Token": _ADMIN_TOKEN}
    for method, path, body in _GATED_REQUESTS:
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code != 401, f"{method} {path} no debería rechazar con el token correcto"


def test_ungated_endpoint_still_works_without_any_token():
    """/health nunca debería exigir el token — no es una acción sensible."""
    response = client.get("/health")
    assert response.status_code == 200


def test_android_build_report_outcome_never_requires_a_token():
    """
    Mismo criterio que filesystem-access/report-outcome: no hay ninguna
    decisión de permiso acá, solo un aviso auditado de la extensión
    sobre lo que pasó DE VERDAD al compilar/instalar del lado real del
    dispositivo — nunca debería exigir el token admin.
    """
    response = client.post(
        "/android-build/9d1c1b1a-2222-4c3d-8b1a-000000000001/report-outcome",
        json={"outcome": "installed", "detail": "app-debug.apk instalado"},
    )
    assert response.status_code == 200


def test_filesystem_access_report_outcome_never_requires_a_token():
    """
    Deliberadamente SIN token: el Kernel ya auto-permitió esta acción
    por política (auto_allow) antes de que la extensión escribiera algo
    de verdad — este endpoint solo deja constancia auditada de qué pasó,
    nunca decide ni ejecuta nada.
    """
    response = client.post(
        "/filesystem-access/9d1c1b1a-2222-4c3d-8b1a-000000000002/report-outcome",
        json={"outcome": "written", "files_written": ["index.html"]},
    )
    assert response.status_code == 200


# --- GET /admin-token: auto-provisión SOLO para loopback ---
#
# FRICCIÓN REAL ENCONTRADA EN USO: pedirle a un usuario no-programador
# que copie el token de una terminal era impracticable. Si el pedido
# viene de la MISMA máquina (loopback), se lo entrega solo — quien ya
# está en esa máquina podría leer data/keys/admin_token del disco
# igual, así que esto no le da ninguna capacidad nueva a un atacante
# remoto. TestClient por default simula un cliente "testclient", no
# loopback — hay que pasar `client=("127.0.0.1", puerto)` a propósito
# para simular el caso real que se quiere permitir.


def test_admin_token_endpoint_rejects_non_loopback_by_default():
    # TestClient sin `client=` simula un peer que NO es loopback.
    response = client.get("/admin-token")
    assert response.status_code == 403


def test_admin_token_endpoint_serves_the_real_token_from_loopback_ipv4():
    loopback_client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 54321))
    response = loopback_client.get("/admin-token")
    assert response.status_code == 200
    assert response.json() == {"token": _ADMIN_TOKEN}


def test_admin_token_endpoint_serves_the_real_token_from_loopback_ipv6():
    loopback_client = TestClient(app, base_url="http://localhost", client=("::1", 54321))
    response = loopback_client.get("/admin-token")
    assert response.status_code == 200
    assert response.json() == {"token": _ADMIN_TOKEN}


def test_admin_token_endpoint_rejects_a_real_lan_address():
    """El caso real que el token protege: alguien en la LAN, no en la propia máquina."""
    lan_client = TestClient(app, base_url="http://localhost", client=("192.168.1.50", 54321))
    response = lan_client.get("/admin-token")
    assert response.status_code == 403


def test_non_ascii_admin_token_header_returns_401_not_500():
    """
    M-6 (auditoría externa Likay-OS, 2026-09-26): secrets.compare_digest()
    sobre dos `str` exige ASCII puro — un header con un carácter no-ASCII
    (aceptado igual por Starlette, que decodifica headers como latin-1)
    lanzaba TypeError sin capturar, un 500 crudo en vez del 401 esperado.
    """
    # httpx (el cliente que usa TestClient) valida ASCII del lado
    # cliente si se le pasa un str con un carácter no-ASCII — hay que
    # pasar el header ya codificado en bytes (latin-1, lo mismo que
    # Starlette usaría al decodificar un header HTTP real) para
    # ejercitar de verdad el mismo valor que llegaría a compare_digest().
    response = client.post(
        "/tools/no-existe-esta-herramienta/approve",
        json={"approved_by": "alguien"},
        headers=[(b"X-Kal-Admin-Token", "tokén-con-acento-ñ".encode("latin-1"))],
    )
    assert response.status_code == 401

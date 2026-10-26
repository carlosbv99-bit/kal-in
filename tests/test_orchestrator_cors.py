"""
El invariante de CORS de la API: hoy NO hay `CORSMiddleware`, y eso protege.

Por qué importa y por qué es frágil: el servidor escucha en loopback, así que el NAT del
router no es la única puerta. El **navegador** está dentro de la red y cualquier página que
abrimos ejecuta código en nuestra máquina. Con DNS rebinding, una página puede hacer que su
propio dominio resuelva a 127.0.0.1 y hablarle a la API como si fuera el mismo sitio. Lo que
hoy lo frena no es una intención documentada sino una propiedad del borde: la API solo
acepta JSON y los endpoints admin piden el header `X-Kal-Admin-Token`, así que el navegador
necesita un **preflight**, y sin cabeceras CORS permisivas ese preflight falla → la petición
nunca sale. El día que alguien agregue `allow_origins=["*"]` para que el frontend
"funcione", abre exactamente esa puerta y nada más se pone rojo.

Estos tests lo fijan **por comportamiento** (¿la respuesta trae la cabecera o no?), no
mirando si hay un middleware instalado: así también atrapan una cabecera puesta a mano por
otro camino. Si algún día se agrega CORS legítimo para un origen concreto, estos tests
siguen pasando; si se agrega un comodín, fallan con el motivo escrito.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.orchestrator import app

_ORIGEN_AJENO = "https://evil.example"


@pytest.fixture
def client():
    return TestClient(app, base_url="http://localhost")


def _cabeceras_cors(respuesta) -> set[str]:
    return {k.lower() for k in respuesta.headers if k.lower().startswith("access-control-")}


def test_una_peticion_simple_desde_un_origen_ajeno_no_recibe_permiso(client):
    """
    Sin `Access-Control-Allow-Origin`, el navegador no le entrega la respuesta a la página:
    no puede leer `/status`, ni el estado del ledger, ni nada.
    """
    respuesta = client.get("/status", headers={"Origin": _ORIGEN_AJENO})

    assert not _cabeceras_cors(respuesta), (
        f"la API emitió cabeceras CORS ante un origen ajeno: {_cabeceras_cors(respuesta)}. "
        "Un comodín acá habilita DNS rebinding contra el loopback (ver docstring)."
    )


def test_el_preflight_de_un_origen_ajeno_no_se_aprueba(client):
    """
    El caso que de verdad usa el navegador antes de un POST JSON con header propio: si el
    preflight no devuelve `Access-Control-Allow-Origin`, la petición real no se envía.
    """
    respuesta = client.options(
        "/chat",
        headers={
            "Origin": _ORIGEN_AJENO,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-kal-admin-token",
        },
    )

    assert "access-control-allow-origin" not in {k.lower() for k in respuesta.headers}

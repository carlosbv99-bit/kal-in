"""
Orchestrator: punto de coordinación central del agente.

Responsabilidades:
  - Construir el singleton Orchestrator (memoria, tareas, herramientas,
    sesiones, self-modification, self-diagnosis) y el cliente LLM real.
  - Armar la app de FastAPI: token administrativo, montar los routers
    por dominio (agent_core/routers/*.py) y servir el frontend estático.
  - Exponer el loop de razonamiento (agent_core/llm/agent_loop.py) vía
    /chat (agent_core/routers/chat.py) — esto es lo que conecta a kal
    con Ollama y lo hace utilizable desde el frontend, no solo desde
    llamadas API de bajo nivel.

Este archivo YA NO declara los endpoints en sí (2026-07-20: eran 44,
todos acá, con imports de prácticamente todos los subsistemas del
proyecto — un cuello de botella de mantenibilidad real). Cada dominio
(chat, tareas, herramientas, memoria, self-modification, permisos,
diagnóstico, integraciones de IDE, auditoría, estado general) vive en
su propio APIRouter bajo agent_core/routers/, que importa desde acá lo
que necesita compartir (el singleton `orchestrator`, `require_admin_token`,
`_artifact_url`, `_reinject_llm_client`) — nunca al revés.
"""
from __future__ import annotations

import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_core.context_service import ContextService
from agent_core.conversation_engine import ConversationEngine
from agent_core.llm.agent_loop import AgentLoop
from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.openai_compatible_client import OpenAICompatibleClient
from agent_core.llm.planner import PlanningAgentLoop
from agent_core.llm.provider import LLMProvider
from agent_core.llm_settings import read_llm_env_var
from agent_core.runtime.llm_runtimes import OllamaRuntime, OpenAICompatibleRuntime
from agent_core.runtime.managed_provider import RuntimeManagedLLMProvider
from agent_core.runtime.manager import runtime_manager
from agent_core.memory.manager import MemoryManager
from agent_core.default_tools import register_default_static_tools
from agent_core.self_diagnosis import SelfDiagnosisAgent
from agent_core.self_modification import self_modification_manager
from agent_core.sessions import session_manager
from kernel.broker.resource_broker import resource_broker
from kernel.registry.registry import tool_registry
from task_execution.executor import TaskExecutor
from utils.admin_token import get_or_create_admin_token
from utils.config import settings
from utils.logger import get_logger

# Reemplaza el disparo implícito que antes ocurría al importar
# kernel.registry.registry (ver kernel/registry/registry.py) — decidir
# qué herramientas concretas existen por defecto es responsabilidad del
# agente, no del kernel. tests/conftest.py ya importa
# agent_core.orchestrator a nivel de módulo para TODOS los tests del
# repo, así que esta llamada sigue ocurriendo antes de cualquier test.
register_default_static_tools()

logger = get_logger(__name__)


# Nombre fijo bajo el que build_llm_client() registra el runtime elegido
# en runtime_manager (agent_core/runtime/manager.py) — un solo proveedor
# de LLM activo a la vez, igual que siempre (ver LLMConfig.provider).
_ACTIVE_RUNTIME_NAME = "active"


def build_llm_client() -> LLMProvider:
    """
    Fábrica del LLMProvider real según settings.llm.provider — kal se
    distribuye a usuarios con hardware muy distinto (ver
    docs/HISTORY.md), así que el modelo de lenguaje del agente no puede
    quedar hardcodeado a Ollama local. "ollama" (default) no cambia nada del
    comportamiento de siempre. "openai_compatible" sirve tanto para un
    proveedor real en la nube (Qwen/DashScope, Grok/xAI, OpenAI,
    OpenRouter...) como para el propio endpoint OpenAI-compatible de
    Ollama (ya validado en F2, ver agent_core/llm/openai_compatible_client.py).

    Fail-closed: sin LLM_API_KEY configurada, kal ni arranca — mismo
    criterio que IMAGE_GEN_API_KEY/AUDIO_GEN_API_KEY en los adaptadores
    multimodales (tool_integration/adapters/image_gen.py), nunca
    intentar sin autenticación y fallar tarde con un error confuso.

    Runtime Manager (2026-07-25, ver agent_core/runtime/): en vez de
    devolver el cliente concreto directo, lo registra como Runtime bajo
    `_ACTIVE_RUNTIME_NAME` y devuelve un `RuntimeManagedLLMProvider` que
    lo envuelve — así CUALQUIER llamada a `.chat()` pasa por el
    semáforo de `max_parallel` del runtime activo (settings.runtimes),
    evitando que varias ejecuciones concurrentes compitan por
    cargar/descargar modelos grandes en la misma máquina (problema real
    encontrado en uso, ver docs/HISTORY.md "Runtime Manager").
    `AgentLoop`/`Planner`/`SelfDiagnosisAgent` no se enteran de este
    mecanismo — solo siguen viendo un `LLMProvider` normal.
    """
    if settings.llm.provider == "openai_compatible":
        api_key = read_llm_env_var("LLM_API_KEY")
        if not api_key:
            raise RuntimeError(
                "LLM_API_KEY no configurada — completá .env (ver .env.example) "
                "para usar llm.provider: openai_compatible."
            )
        client = OpenAICompatibleClient(base_url=settings.llm.base_url, api_key=api_key)
        runtime = OpenAICompatibleRuntime(client, max_parallel=settings.runtimes.openai_compatible.max_parallel)
    else:
        client = OllamaClient()
        # BUG REAL ENCONTRADO EN USO (2026-07-27): un pedido de imagen
        # congeló la máquina del usuario por completo — SDXL-Turbo
        # (13GB fp32) intentó cargar con el modelo de chat de Ollama
        # todavía residente en RAM, superando la RAM física total
        # (14GB). resource_broker YA liberaba imagen/audio/STT antes de
        # llamar a Ollama (ver OllamaClient.chat()), pero nunca al
        # revés — nada liberaba el modelo de Ollama antes de que un
        # pipeline local pesado intentara cargar. Registrarlo acá cierra
        # ese hueco: tool_integration/services.py ahora llama
        # evict_idle_and_pressured() (que incluye esto) ANTES de cargar.
        # Diagnóstico de lentitud (2026-08-23): timeout PROPIO, más largo
        # que el general — el modelo de chat es chico y de uso muy
        # frecuente, a diferencia de los pipelines de imagen/audio/STT
        # (ver ResourceBrokerConfig.ollama_idle_timeout_seconds). El
        # chequeo de RAM baja de arriba sigue aplicando sin excepción.
        resource_broker.register(
            "ollama.chat_model",
            is_loaded=client.is_model_loaded,
            unload=client.unload_model,
            idle_timeout_seconds=settings.resource_broker.ollama_idle_timeout_seconds,
        )
        runtime = OllamaRuntime(client, max_parallel=settings.runtimes.ollama.max_parallel)
    runtime_manager.register(_ACTIVE_RUNTIME_NAME, runtime)
    return RuntimeManagedLLMProvider(runtime_manager, _ACTIVE_RUNTIME_NAME)


class Orchestrator:
    def __init__(self):
        self.memory = MemoryManager()
        self.tasks = TaskExecutor()
        self.tools = tool_registry
        self.self_modification = self_modification_manager
        self.sessions = session_manager
        self.context_service = ContextService()
        self.conversation_engine = ConversationEngine()
        self.llm = build_llm_client()
        self.agent = AgentLoop(llm_client=self.llm, task_executor=self.tasks, memory=self.memory)
        self.planning_agent = PlanningAgentLoop(self.agent)
        self.self_diagnosis = SelfDiagnosisAgent(llm_client=self.llm)

    def run_consolidation_cycle(self) -> dict:
        """Job periódico: corto->mediano, luego evalúa promoción mediano->largo."""
        consolidated = self.memory.consolidate_short_to_mid()
        promoted = self.memory.promote_mid_to_long()
        return {"consolidated": consolidated, "promoted": promoted}


orchestrator = Orchestrator()

# BUG REAL ENCONTRADO EN USO (2026-08-28): resource_broker.evict_idle_and_pressured()
# (el chequeo de RAM baja que libera TODO — imagen/audio/STT/Ollama —
# ante presión real, ver kernel/broker/resource_broker.py) solo se
# llamaba en dos momentos puntuales: antes de un /chat y antes de
# cargar un pipeline pesado. Dos caídas reales de sistema pasaron con
# NINGÚN pedido nuevo a kal disparando ese chequeo mientras la RAM se
# agotaba por otra causa (una suite de tests corriendo en paralelo,
# sin pedirle nada a kal) — nada evictaba nada hasta que el OOM killer
# del sistema operativo actuó a ciegas (mató un proceso de VS Code, no
# necesariamente el que más RAM usaba). Este thread corre el MISMO
# chequeo (sin cambios de lógica) de forma periódica, independiente
# del tráfico real — la lógica de qué evictar y cuándo sigue siendo
# exactamente la que ya existía y ya está probada.
_pressure_check_stop = threading.Event()


def _pressure_check_loop() -> None:
    interval = settings.resource_broker.pressure_check_interval_seconds
    while not _pressure_check_stop.wait(interval):
        try:
            resource_broker.evict_idle_and_pressured()
        except Exception as e:
            # Nunca debe tumbar el thread de background por un error
            # transitorio (Ollama caído un instante, etc.) — el próximo
            # ciclo lo vuelve a intentar solo.
            logger.warning(f"Chequeo periódico de presión de RAM falló, se reintenta en el próximo ciclo: {e}")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Arranca SOLO cuando la app realmente se sirve (uvicorn, o un
    # TestClient usado como context manager) — nunca con solo importar
    # este módulo, que es como lo usan los ~40 archivos de test de este
    # proyecto (`TestClient(app, base_url=...)` sin `with`), verificado
    # en vivo antes de este cambio para no arrancar un thread real de
    # background (con llamadas de red reales a Ollama) en cada test.
    # clear() antes de arrancar: _pressure_check_stop es un Event a
    # nivel de módulo — sin esto, un segundo arranque de la app dentro
    # del MISMO proceso (p.ej. dos usos consecutivos de
    # `with TestClient(app) as client:`, ver tests) vería la señal de
    # parada del apagado ANTERIOR ya activa y el thread nuevo terminaría
    # de inmediato, sin correr ni un solo ciclo.
    _pressure_check_stop.clear()
    thread = threading.Thread(target=_pressure_check_loop, daemon=True, name="ram-pressure-check")
    thread.start()
    yield
    _pressure_check_stop.set()
    thread.join(timeout=5)


# --- API HTTP ---
#
# Fase 0 de la propuesta "kal-in" (2026-08-23, ver docs/HISTORY.md):
# antes de construir ningún adaptador para otros frameworks de agentes,
# dejar el spec OpenAPI que FastAPI ya genera gratis (/openapi.json,
# /docs) en condiciones de servir como catálogo real — hasta acá tenía
# título pero sin descripción/versión/tags, y ningún endpoint tenía
# `summary=` (algunos ni docstring: los comentarios con `#` que parecen
# documentación no cuentan, FastAPI solo lee el docstring real).
app = FastAPI(
    title="Kal",
    lifespan=_lifespan,
    description=(
        "API HTTP del agente Kal: cada acción (herramienta, escritura de "
        "archivo, acceso a red) pasa por un kernel de seguridad propio — "
        "permisos explícitos (/filesystem-access, /network-access), "
        "sandboxing de herramientas y un log de auditoría con cadena "
        "verificable (/audit/tail). Punto de entrada del agente: /chat."
    ),
    version="0.1.0",
    openapi_tags=[
        {"name": "Chat", "description": "Punto de entrada del agente."},
        {"name": "Permisos", "description": "Aprobación/denegación de acceso a filesystem y red."},
        {"name": "Auditoría", "description": "Log de auditoría con cadena verificable."},
        {"name": "Herramientas", "description": "Herramientas y Skills activas/pendientes de aprobación."},
        {"name": "Sistema", "description": "Salud, estado de las garantías de seguridad, modelos disponibles."},
        {"name": "Memoria", "description": "Memoria de corto/mediano/largo plazo del agente."},
        {"name": "Tareas", "description": "Tareas asíncronas de larga duración."},
        {"name": "Configuración LLM", "description": "Proveedor/modelo del LLM y perfiles cloud."},
        {"name": "Diagnóstico", "description": "Invariantes de auto-diagnóstico y auto-reparación."},
        {"name": "Auto-modificación", "description": "Propuestas de cambio de código del propio agente."},
        {"name": "Skills (propuestas)", "description": "Propuestas de Skills nuevas pendientes de aprobación humana."},
        {"name": "Integración VS Code", "description": "Uso interno de la extensión de VS Code."},
    ],
)

# Recomendación de una auditoría externa (2026-08-24, ver docs/HISTORY.md):
# validar Host/Origin como defensa adicional en profundidad. Nunca fue el
# mecanismo real que protege /admin-token (ese usa request.client.host,
# la IP real de la conexión — no falsificable por un cliente remoto, ver
# más abajo) ni al agente (el SSRF vía browser/download_manager ya lo
# cierra kernel/permissions/network_safety.py::is_unsafe_ip) — esto
# cubre un vector DISTINTO: una página abierta en el MISMO navegador que
# el usuario usa para kal, intentando disparar pedidos contra
# localhost:8000 (rebinding de DNS / confusión de Host).
_ALLOWED_LOCAL_HOSTNAMES = ("localhost", "127.0.0.1")
_ALLOWED_ORIGINS = frozenset(f"http://{host}:8000" for host in _ALLOWED_LOCAL_HOSTNAMES)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_ALLOWED_LOCAL_HOSTNAMES))


class _OriginValidationMiddleware(BaseHTTPMiddleware):
    """
    Rechaza cualquier pedido con un header Origin presente que no sea
    exactamente uno de los orígenes locales esperados. Deliberadamente
    NO exige que Origin esté presente — la mayoría de los clientes
    reales de esta API (la extensión de VS Code vía Node, curl, scripts)
    nunca mandan ese header (es un concepto de navegador, no de HTTP en
    general), así que exigirlo rompería esos clientes legítimos sin
    ganar nada: un atacante que arma sus propios pedidos tampoco tiene
    obligación de mandarlo. Lo que sí se gana es cerrar la puerta a que
    JavaScript corriendo en una página de OTRO origen, en el navegador
    del usuario, dispare pedidos contra esta API — ahí el navegador SÍ
    agrega Origin, y no se puede falsificar desde JS de página.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in _ALLOWED_ORIGINS:
            return PlainTextResponse("Origin no permitido", status_code=403)
        return await call_next(request)


app.add_middleware(_OriginValidationMiddleware)

# Segunda capa de defensa (la primera es que docker-compose ya solo
# publica este puerto en 127.0.0.1, ver docker-compose.yml) para las
# acciones que hoy hacen de facto de "aprobación humana": self-
# modification y aprobación/rollback de herramientas. Sin esto,
# `approved_by` era un string que el propio cliente elegía — no
# verificaba ninguna identidad real. Token persistido en disco (ver
# utils/admin_token.py), no en el código ni en config.yaml.
_ADMIN_TOKEN = get_or_create_admin_token()
logger.info(
    "Token administrativo generado/leído para self-modification y aprobación de "
    "herramientas. Para usar esas acciones desde el frontend, abrilo una vez como "
    f"http://localhost:8000/?admin_token={_ADMIN_TOKEN}"
)


def require_admin_token(x_kal_admin_token: str | None = Header(default=None)) -> None:
    if x_kal_admin_token is None or not secrets.compare_digest(x_kal_admin_token, _ADMIN_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Token administrativo inválido o ausente (header X-Kal-Admin-Token).",
        )


_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})


# Fricción real encontrada en uso: pedirle a un usuario no-programador
# que copie el token administrativo de una terminal para poder usar la
# interfaz web era impracticable. Esto se lo entrega automáticamente al
# FRONTEND (nunca al agente ni a una skill: no es un tool, no hay forma
# de que el LLM llegue a esto) — pero SOLO si la conexión viene de
# loopback (mismo criterio que docker-compose.yml: 127.0.0.1). Quien
# accede desde la propia máquina donde corre kal ya podría leer
# data/keys/admin_token directamente del disco — esto no le da a un
# atacante remoto ninguna capacidad nueva, solo evita que el usuario
# legítimo tenga que ir a buscarlo a mano. Alguien conectándose desde
# otra máquina en la LAN (el caso real que este token protege) sigue
# sin poder obtenerlo por acá.
@app.get(
    "/admin-token",
    tags=["Sistema"],
    summary="Token administrativo (solo desde loopback)",
    description="Entrega el token de X-Kal-Admin-Token al frontend — solo responde si la conexión viene de 127.0.0.1/::1.",
)
def get_admin_token_endpoint(request: Request):
    if request.client is None or request.client.host not in _LOOPBACK_ADDRESSES:
        raise HTTPException(status_code=403, detail="Solo disponible desde la misma máquina donde corre kal.")
    return {"token": _ADMIN_TOKEN}


def _reinject_llm_client() -> None:
    """
    Reconstruye el cliente real y lo re-inyecta en todo lo que ya
    tenía una referencia — sin esto, cambiar el proveedor/perfil activo
    no tendría efecto hasta reiniciar el proceso entero.
    """
    orchestrator.llm = build_llm_client()
    orchestrator.agent.llm = orchestrator.llm
    orchestrator.planning_agent.planner.llm = orchestrator.llm
    orchestrator.self_diagnosis.llm = orchestrator.llm


# --- Artefactos (imágenes/audio/video generados o subidos) ---
_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "data" / "artifacts"
_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def _artifact_url(uri: str) -> str | None:
    """
    Traduce una ruta de archivo real (uri de un Artifact) a la URL
    servida por el mount /artifacts (solo lectura, ver más abajo) para
    que el frontend pueda mostrarla en <img src=...>. None si la ruta
    no está bajo data/artifacts/ (no se puede servir).

    Hallazgo de la revisión de seguridad 2026-07-09: la versión anterior
    comparaba con Path.relative_to() SIN resolver antes — un uri como
    "data/artifacts/../../etc/passwd" tiene ('data', 'artifacts') como
    prefijo literal de sus partes, así que relative_to() lo aceptaba
    igual (devolviendo "../../etc/passwd"), dependiendo enteramente de
    que Starlette bloqueara el traversal real al servir el archivo. Hoy
    no hay ningún llamador que pase un uri controlado por un tercero,
    pero la función debe ser segura POR SÍ SOLA, no solo por la capa que
    la usa después. resolve() normaliza ".." y símlinks ANTES de
    comparar, así que un intento de escape termina en una ruta absoluta
    fuera de _ARTIFACTS_DIR y relative_to() lo rechaza de verdad.
    """
    try:
        return f"/artifacts/{Path(uri).resolve().relative_to(_ARTIFACTS_DIR)}"
    except ValueError:
        # BUG REPORTADO EN USO (2026-09-22): "no siempre muestra la
        # miniatura" en el kiosko — el texto de la respuesta SÍ confirma
        # ubicación (_artifact_to_observation() imprime artifact.uri
        # crudo, sin pasar por acá), pero la miniatura estructurada
        # depende de ESTA función, que hasta ahora fallaba en silencio.
        # Sin poder reproducirlo en pruebas en vivo (tool directa y
        # skill vía kernel funcionaron bien en varias corridas), este
        # log es la instrumentación para atrapar el caso real la
        # próxima vez, con el uri exacto que no resolvió, en vez de
        # seguir adivinando a ciegas.
        logger.warning(f"_artifact_url(): uri no resuelve bajo {_ARTIFACTS_DIR}, miniatura no se mostrará: {uri!r}")
        return None


# --- Routers por dominio (ver agent_core/routers/) ---
# Importados recién acá, DESPUÉS de que orchestrator/require_admin_token/
# _artifact_url/_reinject_llm_client ya existen en este módulo — cada
# router hace `from agent_core.orchestrator import ...` de estos nombres,
# así que tienen que estar definidos antes de este punto.
from agent_core.routers import (  # noqa: E402
    android_build,
    audit,
    chat,
    diagnostics,
    health,
    llm_settings,
    memory,
    permissions,
    self_modification,
    skill_creator,
    tasks,
    tools,
    vscode_integration,
)

for _router_module in (
    health, llm_settings, chat, tasks, tools, memory,
    self_modification, permissions, diagnostics, vscode_integration, audit,
    skill_creator, android_build,
):
    app.include_router(_router_module.router)


# --- CSS/JS del frontend, servidos SIN cache ---
# BUG REAL ENCONTRADO EN USO: StaticFiles (más abajo) deja que el
# navegador cachee style.css/app.js con su heurística por defecto — al
# iterar rápido sobre el frontend en esta sesión, varios cambios de CSS
# no se veían ni con un hard refresh manual. Estas dos rutas explícitas
# (registradas ANTES del mount catch-all, así que Starlette las
# resuelve primero) fuerzan Cache-Control: no-store — el navegador
# nunca sirve una copia vieja de estos dos archivos.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/style.css", include_in_schema=False)
def serve_style_css():
    return FileResponse(_FRONTEND_DIR / "style.css", media_type="text/css", headers={"Cache-Control": "no-store"})


@app.get("/app.js", include_in_schema=False)
def serve_app_js():
    return FileResponse(_FRONTEND_DIR / "app.js", media_type="application/javascript", headers={"Cache-Control": "no-store"})


# Fuera del spec OpenAPI (include_in_schema=False, arriba y abajo):
# estas 3 rutas sirven el frontend estático, no son operaciones de la
# API — dejarlas en /docs solo agrega ruido a alguien evaluando
# integrar con kal vía su API (ver Fase 0 de "kal-in", docs/HISTORY.md).
@app.get("/", include_in_schema=False)
def serve_index_html():
    # BUG REAL ENCONTRADO EN USO: la suposición original de que
    # "index.html no cambia tan seguido" dejó de ser cierta — ganó
    # varios campos/ids nuevos en esta misma sesión (pestañas Modelo/
    # Integraciones). Un index.html viejo cacheado junto con un app.js
    # nuevo (ese sí ya servido sin cache) rompe en silencio: el JS
    # nuevo busca ids que el HTML viejo no tiene. Mismo criterio que
    # style.css/app.js de acá arriba.
    return FileResponse(_FRONTEND_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


app.mount("/artifacts", StaticFiles(directory=str(_ARTIFACTS_DIR)), name="artifacts")

# --- Frontend estático ---
# Mount catch-all en "/" — tiene que ser el ÚLTIMO mount registrado,
# cualquier ruta/mount nuevo va ANTES de este. La ruta explícita de
# arriba (serve_index_html) ya intercepta "/" sin cache; este mount
# sigue sirviendo cualquier OTRO archivo estático del frontend.
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

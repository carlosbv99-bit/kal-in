"""
Configuración en caliente del LLM activo (provider/base_url/
default_model/api_key) — persistida a disco (config/config.yaml +
.env) y aplicada en memoria de inmediato, sin reiniciar el proceso.

Pensado para que un usuario no-programador cambie de Ollama local a un
proveedor en la nube (Qwen, Grok/xAI, OpenAI...) o viceversa desde la
interfaz web, sin editar YAML a mano — kal se distribuye a usuarios
con hardware muy distinto (ver docs/HISTORY.md), no todos pueden
correr un modelo local grande.

Reemplazo de texto DIRIGIDO, nunca yaml.dump()/reescritura completa —
mismo criterio que kernel/registry/skills.py::set_skill_enabled():
config.yaml tiene comentarios explicativos reales (ejemplos de
base_url por proveedor, benchmarks de default_model) que un
yaml.dump() destruiría.
"""
from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
import requests
import yaml

from agent_core.llm.openai_compatible_client import OpenAICompatibleClient
from kernel.permissions.network_safety import is_unsafe_ip
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_CONFIG_PATH = Path("config/config.yaml")
_ENV_PATH = Path(".env")
_ENV_EXAMPLE_PATH = Path(".env.example")
# Siempre el Ollama LOCAL de esta máquina, nunca settings.llm.base_url
# (que puede apuntar a un proveedor en la nube si ese es el proveedor
# ACTIVO) — descargar/listar modelos locales es una gestión aparte,
# independiente de cuál proveedor esté activo en un momento dado.
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
_OLLAMA_PULL_TIMEOUT_SECONDS = 3600
# Perfiles de proveedores en la nube guardados (nombre/base_url/nombre
# de variable de entorno — NUNCA la key en sí, esa vive solo en .env).
# Archivo propio, no config.yaml: es una lista que crece con el uso,
# 100% generada por kal — a diferencia de config.yaml, no tiene
# comentarios de autor que preservar, así que un dump completo es
# seguro acá (ver _save_cloud_profiles_file más abajo).
_CLOUD_PROFILES_PATH = Path("data/keys/cloud_profiles.json")


class LLMSettingsError(Exception):
    """La actualización pedida es inválida — nada se escribió a disco."""


def _validate_cloud_base_url(base_url: str) -> None:
    """
    VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    2026-09-26), A-7: un proveedor "openai_compatible" es, por
    definición, un servicio EN LA NUBE (a diferencia de "ollama", que
    sí soporta un base_url propio en la LAN a propósito) — no hay
    ningún caso de uso legítimo para que apunte a una IP privada/
    interna, y mandar LLM_API_KEY por http:// en vez de https:// la
    expone en texto plano en la red. Este endpoint ya exige token
    admin (ver require_admin_token en agent_core/orchestrator.py), pero
    es la misma filosofía de "cada capa se protege sola" que el resto
    de este archivo (p.ej. _ensure_enough_ram_to_load): un perfil
    guardado con un base_url malicioso (data/keys/cloud_profiles.json
    manipulado por fuera de este código) no debe poder redirigir la
    API key ni el tráfico del agente hacia un servicio interno.
    """
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise LLMSettingsError(
            f"'base_url' de un proveedor en la nube debe usar https:// (recibido: {parsed.scheme or '(ninguno)'}) "
            "— http:// expondría la API key en texto plano en la red."
        )
    hostname = parsed.hostname
    if not hostname:
        raise LLMSettingsError(f"'base_url' inválida: no se pudo determinar el host de '{base_url}'.")
    try:
        resolved_ip = socket.gethostbyname(hostname)
    except OSError:
        raise LLMSettingsError(f"No se pudo resolver el host '{hostname}' de 'base_url'.")
    if is_unsafe_ip(resolved_ip):
        raise LLMSettingsError(
            f"'base_url' resuelve a una IP privada/interna ({resolved_ip}) — un proveedor en la nube "
            "nunca debería apuntar ahí. Si necesitás un backend propio en tu red, usá provider='ollama' "
            "con un base_url propio, no 'openai_compatible'."
        )


def read_llm_env_var(key: str) -> str | None:
    """
    Lee el valor ACTUAL de una variable relacionada con API keys —
    SIEMPRE desde el archivo .env primero, nunca solo de os.environ.

    BUG REAL ENCONTRADO EN USO: `load_dotenv()` (utils/config.py, se
    re-ejecuta en cada --reload) NUNCA sobreescribe una variable que
    ya esté seteada en el proceso. Si en algún momento de la sesión
    quedó un valor viejo/incorrecto en os.environ (una prueba, un
    primer intento con la key equivocada), ningún --reload posterior
    lo iba a corregir — aunque el archivo .env tuviera después el
    valor correcto y más nuevo. Confirmado en vivo: una key de Groq
    real y válida quedaba rechazada con 401 porque el proceso seguía
    usando un valor viejo en memoria. Leer directo del archivo evita
    esta trampa por completo.
    """
    if _ENV_PATH.exists():
        text = _ENV_PATH.read_text(encoding="utf-8")
        match = re.search(rf'^{re.escape(key)}=(.*)$', text, re.MULTILINE)
        if match and match.group(1):
            return match.group(1)
    return os.environ.get(key)


def get_llm_settings() -> dict:
    return {
        "provider": settings.llm.provider,
        "base_url": settings.llm.base_url,
        "default_model": settings.llm.default_model,
        # Nunca se devuelve la key en sí — solo si hay una guardada.
        "has_api_key": bool(read_llm_env_var("LLM_API_KEY")),
    }


def update_llm_settings(
    provider: str,
    base_url: str | None = None,
    default_model: str | None = None,
    api_key: str | None = None,
    profile_name: str | None = None,
) -> None:
    """
    Valida ANTES de escribir nada (para no dejar config.yaml apuntando
    a un estado que después falla al reconstruir el cliente real —
    ver agent_core/orchestrator.py::build_llm_client()).

    `profile_name`: si se pasa junto con provider="openai_compatible",
    además de activarlo, lo guarda como perfil reusable (ver
    save_cloud_profile) — así "Guardar y activar" en la pestaña Modelo
    lo deja disponible para el selector de modelo más adelante, sin un
    paso separado de "guardar como perfil".
    """
    previous_base_url = settings.llm.base_url  # capturado ANTES de pisarlo, para detectar un cambio real de endpoint

    if provider == "openai_compatible":
        effective_base_url = base_url or settings.llm.base_url
        if not base_url and effective_base_url == _OLLAMA_DEFAULT_BASE_URL:
            raise LLMSettingsError(
                "Falta 'base_url' — un proveedor en la nube necesita la URL completa de su "
                "API (p.ej. https://api.x.ai/v1), nunca el default de Ollama local."
            )
        _validate_cloud_base_url(effective_base_url)
        effective_api_key = api_key or read_llm_env_var("LLM_API_KEY")
        if not effective_api_key:
            raise LLMSettingsError(
                "Falta 'api_key' — no hay ninguna guardada todavía para usar un proveedor en la nube."
            )
        # BUG REAL ENCONTRADO EN USO: default_model es GLOBAL (una sola
        # perilla en config.yaml), no por proveedor — un nombre de
        # modelo de Ollama (p.ej. "deepseek-r1:14b") se quedaba pegado
        # ahí después de activar un proveedor en la nube distinto (el
        # endpoint cambió), rompiendo cualquier /chat sin un 'model'
        # explícito con 404 "model not found". El selector web siempre
        # manda un 'model' explícito así que no lo sufre, pero el
        # agente IDE de VS Code no tiene selector propio — siempre
        # depende de este default, y ahí sí rompía. Si el endpoint
        # cambia de verdad y no se pidió un default_model explícito, se
        # elige automáticamente el primer modelo de chat real que ese
        # proveedor devuelva.
        if default_model is None and effective_base_url != previous_base_url:
            default_model = _first_chat_capable_model(effective_base_url, effective_api_key)
    elif provider == "ollama" and base_url is None:
        # BUG REAL ENCONTRADO EN USO: volver a "ollama" sin esto dejaba
        # `base_url` apuntando a lo que fuera el proveedor en la nube
        # anterior — OllamaClient terminaba pegándole a
        # "https://api.x.ai/v1/api/tags" (404), sin ninguna forma de
        # recuperarse desde la interfaz. Activar Ollama SIEMPRE vuelve
        # a su URL local conocida, salvo que se pase una distinta a
        # propósito (p.ej. un puerto no estándar).
        base_url = _OLLAMA_DEFAULT_BASE_URL

    # BUG REAL ENCONTRADO EN USO: aceptar acá un modelo Ollama local sin
    # soporte de tool-calling (p.ej. llava:13b, un modelo de solo
    # visión) rompía CUALQUIER mensaje posterior, hasta un simple
    # "hola", con 400 ("does not support tools") — el selector web ya
    # no lo ofrece (ver list_model_sources), pero esta es la validación
    # real que lo bloquea también si alguien lo pide por fuera del
    # selector (p.ej. una llamada directa a este endpoint).
    if provider == "ollama" and default_model is not None and not _ollama_model_supports_tools(default_model):
        raise LLMSettingsError(
            f"'{default_model}' no soporta llamadas a herramientas (tool-calling) — kal necesita "
            "esa capacidad en CUALQUIER modelo configurado como default_model del agente, ya que "
            "siempre ofrece herramientas en cada mensaje. Elegí otro modelo (o usalo solo para "
            "multimodal.vision.model en config.yaml, si es un modelo de visión)."
        )

    # BUG REAL ENCONTRADO EN USO (2026-08-28): el usuario seleccionó
    # deepseek-r1:14b (15.1GB) desde la pestaña Modelo mientras kal ya
    # tenía otros modelos cargados — la máquina (14GB de RAM total) se
    # quedó sin memoria real, GNOME mató procesos y VS Code se cayó.
    # Pedido explícito del usuario: "si la memoria está siendo usada y
    # no hay capacidad de cargar otro modelo, aunque el usuario lo
    # pida, kal no debe cargarlo". Este chequeo corre ANTES de aceptar
    # el cambio — nunca después, cuando ya sería tarde.
    if provider == "ollama" and default_model is not None:
        _ensure_enough_ram_to_load(default_model)

    if base_url is not None:
        _update_yaml_field("base_url", base_url)
        settings.llm.base_url = base_url
    if default_model is not None:
        _update_yaml_field("default_model", default_model)
        settings.llm.default_model = default_model
    _update_yaml_field("provider", provider)
    settings.llm.provider = provider

    if api_key:
        _update_env_var("LLM_API_KEY", api_key)
        os.environ["LLM_API_KEY"] = api_key

    if profile_name and provider == "openai_compatible":
        save_cloud_profile(
            profile_name,
            base_url=settings.llm.base_url,
            api_key=api_key or read_llm_env_var("LLM_API_KEY") or "",
        )


def _sanitize_env_suffix(name: str) -> str:
    """'Grok (xAI)' -> 'GROK_XAI' — usado para nombrar la variable de
    entorno propia de cada perfil (LLM_API_KEY_<esto>)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_").upper()
    return cleaned or "PERFIL"


def list_cloud_profiles() -> list[dict]:
    """Perfiles guardados (nombre/base_url/nombre de variable de
    entorno) — NUNCA la key en sí, esa se lee del .env al usarla."""
    if not _CLOUD_PROFILES_PATH.exists():
        return []
    return json.loads(_CLOUD_PROFILES_PATH.read_text(encoding="utf-8"))


def save_cloud_profile(name: str, base_url: str, api_key: str) -> None:
    """Guarda (o actualiza, si ya existe un perfil con ese nombre) un
    perfil de proveedor en la nube — la key se persiste en su PROPIA
    variable de entorno (LLM_API_KEY_<NOMBRE>), no se pisa con la de
    otro perfil ni con la del proveedor activo."""
    api_key_env = f"LLM_API_KEY_{_sanitize_env_suffix(name)}"
    profiles = list_cloud_profiles()
    profiles = [p for p in profiles if p["name"] != name]
    profiles.append({"name": name, "base_url": base_url, "api_key_env": api_key_env})

    _CLOUD_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CLOUD_PROFILES_PATH.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    # B-2 (auditoría externa Likay-OS, 2026-09-26): quedaba con los
    # permisos por defecto del proceso (típicamente 0644, legible por
    # cualquier otro usuario del sistema) — mismo criterio que
    # utils/admin_token.py::get_or_create_admin_token().
    _CLOUD_PROFILES_PATH.chmod(0o600)

    if api_key:
        _update_env_var(api_key_env, api_key)
        os.environ[api_key_env] = api_key


def activate_cloud_profile(name: str) -> None:
    """Hace ACTIVO un perfil ya guardado (lo que responde al próximo
    /chat) — reusa update_llm_settings(), sin volver a pedir la key."""
    profile = next((p for p in list_cloud_profiles() if p["name"] == name), None)
    if profile is None:
        raise LLMSettingsError(f"No existe un perfil guardado llamado '{name}'.")
    api_key = read_llm_env_var(profile["api_key_env"])
    if not api_key:
        raise LLMSettingsError(
            f"El perfil '{name}' no tiene una API key configurada en el entorno "
            f"({profile['api_key_env']})."
        )
    update_llm_settings(provider="openai_compatible", base_url=profile["base_url"], api_key=api_key)


# BUG REAL ENCONTRADO EN USO: GET /v1/models de un proveedor real
# (Groq) devuelve TODOS sus modelos hospedados, no solo los de chat —
# whisper-large-v3 (habla-a-texto), llama-prompt-guard/gpt-oss-safeguard
# (clasificadores de seguridad), orpheus (texto-a-voz), etc. aparecían
# mezclados con los modelos de chat de verdad, aunque nunca podrían
# responder a un /chat de kal. Filtro por nombre — heurístico, no una
# garantía (no hay un campo "tipo" estándar en la respuesta de
# /v1/models), pero cubre los casos reales encontrados.
_NON_CHAT_MODEL_KEYWORDS = ("whisper", "tts", "orpheus", "guard", "safeguard", "embed", "moderation", "rerank")


def _is_chat_capable_model_name(name: str) -> bool:
    lowered = name.lower()
    return not any(keyword in lowered for keyword in _NON_CHAT_MODEL_KEYWORDS)


def get_ollama_model_capabilities(name: str) -> list[str]:
    """
    Consulta las capacidades REALES de un modelo Ollama local vía
    `/api/show` (p.ej. `["completion", "tools"]` para un modelo de
    chat/código, `["completion", "vision"]` para uno de solo visión) —
    la misma fuente de verdad usada para diagnosticar en vivo por qué
    llava:13b rompía el chat como modelo principal. Lista vacía ante
    cualquier error (Ollama caído, modelo no encontrado) — informativo,
    no gatea nada por sí solo (ver `_ollama_model_supports_tools`, que
    sí es fail-closed para esa decisión específica).
    """
    try:
        response = requests.post(f"{_OLLAMA_DEFAULT_BASE_URL}/api/show", json={"name": name}, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.warning(f"No se pudieron consultar las capacidades de '{name}': {e}")
        return []
    return response.json().get("capabilities", [])


def _ollama_model_supports_tools(name: str) -> bool:
    """
    BUG REAL ENCONTRADO EN USO: el selector de modelo de la web dejaba
    elegir CUALQUIER modelo local (incluido uno de solo visión como
    llava:13b, sin soporte de tool-calling) como default_model — el
    agente SIEMPRE manda `tools` en cada request, así que Ollama
    rechazaba con 400 ("does not support tools") absolutamente
    cualquier mensaje, hasta un simple "hola", rompiendo el chat
    entero apenas se lo seleccionaba. A diferencia de
    `_is_chat_capable_model_name` (heurística por palabras clave, la
    única opción para proveedores en la nube sin una API de
    capacidades), esto usa `get_ollama_model_capabilities()` — la
    fuente de verdad real de Ollama, no una adivinanza por el nombre.
    Fail-closed ante cualquier error (capabilities vacío): un modelo
    que no se puede confirmar no aparece en el selector, en vez de
    arriesgarse a ofrecer una opción rota.
    """
    return "tools" in get_ollama_model_capabilities(name)


def _available_ram_mb() -> float:
    """Misma fuente que kernel/broker/resource_broker.py — RAM
    REALMENTE disponible del sistema ahora mismo, no la total."""
    return psutil.virtual_memory().available / (1024 * 1024)


def _ollama_loaded_model_names() -> set[str]:
    """Modelos que Ollama YA tiene cargados en RAM ahora mismo (`/api/ps`)
    — cambiar a uno de estos no consume memoria NUEVA, así que no debe
    bloquearse por este chequeo."""
    try:
        response = requests.get(f"{_OLLAMA_DEFAULT_BASE_URL}/api/ps", timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return set()
    return {m["name"] for m in response.json().get("models", [])}


def _ollama_model_size_mb(name: str) -> float | None:
    """Tamaño en disco de un modelo YA descargado (`/api/tags`, campo
    `size` en bytes) — la mejor estimación disponible de cuánta RAM va
    a pedir Ollama al cargarlo (los pesos ocupan aproximadamente lo
    mismo en RAM que en disco). None si no se pudo determinar."""
    try:
        response = requests.get(f"{_OLLAMA_DEFAULT_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    for model in response.json().get("models", []):
        if model.get("name") == name:
            size_bytes = model.get("size")
            return size_bytes / (1024 * 1024) if size_bytes is not None else None
    return None


def _ensure_enough_ram_to_load(name: str) -> None:
    """
    BUG REAL ENCONTRADO EN USO (2026-08-28): el usuario seleccionó
    deepseek-r1:14b (15.1GB) desde la pestaña Modelo de la interfaz web
    mientras Ollama ya tenía otros modelos cargados — la máquina (14GB
    de RAM total) se quedó sin memoria real, GNOME mató procesos y
    VS Code se cayó. El ResourceBroker (kernel/broker/resource_broker.py)
    protege la RAM que kal mismo gestiona (sus propios pipelines
    pesados vs. el modelo de chat), pero no tenía ningún chequeo ANTES
    de aceptar un cambio de modelo pedido explícitamente por el
    usuario — ahí no hay nada que "evictar", hay que directamente
    rechazar el cambio si no entra.

    Fail-closed (mismo criterio que _ollama_model_supports_tools):
    si no se puede determinar el tamaño real del modelo, se rechaza
    el cambio en vez de arriesgarse a cargarlo a ciegas.
    """
    if name in _ollama_loaded_model_names():
        return  # ya está cargado — cambiar a este modelo no pide RAM nueva
    size_mb = _ollama_model_size_mb(name)
    if size_mb is None:
        raise LLMSettingsError(
            f"No se pudo determinar el tamaño de '{name}' para confirmar que hay RAM suficiente "
            "antes de cargarlo — por seguridad, no se activa sin poder confirmarlo."
        )
    available_mb = _available_ram_mb()
    min_headroom_mb = settings.resource_broker.min_available_ram_mb
    if available_mb - size_mb < min_headroom_mb:
        raise LLMSettingsError(
            f"No hay RAM suficiente para cargar '{name}' ({size_mb:.0f}MB) sin arriesgar el "
            f"sistema — disponible ahora: {available_mb:.0f}MB, mínimo que debe quedar libre "
            f"después de cargarlo: {min_headroom_mb}MB. Liberá memoria (cerrando otros modelos u "
            "otros programas) o elegí un modelo más chico."
        )


def _first_chat_capable_model(base_url: str, api_key: str) -> str | None:
    """Usado por update_llm_settings() para elegir un default_model
    razonable al activar un proveedor en la nube distinto sin uno
    explícito — None si el proveedor no responde (queda sin cambiar,
    no peor que el estado actual)."""
    try:
        client = OpenAICompatibleClient(base_url=base_url, api_key=api_key)
        models = [m for m in client.list_models() if _is_chat_capable_model_name(m)]
    except Exception as e:
        logger.warning(f"No se pudo elegir un modelo automático para {base_url}: {e}")
        return None
    return models[0] if models else None


def list_model_sources() -> list[dict]:
    """
    Modelos disponibles de TODAS las fuentes conocidas — Ollama local +
    cada perfil en la nube guardado que responda con éxito AHORA MISMO
    (nunca uno roto: sin crédito, key inválida, etc. simplemente no
    aparece, en vez de mostrar un error a medias). Es lo que alimenta
    el selector de modelo del chat — no depende de cuál proveedor esté
    ACTIVO en este momento.

    Filtra tres casos reales de "aparece pero no está listo para usar":
    modelos que no son de chat (ver _NON_CHAT_MODEL_KEYWORDS), modelos
    Ollama con sufijo ":cloud" — esos son en realidad un proxy al
    servicio en la nube DE OLLAMA MISMO, que necesita una sesión propia
    (`ollama signin`) sin relación con esta configuración; sin ella,
    devuelven 401 al primer uso, confirmado en vivo — y modelos locales
    SIN soporte de tool-calling (ver _ollama_model_supports_tools): un
    modelo de solo visión como llava:13b elegido acá rompe CUALQUIER
    mensaje, hasta un simple "hola", con 400 ("does not support
    tools"), confirmado en vivo.
    """
    sources: list[dict] = []
    try:
        local_models = [
            m for m in list_local_ollama_models()
            if not m.endswith(":cloud") and _ollama_model_supports_tools(m)
        ]
        sources.append({"name": "ollama", "label": "Local (Ollama)", "models": local_models})
    except LLMSettingsError:
        pass

    for profile in list_cloud_profiles():
        api_key = read_llm_env_var(profile["api_key_env"])
        if not api_key:
            logger.warning(
                f"Perfil '{profile['name']}' sin API key en el entorno ({profile['api_key_env']}) — "
                "no aparece en el selector de modelo."
            )
            continue
        try:
            client = OpenAICompatibleClient(base_url=profile["base_url"], api_key=api_key)
            models = [m for m in client.list_models() if _is_chat_capable_model_name(m)]
        except Exception as e:
            # BUG REAL ENCONTRADO EN USO: esto solo atrapaba ProviderError
            # — cualquier otro tipo de excepción real (JSON inesperado,
            # timeout de red puntual) hacía desaparecer el perfil del
            # selector SIN NINGÚN rastro en los logs, indiagnosticable a
            # ciegas. Ahora cualquier fallo queda registrado con su causa
            # real antes de saltear el perfil.
            logger.warning(f"Perfil '{profile['name']}' no respondió al listar modelos, se omite del selector: {e}")
            continue
        sources.append({"name": profile["name"], "label": profile["name"], "models": models})

    return sources


def list_local_ollama_models() -> list[str]:
    """
    Modelos YA descargados en el Ollama local de esta máquina —
    independiente de cuál proveedor esté ACTIVO ahora mismo (a
    diferencia de GET /models, que lista los modelos del proveedor
    activo, sea local o en la nube).
    """
    try:
        response = requests.get(f"{_OLLAMA_DEFAULT_BASE_URL}/api/tags", timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LLMSettingsError(f"No se pudo conectar a Ollama local en {_OLLAMA_DEFAULT_BASE_URL}: {e}") from e
    data: dict[str, Any] = response.json()
    return [m["name"] for m in data.get("models", [])]


def pull_ollama_model(model: str) -> None:
    """
    Descarga un modelo nuevo al Ollama local (`ollama pull <model>`,
    vía la misma API HTTP que usa el CLI — sin depender de que el
    binario 'ollama' esté en el PATH del proceso de kal). Bloqueante:
    una descarga real puede tardar varios minutos (varios GB) —
    timeout generoso a propósito, no un valor pensado para llamadas
    normales de chat.
    """
    try:
        response = requests.post(
            f"{_OLLAMA_DEFAULT_BASE_URL}/api/pull",
            json={"name": model, "stream": False},
            timeout=_OLLAMA_PULL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LLMSettingsError(f"No se pudo descargar '{model}': {e}") from e
    data = response.json()
    if data.get("status") not in ("success", None):
        raise LLMSettingsError(f"Ollama no confirmó la descarga de '{model}': {data}")


def _update_yaml_field(key: str, value: str) -> None:
    """
    VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    2026-09-26), M-5: `value` se embebía como f'"{value}"' crudo — un
    valor con una comilla o un salto de línea rompía el YAML, o peor,
    INYECTABA nuevas claves. No es un caso solo teórico: default_model
    puede llegar acá elegido automáticamente por
    _first_chat_capable_model(), que lee nombres de modelo devueltos
    por el SERVIDOR del proveedor en la nube configurado — un proveedor
    comprometido/malicioso podría devolver un nombre con esa forma.
    yaml.safe_dump() produce un escalar YAML correctamente escapado
    (comillas, saltos de línea, backslashes) sin depender de que
    `value` nunca contenga nada especial.
    """
    if "\n" in value or "\r" in value:
        # La sustitución de abajo reemplaza UNA línea completa — un
        # valor multilínea (p.ej. un "modelo" con un salto de línea
        # embebido, devuelto por un proveedor en la nube comprometido
        # vía _first_chat_capable_model()) no tiene una representación
        # YAML de una sola línea segura acá. Fail closed, igual que
        # _update_env_var().
        raise LLMSettingsError(f"El valor para '{key}' no puede contener saltos de línea.")

    text = _CONFIG_PATH.read_text(encoding="utf-8")
    # yaml.safe_dump de un dict de una clave (en vez del valor pelado)
    # evita el marcador de fin de documento "..." que PyYAML agrega
    # SIEMPRE que el nodo raíz de un dump es un escalar suelto.
    dumped = yaml.safe_dump({"v": value}).strip()
    safe_value = dumped[len("v:"):].strip()
    # Ancla a INICIO DE LÍNEA — nunca matchea los ejemplos comentados
    # (p.ej. "  #     base_url: ...") porque después de la indentación
    # el próximo carácter ahí es '#', no el nombre de la clave.
    pattern = re.compile(rf'^(\s*){re.escape(key)}:\s*.*$', re.MULTILINE)
    new_text, count = pattern.subn(lambda m: f"{m.group(1)}{key}: {safe_value}", text, count=1)
    if count == 0:
        raise LLMSettingsError(f"No se encontró la clave '{key}' en config.yaml — no se pudo actualizar.")
    _CONFIG_PATH.write_text(new_text, encoding="utf-8")


def _update_env_var(key: str, value: str) -> None:
    # M-5 (auditoría externa Likay-OS, 2026-09-26): un valor con un
    # salto de línea literal podía inyectar una variable de entorno
    # NUEVA en .env (p.ej. una api_key pegada con "\nOTRA_VAR=x" dentro)
    # — fail closed en vez de escribir algo que después se interpreta
    # como dos líneas distintas.
    if "\n" in value or "\r" in value:
        raise LLMSettingsError(f"El valor para '{key}' no puede contener saltos de línea.")

    if not _ENV_PATH.exists():
        base = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8") if _ENV_EXAMPLE_PATH.exists() else ""
        _ENV_PATH.write_text(base, encoding="utf-8")

    text = _ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    if pattern.search(text):
        # Reemplazo vía lambda a propósito: pattern.sub(f"{key}={value}", ...)
        # interpreta backreferences (\1, \g<...>) DENTRO de `value` si
        # el valor los contiene literalmente (p.ej. una key que
        # empiece con "\g" o "\1") — un bug de correctness real,
        # separado de la inyección de arriba. La lambda trata `value`
        # como texto literal, nunca como patrón de reemplazo de re.
        new_text = pattern.sub(lambda m: f"{key}={value}", text, count=1)
    else:
        sep = "\n" if text and not text.endswith("\n") else ""
        new_text = f"{text}{sep}{key}={value}\n"
    _ENV_PATH.write_text(new_text, encoding="utf-8")
    # B-2 (auditoría externa Likay-OS, 2026-09-26): .env tiene API keys
    # en texto plano — quedaba con los permisos por defecto del proceso
    # (típicamente 0644, legible por cualquier otro usuario del
    # sistema). Mismo criterio que utils/admin_token.py.
    _ENV_PATH.chmod(0o600)

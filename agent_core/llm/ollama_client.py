"""
Cliente HTTP mínimo para Ollama — una implementación de LLMProvider
(agent_core/llm/provider.py) entre varias posibles. El modelo de
lenguaje del agente corre 100% local vía Ollama por defecto, nunca
contra una API en la nube (ver LLMConfig en utils/config.py: modelos
":cloud" como glm-5.1:cloud deben seleccionarse explícitamente, nunca
como default).

NOTA DE TRANSPARENCIA: no tengo forma de ejecutar Ollama en el entorno
donde escribo este código (sin red, sin el binario instalado). La
implementación sigue la API HTTP documentada de Ollama (/api/chat,
formato de tools estilo OpenAI) tal como la conozco, pero si tu versión
de Ollama difiere en el formato exacto de tool_calls o del mensaje de
resultado de herramienta (role="tool"), puede necesitar un ajuste menor
— avisar si el primer intento real falla, mismo patrón que tuvimos con
piper-tts y moviepy.

Esta llamada HTTP la hace el proceso principal del agente hacia un
servicio local (Ollama en localhost:11434), no código sandboxeado —
por eso no pasa por las restricciones de red del sandbox (ver
kernel/lifecycle/docker_runner.py): es el propio agente usando un servicio local
de la máquina, igual que los adaptadores multimodales usan modelos
locales directamente.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

from agent_core.llm.provider import ChatResponse, ProviderError, ToolCall
from kernel.broker.resource_broker import resource_broker as _default_resource_broker
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

PostFn = Callable[..., Any]
GetFn = Callable[..., Any]
SleepFn = Callable[[float], None]


class OllamaError(ProviderError):
    """Error específico de OllamaClient. Subclase de ProviderError — el
    núcleo (agent_loop.py, planner.py, self_diagnosis.py) atrapa
    ProviderError, nunca este tipo directamente."""


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        connection_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        post_fn: PostFn | None = None,
        get_fn: GetFn | None = None,
        sleep_fn: SleepFn | None = None,
        resource_broker=None,
    ):
        self.base_url = (base_url or settings.llm.base_url).rstrip("/")
        self.timeout = timeout or settings.llm.timeout_seconds
        # BUG REAL ENCONTRADO EN USO: con generación de imagen/audio/video
        # corriendo en la misma máquina, Ollama puede quedar momentáneamente
        # sin responder (recargando el modelo en VRAM/RAM) y romper TODA la
        # tarea con un solo ConnectionError transitorio. Reintentar cubre ese
        # hueco real sin esconder una caída de verdad (no se reintenta en
        # Timeout: eso es una generación lenta, no una desconexión — ni en
        # HTTPError: eso es un error real del servidor, reintentar no ayuda).
        self.connection_retries = (
            connection_retries if connection_retries is not None else settings.llm.connection_retries
        )
        self.retry_backoff_seconds = (
            retry_backoff_seconds if retry_backoff_seconds is not None else settings.llm.retry_backoff_seconds
        )
        # post_fn/get_fn/sleep_fn inyectables para tests sin red real ni
        # esperas reales (mismo patrón que OpenAICompatibleClient).
        self._post = post_fn or requests.post
        self._get = get_fn or requests.get
        self._sleep = sleep_fn or time.sleep
        # inyectable para tests; el default real es el singleton
        # compartido de kernel/broker/resource_broker.py.
        self._resource_broker = resource_broker or _default_resource_broker

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        images: list[str] | None = None,
        response_format: str | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        """
        Llama a POST /api/chat. `messages` sigue el formato
        {"role": "system"|"user"|"assistant"|"tool", "content": str}.
        `tools` sigue el formato de function-calling estilo OpenAI:
        [{"type": "function", "function": {"name", "description", "parameters"}}].
        `images`: strings base64 (sin prefijo "data:") para modelos de
        visión (p.ej. llama3.2-vision) — se adjuntan al ÚLTIMO mensaje,
        formato documentado de /api/chat de Ollama. No modifica
        `messages` in-place (evita mutar la lista del llamador).
        `response_format`: pasa tal cual como "format" en el payload de
        Ollama — p.ej. "json" fuerza que `message.content` sea un
        string JSON válido (usado por agent_core/conversation_engine.py,
        validado empíricamente contra qwen2.5:3b/gemma3:4b/llama3.2:3b).
        Default None preserva el comportamiento actual de todo el resto
        de los llamadores (agent_loop.py, planner.py, self_diagnosis.py).
        `temperature`: pasa tal cual en "options.temperature" del payload
        de Ollama. BUG REAL ENCONTRADO EN USO (2026-07-25): sin esto,
        agent_core/conversation_engine.py::classify() usaba el
        temperature default de Ollama (alto, pensado para conversación
        variada) — el MISMO pedido exacto ("crea una naranja") devolvía
        a veces confidence=0.9 y a veces <0.5, cruzando el
        confidence_threshold de forma no determinística: el usuario veía
        "a veces genera, a veces no" para pedidos idénticos. Default
        None preserva el comportamiento actual para todo el resto de los
        llamadores.
        """
        # Libera RAM de servicios multimedia inactivos ANTES de pedirle a
        # Ollama (local, misma RAM del sistema) que genere — ver
        # kernel/broker/resource_broker.py, bug real de contención de RAM.
        self._resource_broker.evict_idle_and_pressured()

        if images:
            messages = [*messages[:-1], {**messages[-1], "images": images}]

        payload: dict[str, Any] = {
            "model": model or settings.llm.default_model,
            "messages": messages,
            "stream": False,
            # BUG REAL ENCONTRADO EN USO (2026-07-30): modelos "híbridos"
            # con modo de razonamiento (qwen3.5, etc.) a veces gastan TODA
            # la generación en un campo "reasoning"/"thinking" separado y
            # dejan `message.content` completamente vacío, incluso para
            # pedidos triviales ("respondé solo con la palabra: listo") —
            # confirmado en vivo: con think=false, el mismo pedido responde
            # de inmediato con content="listo" (3 tokens en vez de 250+).
            # Ollama ignora este campo sin error en modelos que no lo
            # soportan (confirmado con qwen2.5:3b) — seguro de mandar
            # siempre, no solo para modelos "thinking" conocidos.
            "think": False,
            # Diagnóstico de lentitud (2026-08-23): sin esto, Ollama usa
            # su propio default de systemd (OLLAMA_KEEP_ALIVE, 5m acá) —
            # un segundo temporizador independiente del resource_broker
            # de kal, que puede descargar el modelo ANTES de que el
            # timeout propio (más largo, ver ResourceBrokerConfig.
            # ollama_idle_timeout_seconds) llegue a aplicarse. Se manda
            # el doble de ese valor acá a propósito: el resource_broker
            # de kal queda como la autoridad real de cuándo descargar
            # (incluida la evicción inmediata ante RAM baja, ver
            # kernel/broker/resource_broker.py), esto es solo una red de
            # seguridad generosa para que Ollama nunca actúe primero.
            "keep_alive": settings.resource_broker.ollama_idle_timeout_seconds * 2,
        }
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["format"] = response_format
        # BUG REAL ENCONTRADO EN USO (2026-09-22): sin esto, un modelo que
        # entra en un bucle de repetición nunca para solo — sigue generando
        # hasta agotar la ventana de contexto completa (miles de tokens,
        # varios minutos de cómputo real en CPU) antes de que timeout_seconds
        # corte la espera. num_predict acota el PEOR caso a un tiempo finito
        # sin depender de que el modelo encuentre un stop token por su cuenta
        # (ver settings.llm.max_response_tokens para el porqué del valor).
        options = {"num_predict": settings.llm.max_response_tokens}
        if temperature is not None:
            options["temperature"] = temperature
        payload["options"] = options

        response = self._post_with_retry(payload)
        data = response.json()
        message = data.get("message", {})
        content = message.get("content", "") or ""

        tool_calls = []
        for raw_call in message.get("tool_calls", []) or []:
            function = raw_call.get("function", {})
            arguments = function.get("arguments", {})
            # Defensivo: algunas versiones/modelos podrían devolver los
            # argumentos como string JSON en vez de objeto ya parseado.
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    logger.warning(f"No se pudo parsear arguments de tool_call como JSON: {arguments!r}")
                    arguments = {}
            tool_calls.append(ToolCall(name=function.get("name", ""), arguments=arguments, id=raw_call.get("id")))

        return ChatResponse(content=content, tool_calls=tool_calls, raw=data)

    def _post_with_retry(self, payload: dict[str, Any]):
        attempts = self.connection_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
                response.raise_for_status()
                return response
            except requests.exceptions.ConnectionError as e:
                if attempt >= attempts:
                    raise OllamaError(
                        f"No se pudo conectar a Ollama en {self.base_url} tras {attempts} intentos. "
                        "¿Está corriendo? ('ollama serve' o el servicio del sistema)"
                    ) from e
                logger.warning(
                    f"Ollama no respondió (intento {attempt}/{attempts}), reintentando en "
                    f"{self.retry_backoff_seconds}s: {e}"
                )
                self._sleep(self.retry_backoff_seconds)
            except requests.exceptions.Timeout as e:
                raise OllamaError(
                    f"Ollama no respondió en {self.timeout}s (un modelo grande en CPU puede tardar; "
                    "subir llm.timeout_seconds en config.yaml si esto pasa seguido)"
                ) from e
            except requests.exceptions.HTTPError as e:
                # BUG REAL ENCONTRADO EN USO (2026-08-23): "crea un proyecto de agenda
                # para android" (propose_project_files con 12 archivos, una llamada a
                # herramienta larga/compleja) tiró 500 tras 48s y 8217 tokens —
                # confirmado en el propio log de Ollama (journalctl -u ollama):
                # "qwen3.5 tool call parsing failed" / "XML syntax error... element
                # <function> closed by </parameter>". No es un bug de kal: el MODELO
                # generó XML mal formado al describir su propia llamada a herramienta,
                # y el parser interno de Ollama, en vez de devolver el texto crudo
                # como fallback, corta la request entera con 500 — perdiendo toda esa
                # generación. Es un artefacto de muestreo (no determinístico: la misma
                # llamada regenerada puede salir bien), así que reintentar tiene
                # sentido acá — a diferencia de un 4xx (error real del pedido, donde
                # reintentar solo repite el mismo error).
                if e.response is not None and e.response.status_code == 500 and attempt < attempts:
                    logger.warning(
                        f"Ollama devolvió 500 (intento {attempt}/{attempts}, probable fallo de parseo del "
                        f"propio modelo al generar una llamada a herramienta), reintentando en "
                        f"{self.retry_backoff_seconds}s: {e}"
                    )
                    self._sleep(self.retry_backoff_seconds)
                    continue
                raise OllamaError(f"Ollama devolvió un error HTTP: {e}") from e

    def is_model_loaded(self) -> bool:
        """
        GET /api/ps — True si Ollama tiene AL MENOS un modelo cargado en
        RAM ahora mismo. Fail-safe: si no se puede consultar, devuelve
        False (nunca debe bloquear la eviction de otros recursos por
        esto, ver kernel/broker/resource_broker.py).
        """
        try:
            response = self._get(f"{self.base_url}/api/ps", timeout=5)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            return False
        return bool(response.json().get("models"))

    def unload_model(self) -> None:
        """
        Descarga TODOS los modelos actualmente cargados en Ollama (POST
        /api/generate con keep_alive=0, sin prompt — formato documentado
        de Ollama para forzar la descarga inmediata). Usado por
        resource_broker.evict_idle_and_pressured() bajo presión real de
        RAM, ANTES de que un pipeline local pesado (imagen/audio/STT)
        intente cargar — BUG REAL ENCONTRADO EN USO (2026-07-27): sin
        esto, un pedido de imagen podía intentar cargar SDXL-Turbo
        (13GB) con el modelo de chat de Ollama todavía residente,
        superando la RAM física total de la máquina y congelando el
        sistema entero. Nunca lanza — best-effort, mismo criterio
        fail-safe que el resto de este mecanismo.
        """
        try:
            response = self._get(f"{self.base_url}/api/ps", timeout=5)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            return
        for model in response.json().get("models", []):
            name = model.get("name")
            if not name:
                continue
            try:
                self._post(f"{self.base_url}/api/generate", json={"model": name, "keep_alive": 0}, timeout=self.timeout)
            except requests.exceptions.RequestException:
                logger.warning(f"No se pudo descargar el modelo '{name}' de Ollama")

    def list_models(self) -> list[str]:
        """Consulta GET /api/tags para listar modelos disponibles localmente."""
        try:
            response = self._get(f"{self.base_url}/api/tags", timeout=self.timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise OllamaError(f"No se pudo listar modelos de Ollama: {e}") from e
        data = response.json()
        return [m["name"] for m in data.get("models", [])]

    def is_available(self) -> bool:
        try:
            self._get(f"{self.base_url}/api/tags", timeout=5)
            return True
        except requests.exceptions.RequestException:
            return False

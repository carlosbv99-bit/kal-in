"""
Segundo LLMProvider real (agent_core/llm/provider.py) — no un stub.
Existe para validar que el contrato de F1 generaliza de verdad: este
cliente habla el formato OpenAI-compatible (respuesta envuelta en
"choices[].message", tool_calls con "arguments" siempre como string
JSON) en vez del formato nativo de Ollama (/api/chat, ver
ollama_client.py) — un wire format genuinamente distinto, no una
copia con otro nombre.

Por defecto apunta al propio endpoint OpenAI-compatible que Ollama ya
expone (`{base_url}/v1/chat/completions`, `/v1/models`) — el mismo
Ollama local que ya tenés corriendo, sin costo ni instalación nueva,
pero ejercitando un código de parseo completamente distinto. Con un
`base_url`/`api_key` apuntando a OpenAI real (o cualquier otro servicio
compatible: OpenRouter, vLLM, etc.) la misma clase sirve tal cual — no
hay nada específico de Ollama acá.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import requests

from agent_core.llm.provider import ChatResponse, ProviderError, ToolCall
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

PostFn = Callable[..., Any]
GetFn = Callable[..., Any]


class OpenAICompatibleError(ProviderError):
    """Error específico de OpenAICompatibleClient."""


def _tool_use_failed_fallback_content(exc: requests.exceptions.HTTPError) -> str | None:
    """
    BUG REAL ENCONTRADO EN USO: Groq puede rechazar con 400 y
    `code: "tool_use_failed"` cuando el modelo intenta una llamada a
    herramienta mal formada — pero el cuerpo del error YA trae, en
    `failed_generation`, la respuesta en texto plano que el modelo
    quería dar antes de ese intento roto. Sin esto, el usuario se
    quedaba sin ninguna respuesta por un intento de herramienta
    fallido, aunque el modelo sí tenía algo útil para decir. None si
    el error no tiene esta forma específica (cualquier otro 400 sigue
    siendo un error real, se propaga igual).
    """
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or error.get("code") != "tool_use_failed":
        return None
    return error.get("failed_generation") or None


def _with_stringified_tool_call_arguments(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    BUG REAL ENCONTRADO EN USO: el formato OpenAI (que Groq valida
    ESTRICTO, a diferencia de Ollama que es tolerante) exige que
    tool_calls[].function.arguments sea un STRING con JSON adentro,
    nunca el objeto ya parseado — sin esto, Groq rechazaba CUALQUIER
    turno posterior a una llamada a herramienta con 400 ("arguments:
    value must be a string"). agent_core/llm/agent_loop.py arma
    `messages` en formato CANÓNICO (arguments como dict, lo que espera
    Ollama nativo) — este cliente es el único responsable de adaptarlo
    a SU wire format antes de mandarlo, no el núcleo del loop. No muta
    `messages` in-place (mismo criterio que OllamaClient.chat() con
    `images`).
    """
    result = []
    for message in messages:
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            result.append(message)
            continue
        new_tool_calls = []
        for tc in tool_calls:
            function = tc.get("function", {})
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                tc = {**tc, "function": {**function, "arguments": json.dumps(arguments)}}
            new_tool_calls.append(tc)
        result.append({**message, "tool_calls": new_tool_calls})
    return result


def _response_detail(exc: requests.exceptions.RequestException) -> str:
    """
    `str(exc)` de un HTTPError solo trae la línea de estado ("400
    Client Error: Bad Request for url: ..."), nunca el cuerpo — que es
    justamente donde un proveedor real (Grok/xAI, OpenAI, etc.) explica
    QUÉ está mal (p.ej. "model not found", scope insuficiente en la
    key). Sin esto, cualquier error real contra un proveedor en la nube
    es indiagnosticable a ciegas. Trunca por si el cuerpo es enorme.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    body = (response.text or "").strip()
    if not body:
        return str(exc)
    return f"{exc} — respuesta: {body[:500]}"


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
        post_fn: PostFn | None = None,
        get_fn: GetFn | None = None,
    ):
        # Default: el endpoint OpenAI-compatible del propio Ollama local
        # (settings.llm.base_url ya apunta a Ollama) — ver docstring del
        # módulo. `post_fn`/`get_fn` inyectables para tests sin red real
        # (mismo patrón que AudioGenerationTool(http_post=...)).
        self.base_url = (base_url or f"{settings.llm.base_url}/v1").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or settings.llm.timeout_seconds
        self._post = post_fn or requests.post
        self._get = get_fn or requests.get

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_format: str | None = None,
        temperature: float | None = None,
    ) -> ChatResponse:
        """
        POST {base_url}/chat/completions. Misma forma de `tools` que ya
        usa AgentTool.to_ollama_schema() (function-calling estilo
        OpenAI) — eso ya era compatible; lo que cambia de verdad acá es
        cómo se PARSEA la respuesta.

        `response_format`/`temperature`: agregados para que el
        Conversation Engine (agent_core/conversation_engine.py) pueda
        usar este cliente también, sin perder el modo JSON forzado ni
        el temperature bajo que necesita una tarea de clasificación
        (ver ConversationEngineConfig.temperature). Mismo significado
        que en OllamaClient.chat(), traducido al formato OpenAI:
        "json" -> {"type": "json_object"}.
        """
        payload: dict[str, Any] = {
            "model": model or settings.llm.default_model,
            "messages": _with_stringified_tool_call_arguments(messages),
            # BUG REAL ENCONTRADO EN USO (2026-07-30): modelos "híbridos"
            # con modo de razonamiento (qwen3.5, etc.) vía este endpoint
            # devuelven el razonamiento en un campo "reasoning" separado
            # de "content" — sin esto, a veces "content" queda vacío
            # incluso para pedidos triviales. chat_template_kwargs es la
            # forma documentada (no estándar de OpenAI, pero soportada acá)
            # de pedirle al backend que no piense antes de responder.
            "chat_template_kwargs": {"enable_thinking": False},
            # Mismo motivo que OllamaClient.chat() (ver settings.llm.
            # max_response_tokens): sin tope, un bucle de repetición del
            # modelo puede correr indefinidamente en vez de fallar rápido.
            "max_tokens": settings.llm.max_response_tokens,
        }
        if tools:
            payload["tools"] = tools
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if temperature is not None:
            payload["temperature"] = temperature

        try:
            response = self._post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise OpenAICompatibleError(
                f"No se pudo conectar a {self.base_url}. ¿El servicio está corriendo?"
            ) from e
        except requests.exceptions.Timeout as e:
            raise OpenAICompatibleError(
                f"{self.base_url} no respondió en {self.timeout}s"
            ) from e
        except requests.exceptions.HTTPError as e:
            fallback_content = _tool_use_failed_fallback_content(e)
            if fallback_content is not None:
                # El contenido de failed_generation queda en el log —
                # sin esto, un intento de tool-call mal formado real
                # (qué herramienta, qué argumentos) queda invisible en
                # cuanto se usa el fallback, indiagnosticable a ciegas
                # (bug real: no se podía ver QUÉ intentaba llamar el
                # modelo la primera vez que esto se investigó).
                logger.warning(
                    f"{self.base_url} rechazó un intento de llamada a herramienta mal formado "
                    f"(tool_use_failed) — se usa la respuesta en texto plano que el modelo ya había "
                    f"generado. failed_generation: {fallback_content[:800]!r}"
                )
                return ChatResponse(content=fallback_content, tool_calls=[], raw={})
            raise OpenAICompatibleError(f"{self.base_url} devolvió un error HTTP: {_response_detail(e)}") from e

        data = response.json()
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content") or ""

        tool_calls = []
        for raw_call in message.get("tool_calls", []) or []:
            function = raw_call.get("function", {})
            arguments = function.get("arguments", {})
            # A diferencia de Ollama (a veces objeto ya parseado), el
            # formato OpenAI manda "arguments" SIEMPRE como string JSON.
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    logger.warning(f"No se pudo parsear arguments de tool_call como JSON: {arguments!r}")
                    arguments = {}
            tool_calls.append(ToolCall(name=function.get("name", ""), arguments=arguments, id=raw_call.get("id")))

        return ChatResponse(content=content, tool_calls=tool_calls, raw=data)

    def list_models(self) -> list[str]:
        """Consulta GET {base_url}/models — formato {"data": [{"id": ...}, ...]}."""
        try:
            response = self._get(f"{self.base_url}/models", headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise OpenAICompatibleError(f"No se pudo listar modelos de {self.base_url}: {_response_detail(e)}") from e
        data = response.json()
        return [m["id"] for m in data.get("data", [])]

    def is_available(self) -> bool:
        try:
            response = self._get(f"{self.base_url}/models", headers=self._headers(), timeout=5)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException:
            return False

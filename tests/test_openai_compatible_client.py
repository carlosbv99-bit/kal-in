"""
Tests de agent_core/llm/openai_compatible_client.py::OpenAICompatibleClient
— el segundo LLMProvider real que existe para validar que el contrato
de F1 (agent_core/llm/provider.py) generaliza de verdad. Sin red real:
`post_fn`/`get_fn` inyectados con respuestas guionadas (mismo patrón
que AudioGenerationTool(http_post=...) en test_audio_gen.py).

Respuesta real verificada contra Ollama corriendo de verdad (2026-07-09,
`qwen3-coder:30b` vía su endpoint /v1/chat/completions) para confirmar
que este formato ("choices"[0]."message") no es una suposición: es lo
que Ollama devuelve en la práctica.
"""
from __future__ import annotations

import pytest

from agent_core.llm.openai_compatible_client import (
    OpenAICompatibleClient,
    OpenAICompatibleError,
)
from agent_core.llm.provider import ProviderError


class FakeResponse:
    def __init__(self, json_data, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            # `response=self` es lo que hace requests de verdad — sin
            # esto, exc.response sería None y _response_detail() nunca
            # podría recuperar el cuerpo real del error.
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _client(post_fn=None, get_fn=None):
    return OpenAICompatibleClient(base_url="http://fake:1234/v1", post_fn=post_fn, get_fn=get_fn)


def test_chat_parses_content_from_choices_message():
    response = FakeResponse(
        {"choices": [{"message": {"role": "assistant", "content": "hola"}}]}
    )
    client = _client(post_fn=lambda *a, **kw: response)

    result = client.chat([{"role": "user", "content": "hola"}])

    assert result.content == "hola"
    assert result.tool_calls == []


def test_chat_parses_tool_calls_with_arguments_as_json_string():
    # Bug real que este test previene: a diferencia de Ollama (a veces
    # ya viene como dict), OpenAI-compatible SIEMPRE manda "arguments"
    # como un string JSON — si no se parsea, el dict de argumentos le
    # llegaría a AgentLoop como texto crudo en vez de kwargs utilizables.
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "run_code", "arguments": '{"code": "print(1)"}'},
                            }
                        ],
                    }
                }
            ]
        }
    )
    client = _client(post_fn=lambda *a, **kw: response)

    result = client.chat([{"role": "user", "content": "ejecutá esto"}])

    assert result.has_tool_calls is True
    assert result.tool_calls[0].name == "run_code"
    assert result.tool_calls[0].arguments == {"code": "print(1)"}
    # BUG REAL ENCONTRADO EN USO: Groq exige este 'id' para correlacionar
    # la respuesta de la herramienta con la llamada — sin propagarlo acá,
    # agent_loop.py no tenía de dónde sacarlo.
    assert result.tool_calls[0].id == "call_1"


def test_chat_stringifies_outgoing_tool_call_arguments_before_sending():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-19): agent_core/llm/agent_loop.py
    arma `messages` en formato CANÓNICO (arguments como dict — lo que
    Ollama nativo espera). Groq/OpenAI-strict, en cambio, exige que
    tool_calls[].function.arguments sea un STRING JSON — sin esta
    conversión ACÁ (no en agent_loop.py, que no debe conocer el wire
    format de un proveedor concreto), Groq rechazaba con 400 cualquier
    turno posterior a una llamada a herramienta.
    """
    import json

    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "listo"}}]})

    client = _client(post_fn=post_fn)
    messages = [
        {"role": "user", "content": "ejecutá esto"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run_code", "arguments": {"code": "print(1)"}}}],
        },
        {"role": "tool", "content": "1", "tool_call_id": "call_1"},
    ]

    client.chat(messages)

    sent_arguments = payloads[0]["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(sent_arguments, str)
    assert json.loads(sent_arguments) == {"code": "print(1)"}
    # No debe mutar la lista original del llamador.
    assert isinstance(messages[1]["tool_calls"][0]["function"]["arguments"], dict)


def test_chat_translates_json_response_format_and_temperature_to_openai_wire_format():
    """
    Agregado para que agent_core/conversation_engine.py pueda usar este
    cliente (ver ConversationEngineConfig.provider="openai_compatible")
    sin perder el modo JSON forzado ni el temperature bajo que necesita
    una tarea de clasificación — antes ninguno de los dos existía acá.
    """
    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "{}"}}]})

    client = _client(post_fn=post_fn)

    client.chat([{"role": "user", "content": "hola"}], response_format="json", temperature=0.1)

    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert payloads[0]["temperature"] == 0.1


def test_chat_omits_response_format_and_temperature_when_not_requested():
    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "hola"}}]})

    client = _client(post_fn=post_fn)

    client.chat([{"role": "user", "content": "hola"}])

    assert "response_format" not in payloads[0]
    assert "temperature" not in payloads[0]


def test_chat_always_disables_thinking_via_chat_template_kwargs():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-30): modelos "híbridos" con modo
    de razonamiento (qwen3.5, etc.) vía este endpoint devuelven el
    razonamiento en un campo "reasoning" separado de "content" — sin
    esto, "content" quedaba vacío incluso para pedidos triviales,
    confirmado en vivo contra Ollama real. chat_template_kwargs es la
    forma que el backend sí respeta acá (a diferencia de "think" a
    nivel raíz del payload, que no tiene efecto en este endpoint).
    """
    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "hola"}}]})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert payloads[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_raises_provider_error_on_connection_failure():
    import requests

    def broken_post(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(post_fn=broken_post)

    with pytest.raises(ProviderError):
        client.chat([{"role": "user", "content": "hola"}])


def test_list_models_parses_data_id_field():
    response = FakeResponse({"data": [{"id": "qwen3-coder:30b"}, {"id": "deepseek-r1:14b"}]})
    client = _client(get_fn=lambda *a, **kw: response)

    assert client.list_models() == ["qwen3-coder:30b", "deepseek-r1:14b"]


def test_is_available_true_when_get_succeeds():
    client = _client(get_fn=lambda *a, **kw: FakeResponse({"data": []}))
    assert client.is_available() is True


def test_is_available_false_when_get_fails():
    import requests

    def broken_get(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(get_fn=broken_get)
    assert client.is_available() is False


def test_openai_compatible_error_is_a_provider_error():
    assert issubclass(OpenAICompatibleError, ProviderError)


# --- Detalle real del error (bug real encontrado probando contra Grok/xAI) ---
#
# str(HTTPError) SOLO trae la línea de estado ("400 Client Error: Bad
# Request for url: ..."), nunca el cuerpo — que es justamente donde un
# proveedor real (Grok/xAI acá) explica QUÉ está mal. Confirmado en vivo:
# "Model not found: qwen3-coder:30b" (chat) y "Your newly created team
# doesn't have any credits..." (list_models) quedaban invisibles sin esto.


def test_chat_error_includes_the_real_response_body():
    response = FakeResponse(
        None, status_code=400, text='{"code":"invalid-argument","error":"Model not found: qwen3-coder:30b"}',
    )
    client = _client(post_fn=lambda *a, **kw: response)

    with pytest.raises(OpenAICompatibleError, match="Model not found: qwen3-coder:30b"):
        client.chat([{"role": "user", "content": "hola"}])


def test_list_models_error_includes_the_real_response_body():
    response = FakeResponse(
        None, status_code=403, text='{"code":"permission-denied","error":"no tenés créditos"}',
    )
    client = _client(get_fn=lambda *a, **kw: response)

    with pytest.raises(OpenAICompatibleError, match="no tenés créditos"):
        client.list_models()


# --- tool_use_failed de Groq: no tirar la respuesta a la basura ---
#
# BUG REAL ENCONTRADO EN USO: Groq rechaza con 400 cuando el modelo
# intenta una llamada a herramienta mal formada — pero el cuerpo del
# error YA trae, en "failed_generation", la respuesta en texto plano
# que el modelo quería dar. Confirmado en vivo contra el agente IDE de
# VS Code: "crea un proyecto html para un sitio web" perdía la
# respuesta entera por esto.


def test_chat_uses_the_failed_generation_as_fallback_content_on_tool_use_failed():
    response = FakeResponse(
        {
            "error": {
                "message": "Failed to call a function. Please adjust your prompt.",
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": "¡Claro! Acá tenés un ejemplo de proyecto HTML...",
            }
        },
        status_code=400,
    )
    client = _client(post_fn=lambda *a, **kw: response)

    result = client.chat([{"role": "user", "content": "hola"}], tools=[{"type": "function", "function": {}}])

    assert result.content == "¡Claro! Acá tenés un ejemplo de proyecto HTML..."
    assert result.tool_calls == []


def test_chat_still_raises_on_a_400_that_is_not_tool_use_failed():
    response = FakeResponse(
        {"error": {"message": "Model not found", "type": "invalid_request_error", "code": "model_not_found"}},
        status_code=400,
        text='{"error":{"message":"Model not found","type":"invalid_request_error","code":"model_not_found"}}',
    )
    client = _client(post_fn=lambda *a, **kw: response)

    with pytest.raises(OpenAICompatibleError, match="Model not found"):
        client.chat([{"role": "user", "content": "hola"}])

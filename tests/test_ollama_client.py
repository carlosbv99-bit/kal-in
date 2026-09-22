"""
Tests de agent_core/llm/ollama_client.py::OllamaClient — en particular el
reintento ante ConnectionError transitorio (bug real: con generación de
imagen/audio/video corriendo en la misma máquina, Ollama puede quedar
momentáneamente sin responder y romper toda la tarea con un solo
ConnectionError, aunque esté bien un segundo antes y un segundo después).
Sin red real: `post_fn`/`get_fn`/`sleep_fn` inyectados (mismo patrón que
test_openai_compatible_client.py).
"""
from __future__ import annotations

import pytest
import requests

from agent_core.llm.ollama_client import OllamaClient, OllamaError
from agent_core.llm.provider import ProviderError
from utils.config import settings


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        # `response=self` replica el comportamiento REAL de
        # requests.Response.raise_for_status() — necesario para que
        # _post_with_retry() pueda distinguir un 500 (reintentable) de
        # un 4xx (no) mirando e.response.status_code.
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}", response=self)


class FakeResourceBroker:
    """Doble de kernel/broker/resource_broker.py::ResourceBroker — solo
    cuenta llamadas, sin recursos reales ni psutil."""

    def __init__(self):
        self.evict_calls = 0

    def evict_idle_and_pressured(self):
        self.evict_calls += 1


def _client(post_fn=None, get_fn=None, sleep_fn=None, connection_retries=2, resource_broker=None):
    return OllamaClient(
        post_fn=post_fn,
        get_fn=get_fn,
        sleep_fn=sleep_fn or (lambda seconds: None),  # nunca dormir de verdad en tests
        connection_retries=connection_retries,
        resource_broker=resource_broker or FakeResourceBroker(),
    )


def test_chat_succeeds_on_first_try_without_retrying():
    calls = []

    def post_fn(*a, **kw):
        calls.append(1)
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    result = client.chat([{"role": "user", "content": "hola"}])

    assert result.content == "hola"
    assert len(calls) == 1


def test_chat_sends_format_in_payload_when_response_format_is_given():
    # Usado por agent_core/conversation_engine.py para forzar que Ollama
    # devuelva JSON válido en message.content — ver ConversationEngine.
    captured = {}

    def post_fn(url, json=None, **kw):
        captured["payload"] = json
        return FakeResponse({"message": {"content": "{}"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}], response_format="json")

    assert captured["payload"]["format"] == "json"


def test_chat_omits_format_from_payload_by_default():
    captured = {}

    def post_fn(url, json=None, **kw):
        captured["payload"] = json
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert "format" not in captured["payload"]


def test_chat_sends_temperature_in_payload_options_when_given():
    # BUG REAL ENCONTRADO EN USO (2026-07-25): agent_core/
    # conversation_engine.py necesita esto para que su clasificación sea
    # consistente entre llamadas — ver ConversationEngineConfig.temperature.
    captured = {}

    def post_fn(url, json=None, **kw):
        captured["payload"] = json
        return FakeResponse({"message": {"content": "{}"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}], temperature=0.1)

    assert captured["payload"]["options"] == {"num_predict": settings.llm.max_response_tokens, "temperature": 0.1}


def test_chat_sends_only_the_token_cap_in_options_by_default():
    # BUG REAL ENCONTRADO EN USO (2026-09-22): sin num_predict, un modelo
    # que entra en un bucle de repetición nunca para solo — ver
    # settings.llm.max_response_tokens. temperature sigue siendo opcional
    # (solo se agrega si el llamador lo pasa), pero el tope de tokens va
    # SIEMPRE, incluso sin ningún otro override.
    captured = {}

    def post_fn(url, json=None, **kw):
        captured["payload"] = json
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert captured["payload"]["options"] == {"num_predict": settings.llm.max_response_tokens}


def test_chat_parses_tool_call_id_when_present():
    # BUG REAL ENCONTRADO EN USO: Groq (a diferencia de Ollama) valida
    # ESTRICTO el formato OpenAI y exige un 'id' por tool_call para
    # correlacionar la respuesta de la herramienta con la llamada que
    # la originó — sin propagarlo acá, agent_loop.py no tenía de dónde
    # sacarlo para providers estrictos.
    response = FakeResponse(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"id": "call_abc", "function": {"name": "run_code", "arguments": {"code": "print(1)"}}}
                ],
            }
        }
    )
    client = _client(post_fn=lambda *a, **kw: response)

    result = client.chat([{"role": "user", "content": "ejecutá esto"}])

    assert result.tool_calls[0].id == "call_abc"


def test_chat_parses_tool_call_without_id_as_none():
    """Ollama no siempre manda 'id' — no debe romper el parseo."""
    response = FakeResponse(
        {"message": {"content": "", "tool_calls": [{"function": {"name": "run_code", "arguments": {}}}]}}
    )
    client = _client(post_fn=lambda *a, **kw: response)

    result = client.chat([{"role": "user", "content": "ejecutá esto"}])

    assert result.tool_calls[0].id is None


def test_chat_evicts_idle_resources_before_calling_ollama():
    """BUG REAL ENCONTRADO EN USO: sin esto, un pipeline de imagen/audio
    de varios GB se queda en RAM para siempre, compitiendo con Ollama
    por la misma RAM del sistema (confirmado: Ollama quedaba con
    "Connection refused" justo después de generar una imagen). Ver
    kernel/broker/resource_broker.py."""

    def post_fn(*a, **kw):
        return FakeResponse({"message": {"content": "hola"}})

    broker = FakeResourceBroker()
    client = _client(post_fn=post_fn, resource_broker=broker)

    client.chat([{"role": "user", "content": "hola"}])

    assert broker.evict_calls == 1


def test_chat_retries_on_transient_connection_error_then_succeeds():
    attempts = {"n": 0}

    def post_fn(*a, **kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.exceptions.ConnectionError("ollama recargando el modelo")
        return FakeResponse({"message": {"content": "ya volvió"}})

    slept = []
    client = _client(post_fn=post_fn, sleep_fn=lambda s: slept.append(s), connection_retries=2)

    result = client.chat([{"role": "user", "content": "hola"}])

    assert result.content == "ya volvió"
    assert attempts["n"] == 3  # 2 fallos + 1 éxito
    assert len(slept) == 2  # una pausa antes de cada reintento


def test_chat_raises_after_exhausting_retries_on_connection_error():
    def broken_post(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(post_fn=broken_post, connection_retries=2)

    with pytest.raises(ProviderError):
        client.chat([{"role": "user", "content": "hola"}])


def test_chat_does_not_retry_on_timeout():
    """Un timeout es una generación lenta, no una desconexión — reintentar
    solo duplicaría la espera de algo que ya sabemos que tarda."""
    calls = []

    def slow_post(*a, **kw):
        calls.append(1)
        raise requests.exceptions.Timeout("tardó demasiado")

    client = _client(post_fn=slow_post, connection_retries=2)

    with pytest.raises(OllamaError):
        client.chat([{"role": "user", "content": "hola"}])
    assert len(calls) == 1


def test_chat_does_not_retry_on_a_4xx_http_error():
    """Un 4xx es un error real del pedido en sí — reintentar solo repetiría
    el mismo error, nunca se resuelve solo."""
    calls = []

    def bad_request_post(*a, **kw):
        calls.append(1)
        return FakeResponse({}, status_code=400)

    client = _client(post_fn=bad_request_post, connection_retries=2)

    with pytest.raises(OllamaError):
        client.chat([{"role": "user", "content": "hola"}])
    assert len(calls) == 1


def test_chat_retries_on_a_500_http_error():
    """
    BUG REAL ENCONTRADO EN USO (2026-08-23): "crea un proyecto de agenda
    para android" (propose_project_files con 12 archivos) tiró 500 tras
    48s de generación — confirmado en el propio log de Ollama
    (journalctl -u ollama): "qwen3.5 tool call parsing failed" / "XML
    syntax error... element <function> closed by </parameter>". El
    MODELO generó XML mal formado al describir su propia llamada a
    herramienta; el parser interno de Ollama corta la request entera
    con 500 en vez de devolver el texto crudo. Es un artefacto de
    muestreo (no determinístico) — reintentar tiene sentido, a
    diferencia de un 4xx real.
    """
    calls = []

    def flaky_post(*a, **kw):
        calls.append(1)
        return FakeResponse({}, status_code=500)

    client = _client(post_fn=flaky_post, connection_retries=2)

    with pytest.raises(OllamaError):
        client.chat([{"role": "user", "content": "hola"}])
    assert len(calls) == 3  # intento original + 2 reintentos, todos 500


def test_chat_succeeds_after_a_transient_500_http_error():
    """El mismo pedido regenerado por el modelo puede salir bien la
    segunda vez — el reintento debe devolver esa respuesta real, no
    seguir fallando solo porque el PRIMER intento falló."""
    responses = [FakeResponse({}, status_code=500), FakeResponse({"message": {"content": "listo"}})]

    def flaky_then_ok_post(*a, **kw):
        return responses.pop(0)

    client = _client(post_fn=flaky_then_ok_post, connection_retries=2)

    result = client.chat([{"role": "user", "content": "hola"}])

    assert result.content == "listo"


def test_list_models_uses_injected_get_fn():
    client = _client(get_fn=lambda *a, **kw: FakeResponse({"models": [{"name": "qwen3-coder:30b"}]}))
    assert client.list_models() == ["qwen3-coder:30b"]


def test_is_available_false_on_connection_error():
    def broken_get(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(get_fn=broken_get)
    assert client.is_available() is False


def test_chat_attaches_images_to_last_message():
    """Formato de /api/chat de Ollama para modelos de visión
    (llama3.2-vision): 'images' es una lista de base64 en el mensaje,
    no un campo separado del payload."""
    payloads = []

    def post_fn(url, json, **kw):
        payloads.append(json)
        return FakeResponse({"message": {"content": "una foto de un gato"}})

    client = _client(post_fn=post_fn)
    client.chat(
        [{"role": "user", "content": "¿qué hay en esta imagen?"}],
        model="llama3.2-vision:latest",
        images=["ZmFrZWJhc2U2NA=="],
    )

    assert payloads[0]["messages"][-1]["images"] == ["ZmFrZWJhc2U2NA=="]
    assert payloads[0]["messages"][-1]["content"] == "¿qué hay en esta imagen?"


def test_chat_without_images_does_not_add_the_key():
    payloads = []

    def post_fn(url, json, **kw):
        payloads.append(json)
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert "images" not in payloads[0]["messages"][-1]


def test_chat_always_disables_ollama_thinking_mode():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-30): modelos "híbridos" con modo
    de razonamiento (qwen3.5, etc.) a veces gastan TODA la generación en
    un campo "thinking" separado y dejan message.content completamente
    vacío, incluso para pedidos triviales — confirmado en vivo contra
    Ollama real: con think=false, el mismo pedido responde de inmediato
    con contenido real. Ollama ignora este campo sin error en modelos
    que no lo soportan (confirmado con qwen2.5:3b).
    """
    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert payloads[0]["think"] is False


def test_chat_sends_a_keep_alive_longer_than_the_resource_broker_own_timeout():
    """
    Diagnóstico de lentitud (2026-08-23): sin esto, Ollama usa su propio
    default de systemd (5m) — un segundo temporizador independiente del
    resource_broker de kal que puede descargar el modelo ANTES de que
    el timeout propio (más largo) del broker llegue a aplicarse. Se
    manda el doble a propósito, para que el resource_broker de kal
    quede como la autoridad real (ver kernel/broker/resource_broker.py).
    """
    payloads = []

    def post_fn(url, json=None, **kw):
        payloads.append(json)
        return FakeResponse({"message": {"content": "hola"}})

    client = _client(post_fn=post_fn)
    client.chat([{"role": "user", "content": "hola"}])

    assert payloads[0]["keep_alive"] == settings.resource_broker.ollama_idle_timeout_seconds * 2


def test_chat_with_images_does_not_mutate_caller_messages():
    original_messages = [{"role": "user", "content": "hola"}]
    client = _client(post_fn=lambda *a, **kw: FakeResponse({"message": {"content": "ok"}}))

    client.chat(original_messages, images=["abc"])

    assert "images" not in original_messages[0]


# --- is_model_loaded()/unload_model() ---
# BUG REAL ENCONTRADO EN USO (2026-07-27): un pedido de imagen congeló
# la máquina del usuario — SDXL-Turbo (13GB fp32) intentó cargar con el
# modelo de chat de Ollama todavía residente en RAM, superando la RAM
# física total. Estos dos métodos permiten que resource_broker libere
# el modelo de Ollama ANTES de que un pipeline local pesado cargue.


def test_is_model_loaded_true_when_ps_lists_a_model():
    client = _client(get_fn=lambda *a, **kw: FakeResponse({"models": [{"name": "qwen2.5-coder:14b"}]}))
    assert client.is_model_loaded() is True


def test_is_model_loaded_false_when_ps_is_empty():
    client = _client(get_fn=lambda *a, **kw: FakeResponse({"models": []}))
    assert client.is_model_loaded() is False


def test_is_model_loaded_false_on_connection_error():
    def broken_get(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(get_fn=broken_get)
    assert client.is_model_loaded() is False


def test_unload_model_requests_keep_alive_zero_for_each_loaded_model():
    posted = []

    def post_fn(url, json=None, **kw):
        posted.append((url, json))
        return FakeResponse({})

    client = _client(
        get_fn=lambda *a, **kw: FakeResponse(
            {"models": [{"name": "qwen2.5-coder:14b"}, {"name": "qwen2.5:3b"}]}
        ),
        post_fn=post_fn,
    )

    client.unload_model()

    assert len(posted) == 2
    urls = {url for url, _ in posted}
    assert urls == {"http://localhost:11434/api/generate"}
    payloads = {p["model"]: p["keep_alive"] for _, p in posted}
    assert payloads == {"qwen2.5-coder:14b": 0, "qwen2.5:3b": 0}


def test_unload_model_is_a_no_op_when_nothing_is_loaded():
    posted = []
    client = _client(
        get_fn=lambda *a, **kw: FakeResponse({"models": []}),
        post_fn=lambda *a, **kw: posted.append(1),
    )

    client.unload_model()

    assert posted == []


def test_unload_model_never_raises_when_ollama_is_unreachable():
    def broken_get(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(get_fn=broken_get)

    client.unload_model()  # no debe lanzar


def test_unload_model_never_raises_if_one_unload_request_fails():
    def broken_post(*a, **kw):
        raise requests.exceptions.ConnectionError("no conecta")

    client = _client(
        get_fn=lambda *a, **kw: FakeResponse({"models": [{"name": "qwen2.5-coder:14b"}]}),
        post_fn=broken_post,
    )

    client.unload_model()  # no debe lanzar, solo loguea

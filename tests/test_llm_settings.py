"""
Tests de agent_core/llm_settings.py — configuración en caliente del
LLM activo (local u en la nube), persistida a config.yaml + .env sin
destruir comentarios (reemplazo de texto dirigido, nunca yaml.dump()).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from agent_core import llm_settings
from agent_core.llm.provider import ProviderError
from agent_core.llm_settings import (
    LLMSettingsError,
    activate_cloud_profile,
    get_llm_settings,
    get_ollama_model_capabilities,
    list_cloud_profiles,
    list_local_ollama_models,
    list_model_sources,
    pull_ollama_model,
    save_cloud_profile,
    update_llm_settings,
)
from utils.config import settings

_FAKE_CONFIG_YAML = """schema_version: 1

llm:
  # comentario real que NO debe borrarse
  provider: "ollama"
  base_url: "http://localhost:11434"
  # otro comentario, con un ejemplo comentado que NO debe matchear:
  #     base_url: "https://api.x.ai/v1"
  #     default_model: "grok-3"
  default_model: "qwen3-coder:30b"
"""


class _NoModelsClient:
    """Doble por default de OpenAICompatibleClient para TODOS los tests
    de este archivo — evita que update_llm_settings()/activate_cloud_profile()
    disparen una llamada de red REAL (ver _first_chat_capable_model())
    solo por cambiar de base_url en un test que no le interesa esto.
    Sin modelos => _first_chat_capable_model() devuelve None rápido,
    sin tocar la red. Los tests que sí quieren probar la elección
    automática de modelo monkeypatchean OpenAICompatibleClient de nuevo
    con una lista real."""

    def __init__(self, base_url, api_key):
        pass

    def list_models(self):
        return []


@pytest.fixture(autouse=True)
def _fake_paths(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_FAKE_CONFIG_YAML, encoding="utf-8")
    env_example_path = tmp_path / ".env.example"
    env_example_path.write_text("AGENT_ENV=development\nLLM_API_KEY=\n", encoding="utf-8")
    env_path = tmp_path / ".env"

    monkeypatch.setattr(llm_settings, "_CONFIG_PATH", config_path)
    monkeypatch.setattr(llm_settings, "_ENV_PATH", env_path)
    monkeypatch.setattr(llm_settings, "_ENV_EXAMPLE_PATH", env_example_path)
    monkeypatch.setattr(llm_settings, "_CLOUD_PROFILES_PATH", tmp_path / "cloud_profiles.json")
    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", _NoModelsClient)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # BUG REAL ENCONTRADO EN USO: settings.llm es el singleton global de
    # verdad, cargado UNA vez del config.yaml REAL del proyecto al
    # importar utils.config — solo restaurar "lo que hubiera antes" no
    # alcanza para aislar los tests: si el config.yaml real quedó en un
    # estado raro (p.ej. por probar la API a mano en la misma sesión),
    # ese estado corrupto se filtraba como línea de base de los tests.
    # Por eso además de restaurar al final, se fuerza un estado conocido
    # ANTES de cada test (el mismo que describe _FAKE_CONFIG_YAML).
    original = (settings.llm.provider, settings.llm.base_url, settings.llm.default_model)
    settings.llm.provider = "ollama"
    settings.llm.base_url = "http://localhost:11434"
    settings.llm.default_model = "qwen3-coder:30b"
    yield config_path, env_path
    settings.llm.provider, settings.llm.base_url, settings.llm.default_model = original


def test_get_llm_settings_never_exposes_the_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "secreto-real")
    result = get_llm_settings()
    assert result["has_api_key"] is True
    assert "secreto-real" not in str(result)
    assert "api_key" not in result


def test_read_llm_env_var_prefers_the_env_file_over_a_stale_os_environ(_fake_paths, monkeypatch):
    # BUG REAL ENCONTRADO EN USO: load_dotenv() nunca sobreescribe una
    # variable ya seteada en el proceso — si os.environ quedó con un
    # valor viejo (p.ej. de una prueba anterior o un guardado a medias),
    # ningún --reload posterior lo corregía aunque el .env en disco
    # tuviera el valor correcto y más nuevo. Confirmado en vivo con una
    # key de Groq real rechazada con 401 mientras el .env ya tenía la
    # key correcta.
    _, env_path = _fake_paths
    monkeypatch.setenv("LLM_API_KEY", "valor-viejo-en-memoria")
    env_path.write_text("LLM_API_KEY=valor-correcto-en-disco\n", encoding="utf-8")

    assert llm_settings.read_llm_env_var("LLM_API_KEY") == "valor-correcto-en-disco"


def test_read_llm_env_var_falls_back_to_os_environ_when_key_missing_from_env_file(
    _fake_paths, monkeypatch
):
    monkeypatch.setenv("LLM_API_KEY", "solo-en-el-proceso")
    # El archivo .env existe pero no menciona esta clave en absoluto.
    _fake_paths[1].write_text("AGENT_ENV=development\n", encoding="utf-8")

    assert llm_settings.read_llm_env_var("LLM_API_KEY") == "solo-en-el-proceso"


def test_update_rejects_cloud_provider_without_base_url():
    with pytest.raises(LLMSettingsError, match="base_url"):
        update_llm_settings(provider="openai_compatible", api_key="sk-123")


def test_update_rejects_cloud_provider_without_any_api_key():
    with pytest.raises(LLMSettingsError, match="api_key"):
        update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1")


def test_update_persists_provider_base_url_and_model_to_yaml(_fake_paths):
    config_path, _ = _fake_paths
    update_llm_settings(
        provider="openai_compatible", base_url="https://api.x.ai/v1",
        default_model="grok-3", api_key="sk-123",
    )

    text = config_path.read_text(encoding="utf-8")
    assert 'provider: "openai_compatible"' in text
    assert 'base_url: "https://api.x.ai/v1"' in text
    assert 'default_model: "grok-3"' in text
    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.base_url == "https://api.x.ai/v1"
    assert settings.llm.default_model == "grok-3"


def test_update_preserves_comments_and_commented_out_examples(_fake_paths):
    config_path, _ = _fake_paths
    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1", api_key="sk-123")

    text = config_path.read_text(encoding="utf-8")
    assert "# comentario real que NO debe borrarse" in text
    assert '#     base_url: "https://api.x.ai/v1"' in text  # el ejemplo comentado sigue intacto


def test_update_persists_api_key_to_env_creating_it_from_example(_fake_paths):
    _, env_path = _fake_paths
    assert not env_path.exists()

    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1", api_key="sk-nueva")

    assert env_path.exists()
    text = env_path.read_text(encoding="utf-8")
    assert "LLM_API_KEY=sk-nueva" in text
    assert "AGENT_ENV=development" in text  # vino de .env.example, no se perdió


def test_update_replaces_existing_api_key_in_env_without_duplicating(_fake_paths):
    _, env_path = _fake_paths
    env_path.write_text("AGENT_ENV=development\nLLM_API_KEY=vieja\n", encoding="utf-8")

    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1", api_key="nueva")

    text = env_path.read_text(encoding="utf-8")
    assert text.count("LLM_API_KEY=") == 1
    assert "LLM_API_KEY=nueva" in text


def test_update_without_api_key_keeps_the_existing_one(_fake_paths, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "ya-guardada")

    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1")  # sin api_key

    import os
    assert os.environ["LLM_API_KEY"] == "ya-guardada"


def test_switching_back_to_ollama_does_not_require_base_url_or_api_key(_fake_paths):
    update_llm_settings(provider="ollama")  # no debe levantar nada
    assert settings.llm.provider == "ollama"


def test_switching_back_to_ollama_resets_base_url_to_the_local_default(_fake_paths, monkeypatch):
    """
    Bug real encontrado en uso: tras activar un proveedor en la nube,
    volver a "ollama" sin pasar base_url dejaba la URL vieja (la del
    proveedor en la nube) — OllamaClient terminaba pegándole al host
    equivocado, sin ninguna forma de recuperarse desde la interfaz.
    """
    monkeypatch.setenv("LLM_API_KEY", "algo")
    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1")
    assert settings.llm.base_url == "https://api.x.ai/v1"

    update_llm_settings(provider="ollama")  # sin base_url explícito

    assert settings.llm.base_url == "http://localhost:11434"


def test_switching_back_to_ollama_respects_an_explicit_custom_base_url(_fake_paths, monkeypatch):
    """Si alguien corre Ollama en un puerto no estándar, un base_url
    explícito no debe pisarse con el default."""
    monkeypatch.setenv("LLM_API_KEY", "algo")
    update_llm_settings(provider="openai_compatible", base_url="https://api.x.ai/v1")

    update_llm_settings(provider="ollama", base_url="http://localhost:12345")

    assert settings.llm.base_url == "http://localhost:12345"


def test_update_rejects_an_ollama_default_model_without_tool_support(_fake_paths, monkeypatch):
    """
    BUG REAL ENCONTRADO EN USO: activar llava:13b (un modelo de solo
    visión, sin tool-calling) como default_model rompía CUALQUIER
    mensaje posterior con 400 ("does not support tools"), hasta un
    simple "hola" — validado ANTES de escribir nada, como ya promete el
    docstring de esta función.
    """
    monkeypatch.setattr(llm_settings, "_ollama_model_supports_tools", lambda name: False)

    with pytest.raises(LLMSettingsError, match="no soporta llamadas a herramientas"):
        update_llm_settings(provider="ollama", default_model="llava:13b")

    assert settings.llm.default_model == "qwen3-coder:30b"  # el de _FAKE_CONFIG_YAML, sin tocar


def test_update_accepts_an_ollama_default_model_with_tool_support(_fake_paths, monkeypatch):
    monkeypatch.setattr(llm_settings, "_ollama_model_supports_tools", lambda name: True)
    monkeypatch.setattr(llm_settings, "_ensure_enough_ram_to_load", lambda name: None)

    update_llm_settings(provider="ollama", default_model="qwen2.5-coder:14b")

    assert settings.llm.default_model == "qwen2.5-coder:14b"


def test_switching_to_a_new_cloud_endpoint_without_an_explicit_model_picks_one_automatically(
    _fake_paths, monkeypatch
):
    """
    BUG REAL ENCONTRADO EN USO: default_model es una perilla GLOBAL, no
    por proveedor — un nombre de modelo de Ollama (p.ej.
    "deepseek-r1:14b") se quedaba pegado ahí después de activar un
    proveedor en la nube distinto. El selector web siempre manda un
    'model' explícito así que no lo sufre, pero el agente IDE de VS
    Code no tiene selector propio — dependía de este default y rompía
    con 404 "model not found" contra el proveedor nuevo.
    """

    class FakeClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            return ["llama-3.3-70b-versatile", "whisper-large-v3"]

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FakeClient)

    update_llm_settings(provider="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")

    assert settings.llm.default_model == "llama-3.3-70b-versatile"  # nunca whisper (no es de chat)


def test_switching_to_the_same_cloud_endpoint_again_does_not_touch_the_current_model(
    _fake_paths, monkeypatch
):
    """Re-guardar el mismo proveedor (p.ej. solo actualizar la API key)
    no debe pisar un default_model ya elegido a mano, solo un cambio
    de endpoint real dispara la elección automática."""

    class FakeClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            return ["deberia-no-usarse"]

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FakeClient)

    update_llm_settings(provider="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")
    update_llm_settings(
        provider="openai_compatible", base_url="https://api.groq.com/openai/v1",
        default_model="elegido-a-mano", api_key="gsk_real",
    )

    update_llm_settings(provider="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_otra")

    assert settings.llm.default_model == "elegido-a-mano"


def test_switching_cloud_endpoint_with_an_explicit_model_respects_it(_fake_paths, monkeypatch):
    class FakeClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            return ["no-deberia-elegirse-este"]

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FakeClient)

    update_llm_settings(
        provider="openai_compatible", base_url="https://api.groq.com/openai/v1",
        default_model="elegido-a-mano", api_key="gsk_real",
    )

    assert settings.llm.default_model == "elegido-a-mano"


def test_switching_cloud_endpoint_when_the_provider_does_not_respond_keeps_the_previous_model(
    _fake_paths, monkeypatch
):
    class BrokenClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            raise ProviderError("no responde")

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", BrokenClient)

    update_llm_settings(provider="openai_compatible", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")

    assert settings.llm.default_model == "qwen3-coder:30b"  # el de _FAKE_CONFIG_YAML, sin tocar


# --- list_local_ollama_models() / pull_ollama_model() ---
# Independientes del proveedor ACTIVO (settings.llm.base_url podría
# apuntar a un proveedor en la nube) — siempre hablan con el Ollama
# local fijo, nunca con lo que esté configurado como proveedor activo.


def _fake_response(json_data=None, status_ok=True):
    def _raise():
        if not status_ok:
            raise requests.exceptions.HTTPError("boom")
    return SimpleNamespace(raise_for_status=_raise, json=lambda: json_data or {})


def test_list_local_ollama_models_parses_the_real_ollama_tags_shape(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "get",
        lambda url, timeout: _fake_response({"models": [{"name": "qwen3-coder:30b"}, {"name": "llava:7b"}]}),
    )
    assert list_local_ollama_models() == ["qwen3-coder:30b", "llava:7b"]


def test_list_local_ollama_models_raises_a_clear_error_when_ollama_is_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("no route")
    monkeypatch.setattr(llm_settings.requests, "get", _raise)

    with pytest.raises(LLMSettingsError, match="No se pudo conectar a Ollama"):
        list_local_ollama_models()


def test_pull_ollama_model_sends_the_correct_request(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return _fake_response({"status": "success"})

    monkeypatch.setattr(llm_settings.requests, "post", fake_post)
    pull_ollama_model("qwen2.5-coder:14b")

    assert calls[0][0] == "http://localhost:11434/api/pull"
    assert calls[0][1] == {"name": "qwen2.5-coder:14b", "stream": False}


def test_pull_ollama_model_raises_a_clear_error_on_failure(monkeypatch):
    def _raise(*a, **k):
        raise requests.exceptions.Timeout("too slow")
    monkeypatch.setattr(llm_settings.requests, "post", _raise)

    with pytest.raises(LLMSettingsError, match="No se pudo descargar"):
        pull_ollama_model("qwen2.5-coder:14b")


# --- Perfiles de proveedores en la nube (guardar varios a la vez) ---
#
# BUG REAL ENCONTRADO EN USO: "en el selector de modelo activo... deben
# mostrarse todos los modelos en la nube correctamente activados" — kal
# solo recordaba UN proveedor en la nube a la vez. Estos perfiles dejan
# guardar varios (cada uno con su propia key, en su propia variable de
# entorno) y el selector los junta a todos los que respondan de verdad.


def test_save_cloud_profile_persists_name_base_url_and_its_own_key_env():
    save_cloud_profile("Grok (xAI)", base_url="https://api.x.ai/v1", api_key="xai-123")

    profiles = list_cloud_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "Grok (xAI)"
    assert profiles[0]["base_url"] == "https://api.x.ai/v1"
    assert profiles[0]["api_key_env"] == "LLM_API_KEY_GROK_XAI"
    import os
    assert os.environ["LLM_API_KEY_GROK_XAI"] == "xai-123"


def test_save_cloud_profile_updates_existing_instead_of_duplicating():
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_old")
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_new")

    profiles = list_cloud_profiles()
    assert len(profiles) == 1
    import os
    assert os.environ["LLM_API_KEY_GROQ"] == "gsk_new"


def test_save_cloud_profile_keeps_different_profiles_with_their_own_keys():
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")
    save_cloud_profile("grok", base_url="https://api.x.ai/v1", api_key="xai-real")

    profiles = {p["name"]: p for p in list_cloud_profiles()}
    assert set(profiles) == {"groq", "grok"}
    import os
    assert os.environ["LLM_API_KEY_GROQ"] == "gsk_real"
    assert os.environ["LLM_API_KEY_GROK"] == "xai-real"


def test_activate_cloud_profile_switches_the_active_provider():
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")

    activate_cloud_profile("groq")

    assert settings.llm.provider == "openai_compatible"
    assert settings.llm.base_url == "https://api.groq.com/openai/v1"
    import os
    assert os.environ["LLM_API_KEY"] == "gsk_real"


def test_activate_cloud_profile_raises_a_clear_error_for_an_unknown_profile():
    with pytest.raises(LLMSettingsError, match="No existe un perfil"):
        activate_cloud_profile("no-existe")


def test_update_llm_settings_with_profile_name_also_saves_it_as_reusable():
    update_llm_settings(
        provider="openai_compatible", base_url="https://api.groq.com/openai/v1",
        api_key="gsk_real", profile_name="groq",
    )

    profiles = list_cloud_profiles()
    assert len(profiles) == 1
    assert profiles[0]["name"] == "groq"


def test_list_model_sources_includes_ollama_and_working_cloud_profiles(monkeypatch):
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", lambda: ["qwen3-coder:30b"])
    monkeypatch.setattr(llm_settings, "_ollama_model_supports_tools", lambda name: True)
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")

    class FakeClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            return ["llama-3.3-70b-versatile"]

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FakeClient)

    sources = list_model_sources()

    assert {"name": "ollama", "label": "Local (Ollama)", "models": ["qwen3-coder:30b"]} in sources
    assert {"name": "groq", "label": "groq", "models": ["llama-3.3-70b-versatile"]} in sources


def test_list_model_sources_never_includes_a_profile_that_fails(monkeypatch):
    """Confirmación real de "correctamente activados": un perfil guardado
    pero roto (key inválida, sin crédito) simplemente no aparece."""
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", list)
    save_cloud_profile("grok", base_url="https://api.x.ai/v1", api_key="bad-key")

    class FailingClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            raise ProviderError("Incorrect API key")

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FailingClient)

    sources = list_model_sources()

    assert all(s["name"] != "grok" for s in sources)


def test_list_model_sources_skips_a_profile_with_no_key_in_the_environment(monkeypatch):
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", list)
    # api_key="" a propósito: save_cloud_profile() solo escribe la key
    # (al .env y a os.environ) si es verdadera — así el perfil queda
    # guardado (nombre/base_url) pero genuinamente sin ninguna key en
    # ningún lado, el escenario real que este test quiere cubrir.
    save_cloud_profile("openai", base_url="https://api.openai.com/v1", api_key="")

    sources = list_model_sources()

    assert all(s["name"] != "openai" for s in sources)


# --- Filtro de "listo para usar" (no chat / Ollama :cloud sin sesión) ---
#
# BUG REAL ENCONTRADO EN USO: GET /v1/models de un proveedor real (Groq)
# trae TODOS sus modelos hospedados, no solo los de chat — whisper
# (habla-a-texto), prompt-guard/safeguard (clasificadores de
# seguridad), orpheus (texto-a-voz) aparecían mezclados con los
# modelos de chat de verdad en el selector.


def test_list_model_sources_filters_out_known_non_chat_models(monkeypatch):
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", list)
    save_cloud_profile("groq", base_url="https://api.groq.com/openai/v1", api_key="gsk_real")

    class FakeClient:
        def __init__(self, base_url, api_key):
            pass

        def list_models(self):
            return [
                "llama-3.3-70b-versatile",
                "whisper-large-v3",
                "meta-llama/llama-prompt-guard-2-86m",
                "openai/gpt-oss-safeguard-20b",
                "canopylabs/orpheus-v1-english",
                "llama-3.1-8b-instant",
            ]

    monkeypatch.setattr(llm_settings, "OpenAICompatibleClient", FakeClient)

    sources = list_model_sources()

    groq_models = next(s["models"] for s in sources if s["name"] == "groq")
    assert groq_models == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def test_list_model_sources_excludes_ollama_cloud_models(monkeypatch):
    """
    Un modelo Ollama con sufijo ":cloud" (p.ej. "glm-5.1:cloud") es en
    realidad un proxy al servicio en la nube DE OLLAMA MISMO, que
    necesita su propia sesión ('ollama signin') — sin ella, devuelve
    401 al primer uso (confirmado en vivo). No está "listo para usar"
    solo porque `ollama list` lo muestre como instalado.
    """
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", lambda: ["qwen3-coder:30b", "glm-5.1:cloud"])
    monkeypatch.setattr(llm_settings, "_ollama_model_supports_tools", lambda name: True)

    sources = list_model_sources()

    ollama_models = next(s["models"] for s in sources if s["name"] == "ollama")
    assert ollama_models == ["qwen3-coder:30b"]


# --- Filtro de tool-calling para modelos Ollama locales ---
#
# BUG REAL ENCONTRADO EN USO: el selector dejaba elegir llava:13b (sin
# soporte de tools) como default_model — Ollama rechazaba con 400
# ("does not support tools") CUALQUIER mensaje, hasta un simple "hola",
# rompiendo el chat entero apenas se lo seleccionaba.


def test_get_ollama_model_capabilities_returns_the_real_list(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "post",
        lambda url, json=None, timeout=None: _fake_response({"capabilities": ["completion", "vision"]}),
    )

    assert get_ollama_model_capabilities("llava:13b") == ["completion", "vision"]


def test_get_ollama_model_capabilities_returns_empty_list_when_ollama_is_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(llm_settings.requests, "post", _raise)

    assert get_ollama_model_capabilities("qwen2.5-coder:14b") == []


def test_ollama_model_supports_tools_reads_capabilities_from_api_show(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        assert json == {"name": "llava:13b"}
        return _fake_response({"capabilities": ["completion", "vision"]})

    monkeypatch.setattr(llm_settings.requests, "post", fake_post)

    assert llm_settings._ollama_model_supports_tools("llava:13b") is False


def test_ollama_model_supports_tools_true_when_tools_in_capabilities(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "post",
        lambda url, json=None, timeout=None: _fake_response({"capabilities": ["completion", "tools"]}),
    )

    assert llm_settings._ollama_model_supports_tools("qwen2.5-coder:14b") is True


def test_ollama_model_supports_tools_fails_closed_when_ollama_is_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(llm_settings.requests, "post", _raise)

    assert llm_settings._ollama_model_supports_tools("qwen2.5-coder:14b") is False


# --- Chequeo de RAM antes de cargar un modelo Ollama nuevo ---
#
# BUG REAL ENCONTRADO EN USO (2026-08-28): el usuario seleccionó
# deepseek-r1:14b (15.1GB) desde la pestaña Modelo mientras Ollama ya
# tenía otros modelos cargados — la máquina (14GB de RAM total) se
# quedó sin memoria real, GNOME mató procesos y VS Code se cayó. Pedido
# explícito: "si la memoria está siendo usada y no hay capacidad de
# cargar otro modelo, aunque el usuario lo pida, kal no debe cargarlo".


def test_ensure_enough_ram_skips_the_check_when_the_model_is_already_loaded(monkeypatch):
    """Cambiar a un modelo YA cargado no pide RAM nueva — no debe
    siquiera consultar su tamaño."""
    monkeypatch.setattr(llm_settings, "_ollama_loaded_model_names", lambda: {"qwen3.5:4b"})

    def _explode(name):
        raise AssertionError("no debería consultar el tamaño de un modelo ya cargado")

    monkeypatch.setattr(llm_settings, "_ollama_model_size_mb", _explode)

    llm_settings._ensure_enough_ram_to_load("qwen3.5:4b")  # no debe levantar nada


def test_ensure_enough_ram_rejects_a_model_that_would_exceed_available_ram(monkeypatch):
    monkeypatch.setattr(llm_settings, "_ollama_loaded_model_names", lambda: set())
    monkeypatch.setattr(llm_settings, "_ollama_model_size_mb", lambda name: 15_000.0)  # ~deepseek-r1:14b
    monkeypatch.setattr(llm_settings, "_available_ram_mb", lambda: 3_000.0)
    monkeypatch.setattr(settings.resource_broker, "min_available_ram_mb", 2048)

    with pytest.raises(LLMSettingsError, match="No hay RAM suficiente"):
        llm_settings._ensure_enough_ram_to_load("deepseek-r1:14b")


def test_ensure_enough_ram_accepts_a_model_that_fits_with_headroom_to_spare(monkeypatch):
    monkeypatch.setattr(llm_settings, "_ollama_loaded_model_names", lambda: set())
    monkeypatch.setattr(llm_settings, "_ollama_model_size_mb", lambda name: 4_000.0)  # ~qwen3.5:4b
    monkeypatch.setattr(llm_settings, "_available_ram_mb", lambda: 10_000.0)
    monkeypatch.setattr(settings.resource_broker, "min_available_ram_mb", 2048)

    llm_settings._ensure_enough_ram_to_load("qwen3.5:4b")  # no debe levantar nada


def test_ensure_enough_ram_fails_closed_when_the_model_size_cannot_be_determined(monkeypatch):
    """Mismo criterio que _ollama_model_supports_tools: un modelo que no
    se puede confirmar se rechaza, nunca se carga a ciegas."""
    monkeypatch.setattr(llm_settings, "_ollama_loaded_model_names", lambda: set())
    monkeypatch.setattr(llm_settings, "_ollama_model_size_mb", lambda name: None)

    with pytest.raises(LLMSettingsError, match="No se pudo determinar el tamaño"):
        llm_settings._ensure_enough_ram_to_load("un-modelo-que-no-existe")


def test_ollama_model_size_mb_reads_the_size_field_from_api_tags(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "get",
        lambda url, timeout: _fake_response(
            {"models": [{"name": "qwen3.5:4b", "size": 4_294_967_296}, {"name": "otro", "size": 1}]}
        ),
    )

    assert llm_settings._ollama_model_size_mb("qwen3.5:4b") == 4096.0


def test_ollama_model_size_mb_returns_none_for_an_unknown_model(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "get",
        lambda url, timeout: _fake_response({"models": [{"name": "otro", "size": 1}]}),
    )

    assert llm_settings._ollama_model_size_mb("no-esta-en-la-lista") is None


def test_ollama_loaded_model_names_reads_from_api_ps(monkeypatch):
    monkeypatch.setattr(
        llm_settings.requests, "get",
        lambda url, timeout: _fake_response({"models": [{"name": "qwen3.5:4b"}, {"name": "qwen2.5:3b"}]}),
    )

    assert llm_settings._ollama_loaded_model_names() == {"qwen3.5:4b", "qwen2.5:3b"}


def test_ollama_loaded_model_names_fails_closed_to_empty_set_when_unreachable(monkeypatch):
    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(llm_settings.requests, "get", _raise)

    assert llm_settings._ollama_loaded_model_names() == set()


def test_update_llm_settings_rejects_a_model_switch_without_enough_ram(_fake_paths, monkeypatch):
    """
    Reproduce el incidente real: provider=ollama + default_model de un
    modelo grande, sin RAM suficiente — update_llm_settings() debe
    rechazarlo ANTES de escribir nada a disco, mismo criterio que el
    chequeo de tool-calling.
    """
    monkeypatch.setattr(llm_settings, "_ollama_model_supports_tools", lambda name: True)
    monkeypatch.setattr(llm_settings, "_ollama_loaded_model_names", lambda: set())
    monkeypatch.setattr(llm_settings, "_ollama_model_size_mb", lambda name: 15_000.0)
    monkeypatch.setattr(llm_settings, "_available_ram_mb", lambda: 3_000.0)
    monkeypatch.setattr(settings.resource_broker, "min_available_ram_mb", 2048)

    with pytest.raises(LLMSettingsError, match="No hay RAM suficiente"):
        update_llm_settings(provider="ollama", default_model="deepseek-r1:14b")

    assert settings.llm.default_model == "qwen3-coder:30b"  # el de _FAKE_CONFIG_YAML, sin tocar


def test_list_model_sources_excludes_local_ollama_models_without_tool_support(monkeypatch):
    monkeypatch.setattr(llm_settings, "list_local_ollama_models", lambda: ["qwen2.5-coder:14b", "llava:13b"])
    monkeypatch.setattr(
        llm_settings, "_ollama_model_supports_tools",
        lambda name: name == "qwen2.5-coder:14b",
    )

    sources = list_model_sources()

    ollama_models = next(s["models"] for s in sources if s["name"] == "ollama")
    assert ollama_models == ["qwen2.5-coder:14b"]

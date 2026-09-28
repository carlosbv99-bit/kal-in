"""
Tests de tool_integration/adapters/image_gen.py.

Se saltan si diffusers no está instalado. La generación real con
sd-turbo en CPU puede tardar bastante (segundos a minutos según
hardware) — no es instantáneo, no es un bug si el test tarda.
"""
from __future__ import annotations

import base64

import pytest
import requests

pytest.importorskip("diffusers")
pytest.importorskip("torch")

from tool_integration.adapters.image_gen import ImageGenerationTool
from utils.config import settings


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images"))
    return ImageGenerationTool()


def test_generates_real_png_file(tool):
    artifact = tool.execute(prompt="a red apple on a wooden table")

    assert artifact.modality == "image"
    from pathlib import Path

    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".png"
    assert path.stat().st_size > 0


def test_metadata_includes_prompt_and_model(tool):
    artifact = tool.execute(prompt="a blue bicycle")

    assert artifact.metadata["prompt"] == "a blue bicycle"
    assert artifact.metadata["model"] == settings.multimodal.image.model


def test_output_is_a_valid_image_with_expected_dimensions(tool):
    artifact = tool.execute(prompt="a green forest")

    from PIL import Image

    img = Image.open(artifact.uri)
    assert img.size == (settings.multimodal.image.width, settings.multimodal.image.height)


# --- Backend "api" (OpenAI Images) — sin red real, con un POST HTTP falso ---


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


@pytest.fixture
def api_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "backend", "api")
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images_api"))
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test-fake-key")
    fake_png_b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    fake_post = lambda *a, **kw: FakeResponse({"data": [{"b64_json": fake_png_b64}]})
    return ImageGenerationTool(http_post=fake_post)


def test_api_backend_without_key_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "backend", "api")
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images_api"))
    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)
    tool = ImageGenerationTool(http_post=lambda *a, **kw: FakeResponse({}))

    artifact = tool.execute(prompt="algo")

    assert artifact.metadata["status"] == "error"
    assert "IMAGE_GEN_API_KEY" in artifact.metadata["stderr"]


def test_api_backend_success_writes_decoded_image(api_tool):
    artifact = api_tool.execute(prompt="a red apple")

    assert artifact.modality == "image"
    from pathlib import Path

    path = Path(artifact.uri)
    assert path.exists()
    assert path.read_bytes() == b"fake-png-bytes"
    assert artifact.metadata["backend"] == "api"


def test_api_backend_sends_expected_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "backend", "api")
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images_api"))
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test-fake-key")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return FakeResponse({"data": [{"b64_json": base64.b64encode(b"x").decode()}]})

    tool = ImageGenerationTool(http_post=fake_post)
    tool.execute(prompt="un gato")

    assert len(calls) == 1
    assert calls[0]["json"]["prompt"] == "un gato"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test-fake-key"


def test_api_backend_http_error_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "backend", "api")
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images_api"))
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test-fake-key")
    tool = ImageGenerationTool(http_post=lambda *a, **kw: FakeResponse({}, status_code=401))

    artifact = tool.execute(prompt="algo")

    assert artifact.metadata["status"] == "error"
    assert "Fallo llamando a la API" in artifact.metadata["stderr"]

"""
Tests de tool_integration/adapters/video_gen.py.

Este es el test más pesado de los tres multimodales: genera imagen +
audio real por cada escena y los compone en video. Se mantiene a UNA
sola escena para no disparar el tiempo de ejecución innecesariamente.

Se salta si falta moviepy, el binario ffmpeg, diffusers, torch, o piper
— cualquier eslabón faltante de la cadena completa.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("moviepy")
pytest.importorskip("diffusers")
pytest.importorskip("piper")

if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg no está instalado en el sistema (requerido por moviepy)", allow_module_level=True)

from tool_integration.adapters.video_gen import VideoCompositionTool
from utils.config import settings


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image, "artifact_dir", str(tmp_path / "images"))
    monkeypatch.setattr(settings.multimodal.audio, "artifact_dir", str(tmp_path / "audio"))
    monkeypatch.setattr(settings.multimodal.video, "artifact_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings.multimodal.video, "seconds_per_scene", 2)  # test más rápido

    instance = VideoCompositionTool()
    try:
        instance.audio_tool._get_voice()
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar el modelo de voz de piper: {e}")
    return instance


def test_composes_video_from_single_scene(tool):
    scenes = [{"narration": "Esta es la única escena de la prueba.", "image_prompt": "a calm lake at sunset"}]

    artifact = tool.execute(scenes=scenes)

    assert artifact.modality == "video"
    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".mp4"
    assert path.stat().st_size > 0


def test_rejects_empty_scene_list(tool):
    with pytest.raises(ValueError):
        tool.execute(scenes=[])


def test_rejects_more_scenes_than_the_configured_maximum(tool, monkeypatch):
    """
    B-3 (auditoría externa Likay-OS, 2026-09-26): sin este tope, cada
    escena de más es una generación de imagen + TTS reales sin límite —
    el rechazo debe ocurrir ANTES de generar nada (nunca gasta cómputo
    real en escenas que después de todos modos se van a rechazar).
    """
    monkeypatch.setattr(settings.multimodal.video, "max_scenes", 2)
    scenes = [{"narration": "x", "image_prompt": "x"} for _ in range(3)]

    with pytest.raises(ValueError, match="máximo permitido"):
        tool.execute(scenes=scenes)


def test_metadata_includes_scene_references(tool):
    scenes = [{"narration": "Prueba de metadata.", "image_prompt": "a mountain landscape"}]
    artifact = tool.execute(scenes=scenes)

    assert artifact.metadata["num_scenes"] == 1
    assert len(artifact.metadata["scenes"]) == 1
    assert "image" in artifact.metadata["scenes"][0]
    assert "audio" in artifact.metadata["scenes"][0]

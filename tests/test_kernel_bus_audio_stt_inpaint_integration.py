"""
Tests de integración REAL del Kernel Service Bus para los 3 servicios
agregados al terminar de desacoplar el resto de las herramientas
(audio.synthesize, stt.transcribe, image.inpaint) — mismo patrón que
tests/test_kernel_image_service_integration.py: sin dobles de
prueba, Docker real, socket real, y los modelos reales (piper-tts,
faster-whisper, diffusers de inpainting) cuando están disponibles/ya
cacheados en este entorno. Se saltan con un mensaje claro si algo no
está instalado o si la descarga/carga de un modelo falla — mismo
criterio que el resto de la suite para estos motores.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernel.lifecycle.docker_runner import DockerSandboxRunner
from kernel.lifecycle.executor import SandboxExecutor
from tests.conftest import requires_docker
from sdk.skill import ToolManifest
from kernel.registry.sandboxed_skill import SandboxedSkillTool

pytestmark = requires_docker

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _make_skill_tool(name: str, entry_point: str, kernel_services: list[str], tmp_path: Path) -> SandboxedSkillTool:
    manifest = ToolManifest(name=name, description=f"skill de referencia '{name}'", created_by="skill")
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    return SandboxedSkillTool(
        manifest=manifest, skill_dir=SKILLS_ROOT / name, entry_point=entry_point,
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=kernel_services,
        # kernel_instance NO se inyecta a propósito: usa el bus real
        # de producción, con los servicios reales — es justo lo que se
        # quiere validar acá.
    )


def test_audio_via_kernel_skill_generates_real_audio(tmp_path):
    pytest.importorskip("piper")
    from tool_integration.services import AudioService

    try:
        AudioService()._get_voice()
    except Exception as e:
        pytest.skip(f"No se pudo cargar/descargar la voz de piper: {e}")

    skill_tool = _make_skill_tool(
        "audio_via_kernel", "tool:AudioViaKernelTool", ["audio.synthesize"], tmp_path
    )

    artifact = skill_tool.execute(text="Hola, esto es una prueba real de síntesis de voz vía el kernel.")

    assert artifact.modality == "audio"
    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".wav"
    assert path.stat().st_size > 0


def test_voice_roundtrip_via_kernel_skill_transcribes_its_own_synthesis(tmp_path):
    pytest.importorskip("piper")
    pytest.importorskip("faster_whisper")
    from tool_integration.services import AudioService, STTService

    try:
        AudioService()._get_voice()
    except Exception as e:
        pytest.skip(f"No se pudo cargar/descargar la voz de piper: {e}")
    try:
        STTService()._get_model()
    except Exception as e:
        pytest.skip(f"No se pudo cargar el modelo de whisper: {e}")

    skill_tool = _make_skill_tool(
        "voice_roundtrip_via_kernel", "tool:VoiceRoundtripViaKernelTool",
        ["audio.synthesize", "stt.transcribe"], tmp_path,
    )

    artifact = skill_tool.execute(text="Hola, esto es una prueba de ida y vuelta de voz.")

    assert artifact.modality == "text"
    assert artifact.metadata["original_text"] == "Hola, esto es una prueba de ida y vuelta de voz."
    # No se asume coincidencia exacta (whisper-tiny sobre voz sintética
    # en español no siempre transcribe perfecto, mismo criterio que
    # tests/test_speech_to_text.py) — solo que reconoció contenido real.
    assert len(artifact.metadata["transcribed_text"]) > 0


def test_image_inpaint_via_kernel_skill_edits_a_real_generated_image(tmp_path):
    pytest.importorskip("diffusers")
    pytest.importorskip("torch")
    from tool_integration.services import ImageService

    service = ImageService()
    try:
        service._get_pipeline()
        service._get_inpaint_pipeline()
    except Exception as e:
        pytest.skip(f"No se pudo cargar/descargar un modelo de imagen: {e}")

    skill_tool = _make_skill_tool(
        "image_inpaint_via_kernel", "tool:ImageInpaintViaKernelTool",
        ["image.generate", "image.inpaint"], tmp_path,
    )

    artifact = skill_tool.execute(
        prompt="a small red boat on a calm lake", inpaint_prompt="a bright yellow sun in the sky",
    )

    assert artifact.modality == "image"
    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".png"
    assert path.stat().st_size > 0
    assert artifact.metadata["operation"] == "inpaint"

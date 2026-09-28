"""
Adaptador de "video": en realidad una COMPOSICIÓN, no generación real.

No existe una opción local sin GPU viable para generación de video real
(text-to-video) — los modelos que hacen esto (Stable Video Diffusion,
etc.) requieren GPU con VRAM considerable. Esta clase construye en su
lugar un video tipo slideshow: por cada escena, genera una imagen
(ImageGenerationTool) + narración TTS (AudioGenerationTool), y las une
con un paneo simple (efecto Ken Burns) vía moviepy/ffmpeg. Es una
alternativa honesta para "video explicativo", no un sustituto de
generación de video real.

Requiere el binario `ffmpeg` instalado en el sistema (moviepy lo usa
por debajo) — no es un paquete de pip:
    sudo apt install ffmpeg

NOTA DE TRANSPARENCIA (actualizada tras pruebas reales): moviepy 2.x
efectivamente renombró los métodos set_X() a with_X() (confirmado por
error real: "'ImageClip' object has no attribute 'set_duration'. Did
you mean: 'with_duration'?"). Las llamadas a estos métodos usan
_call_first_available() para probar ambos nombres y tolerar cualquiera
de las dos versiones, en vez de asumir una sola.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import TypedDict

from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest
from tool_integration.adapters.audio_gen import AudioGenerationTool
from tool_integration.adapters.image_gen import ImageGenerationTool
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class Scene(TypedDict):
    narration: str       # texto que se convierte a voz (audio_gen)
    image_prompt: str    # prompt de la imagen de esta escena (image_gen)


class VideoCompositionTool(Tool):
    manifest = ToolManifest(
        name="video_composition",
        description=(
            "Compone un video tipo slideshow a partir de escenas "
            "(imagen generada + narración TTS + paneo simple). NO es "
            "generación de video real — ver docstring del módulo."
        ),
        requires_network=False,
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "scenes": {
                    "type": "array",
                    "description": (
                        "Lista de escenas: cada una con 'narration' (texto a narrar) "
                        "e 'image_prompt' (descripción de la imagen)"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "narration": {"type": "string"},
                            "image_prompt": {"type": "string"},
                        },
                        "required": ["narration", "image_prompt"],
                    },
                }
            },
            "required": ["scenes"],
        },
    )

    def __init__(self):
        self.cfg = settings.multimodal.video
        self.image_tool = ImageGenerationTool()
        self.audio_tool = AudioGenerationTool()
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)

    def execute(self, scenes: list[Scene], **kwargs) -> Artifact:
        if not scenes:
            raise ValueError("se necesita al menos una escena para componer un video")
        if len(scenes) > self.cfg.max_scenes:
            raise ValueError(
                f"se pidieron {len(scenes)} escenas — el máximo permitido es {self.cfg.max_scenes} "
                "(cada escena genera una imagen y una narración reales, ver config.yaml: "
                "multimodal.video.max_scenes)."
            )

        try:
            from moviepy.editor import AudioFileClip, ImageClip, concatenate_videoclips
        except ImportError:
            from moviepy import (  # moviepy >= 2.0
                AudioFileClip,
                ImageClip,
                concatenate_videoclips,
            )

        clips = []
        scene_artifacts = []
        try:
            for i, scene in enumerate(scenes):
                logger.info(f"Componiendo escena {i + 1}/{len(scenes)}")
                image_artifact = self.image_tool.execute(prompt=scene["image_prompt"])
                audio_artifact = self.audio_tool.execute(text=scene["narration"])
                scene_artifacts.append({"image": image_artifact.uri, "audio": audio_artifact.uri})

                audio_clip = AudioFileClip(audio_artifact.uri)
                duration = max(audio_clip.duration, self.cfg.seconds_per_scene)

                # BUG REAL ENCONTRADO EN PRUEBAS: moviepy 2.x renombró
                # los métodos set_X() a with_X() (confirmado por el
                # propio error real: "'ImageClip' object has no
                # attribute 'set_duration'. Did you mean:
                # 'with_duration'?"). En vez de fijar una sola API y
                # arriesgarme a romper con la otra versión, se prueba
                # primero el nombre v2 y se cae a v1 si no existe.
                image_clip = ImageClip(image_artifact.uri)
                image_clip = self._call_first_available(image_clip, ("with_duration", "set_duration"), duration)
                image_clip = self._call_first_available(
                    image_clip, ("resized", "resize"),
                    lambda t, d=duration: 1.0 + 0.05 * (t / d),
                )
                image_clip = self._call_first_available(image_clip, ("with_audio", "set_audio"), audio_clip)
                clips.append(image_clip)

            final = concatenate_videoclips(clips, method="compose")

            artifact_id = str(uuid.uuid4())
            path = Path(self.cfg.artifact_dir) / f"{artifact_id}.mp4"
            final.write_videofile(str(path), fps=self.cfg.fps, codec="libx264", audio_codec="aac", logger=None)
            final.close()
        finally:
            for clip in clips:
                clip.close()

        return Artifact(
            modality="video",
            uri=str(path),
            metadata={"num_scenes": len(scenes), "scenes": scene_artifacts},
        )

    @staticmethod
    def _call_first_available(obj, method_names: tuple[str, ...], *args, **kwargs):
        """
        Llama al primer método de `method_names` que exista en `obj`.
        Usado para tolerar el rename set_X()->with_X() entre moviepy
        1.x y 2.x sin fijar una sola versión como asumida.
        """
        for name in method_names:
            method = getattr(obj, name, None)
            if method is not None:
                return method(*args, **kwargs)
        raise AttributeError(
            f"Ninguno de los métodos {method_names} existe en {type(obj).__name__} "
            "— la API de moviepy instalada difiere más de lo esperado."
        )

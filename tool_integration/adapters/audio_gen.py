"""
Adaptador de generación de audio (texto a voz): dos backends.

- "local" (default): 100% local, sin GPU, sin API keys. Usa piper-tts (TTS
  neuronal vía ONNX Runtime), diseñado para correr eficientemente en CPU
  (a diferencia de motores más pesados como Coqui TTS).
- "api": OpenAI TTS (tts-1-hd por defecto). Requiere AUDIO_GEN_API_KEY en
  el entorno (ver .env.example) — sin ella, error claro, nunca cae en
  silencio al backend local. Sin credenciales reales de OpenAI en este
  entorno de desarrollo, se probó con un POST HTTP inyectado/falso (ver
  tests/test_audio_gen.py), no contra la API real.

NOTA DE TRANSPARENCIA (actualizada tras pruebas reales): la primera
versión de este adaptador tenía dos bugs reales, encontrados al
probarlo contra piper-tts instalado de verdad (no pude anticiparlos
sin poder instalar el paquete durante el desarrollo inicial):
  1. voice.synthesize() no configura los parámetros del WAV por sí
     sola (canales/ancho de muestra/frecuencia) — hay que hacerlo
     explícitamente antes de llamarla.
  2. En piper-tts >= 1.2, voice.synthesize() es un GENERADOR: la
     escritura real al archivo ocurre como efecto secundario de cada
     iteración, no de la sola llamada. Sin iterarlo completamente, el
     archivo queda con header válido pero 0 frames de audio, sin
     lanzar ninguna excepción — el bug más difícil de detectar de los
     dos, porque no falla ruidosamente.
Ambos ya están corregidos abajo. Si tu versión de piper-tts es más
antigua y estos ajustes rompen algo, es la señal de que la superficie
de la API cambió de nuevo entre versiones.

Los modelos de voz (.onnx + .onnx.json) se descargan una única vez
desde el repositorio "rhasspy/piper-voices" en HuggingFace Hub (mismo
patrón que sentence-transformers y sd-turbo: red solo la primera vez,
luego cacheado en disco). La estructura de subcarpetas del repo
también es una inferencia de su convención habitual
(<idioma>/<idioma_país>/<voz>/<calidad>/) — verificar contra
https://huggingface.co/rhasspy/piper-voices si la descarga falla.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

from tool_integration.provider import TTSProvider
from tool_integration.services import AudioService
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"


class AudioGenerationTool(Tool):
    manifest = ToolManifest(
        name="audio_generation",
        description="Genera audio (texto a voz) local vía piper-tts (CPU), o API si se configura",
        requires_network=False,  # tras la descarga inicial del modelo de voz (backend local)
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Texto a convertir en audio"}},
            "required": ["text"],
        },
    )

    def __init__(self, http_post: Callable[..., Any] | None = None, audio_service: TTSProvider | None = None):
        self.cfg = settings.multimodal.audio
        self.http_post = http_post or requests.post
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)
        # El tipo declarado es TTSProvider (tool_integration/provider.py)
        # — este adaptador no necesita saber que el motor concreto es
        # piper-tts. Por defecto, sin inyectar, arma su PROPIO
        # AudioService con su MISMO self.cfg (un test que monkeypatchea
        # artifact_dir antes de instanciar esta clase sigue funcionando
        # igual) — mismo patrón que ImageGenerationTool/ImageService.
        # En producción, la instancia compartida real es la misma que
        # usa el Kernel Service Bus para skills con
        # kernel_services: ["audio.synthesize"].
        self.audio_service = audio_service or AudioService(cfg=self.cfg)

    def _get_voice(self):
        # Delegado — se preserva el nombre por compatibilidad (varios
        # tests lo llaman directo para forzar la carga antes de tiempo).
        return self.audio_service._get_voice()

    def execute(self, text: str, **kwargs) -> Artifact:
        if self.cfg.backend == "api":
            return self._generate_via_api(text)
        return self._generate_locally(text)

    def _generate_locally(self, text: str) -> Artifact:
        result = self.audio_service.synthesize(text)
        return Artifact(modality="audio", uri=result["path"], metadata=result["metadata"])

    # --- Backend API (OpenAI TTS) ---

    def _generate_via_api(self, text: str) -> Artifact:
        api_key = os.environ.get("AUDIO_GEN_API_KEY")
        if not api_key:
            return self._error(
                "AUDIO_GEN_API_KEY no configurada — completá .env (ver .env.example) "
                "para usar multimodal.audio.backend: api."
            )

        try:
            response = self.http_post(
                OPENAI_TTS_URL,
                json={
                    "model": self.cfg.api_model,
                    "input": text,
                    "voice": self.cfg.api_voice,
                    "response_format": "wav",
                },
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=60,
            )
            response.raise_for_status()
            audio_bytes = response.content
        except Exception as e:
            logger.warning(f"Fallo generando audio vía API: {e}")
            return self._error(f"Fallo llamando a la API de audio: {e}")

        artifact_id = str(uuid.uuid4())
        path = Path(self.cfg.artifact_dir) / f"{artifact_id}.wav"
        path.write_bytes(audio_bytes)

        return Artifact(
            modality="audio",
            uri=str(path),
            metadata={"text": text, "model": self.cfg.api_model, "voice": self.cfg.api_voice, "backend": "api"},
        )

    @staticmethod
    def _error(message: str) -> Artifact:
        return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": message})

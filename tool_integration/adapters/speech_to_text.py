"""
Adaptador de Speech-to-Text: 100% local, sin GPU, sin API keys, vía
faster-whisper (implementación de Whisper sobre CTranslate2 — más liviana
y rápida en CPU que openai-whisper original, mismo criterio de "elegir el
motor viable en CPU" que ya se usó para sd-turbo en vez de SD completo y
piper-tts en vez de Coqui).

Modelo por defecto "tiny" (~75MB): rápido, razonablemente preciso para
frases cortas — no esperes la precisión de "large" en audio ruidoso o con
acentos marcados. El modelo se descarga una única vez desde HuggingFace
Hub (requiere red esa vez), luego queda cacheado en disco.
"""
from __future__ import annotations

from tool_integration.provider import STTProvider
from tool_integration.services import KernelServiceError, STTService
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class SpeechToTextTool(Tool):
    manifest = ToolManifest(
        name="speech_to_text",
        description=(
            "Transcribe un archivo de audio ya existente (p.ej. generado por "
            "audio_generation) a texto, local vía Whisper (CPU)."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "audio_path": {"type": "string", "description": "Ruta al archivo de audio a transcribir"},
            },
            "required": ["audio_path"],
        },
    )

    def __init__(self, stt_service: STTProvider | None = None):
        self.cfg = settings.multimodal.stt
        # El tipo declarado es STTProvider (tool_integration/provider.py)
        # — este adaptador no necesita saber que el motor concreto es
        # faster-whisper. Por defecto, sin inyectar, arma su PROPIO
        # STTService — mismo patrón que ImageGenerationTool/ImageService.
        self.stt_service = stt_service or STTService(cfg=self.cfg)

    def _get_model(self):
        # Delegado — se preserva el nombre por compatibilidad (varios
        # tests lo llaman directo para forzar la carga antes de tiempo).
        return self.stt_service._get_model()

    def execute(self, audio_path: str, **kwargs) -> Artifact:
        try:
            result = self.stt_service.transcribe(audio_path)
        except KernelServiceError as e:
            return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": str(e)})

        return Artifact(modality="text", uri="", metadata=result["metadata"])

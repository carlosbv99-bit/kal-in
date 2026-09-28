"""
Skill de referencia que ENCADENA dos servicios del Kernel Service Bus
en una sola ejecución: sintetiza voz (audio.synthesize) y transcribe
ese mismo audio de vuelta a texto (stt.transcribe) — sin ninguna
dependencia de ML propia (ni piper-tts ni faster-whisper).

A diferencia de skills/image_via_kernel/ y skills/audio_via_kernel/
(que solo producen un artefacto), acá el "artifact://audio/<uuid>" que
devuelve la primera llamada se pasa TAL CUAL como argumento de la
segunda — la skill nunca ve una ruta real de host, el kernel resuelve
la referencia por su cuenta (ver
kernel/api/bus.py::KernelServiceBus._resolve_input_artifacts()).
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.context import call as kernel_call
from sdk.skill import Tool


class VoiceRoundtripViaKernelTool(Tool):
    def execute(self, text: str, **kwargs) -> Artifact:
        synth_result = kernel_call("audio.synthesize", text=text)
        transcribe_result = kernel_call("stt.transcribe", audio_path=synth_result["artifact"])

        metadata = transcribe_result.get("metadata", {})
        return Artifact(
            modality="text",
            uri="",
            metadata={
                "original_text": text,
                "transcribed_text": metadata.get("summary", ""),
                "detected_language": metadata.get("detected_language"),
            },
        )

"""
CONTRATO PÚBLICO entre las Tools de voz (tool_integration/adapters/
audio_gen.py, speech_to_text.py) y el motor concreto que sintetiza/
transcribe — mismo espíritu que agent_core/llm/provider.py::
LLMProvider, aplicado a la capacidad de voz en vez de la de lenguaje.

Antes de este archivo, AudioGenerationTool/SpeechToTextTool declaraban
su dependencia inyectada como el tipo CONCRETO (`AudioService`/
`STTService`, ver tool_integration/services.py) — funcionaba porque
solo existe una implementación real de cada una (piper-tts,
faster-whisper), pero el adaptador terminaba conociendo el motor
concreto sin necesidad. Este archivo nombra la forma mínima que
cualquier motor de síntesis/transcripción debe tener; `AudioService`/
`STTService` ya la cumplen estructuralmente HOY, sin ningún cambio de
código en services.py.

Segundo caso real de "Provider" fuera de LLMProvider (2026-07-21,
mismo pedido del usuario que agent_core/client_provider.py) — deliberadamente
sin dataclasses tipadas para el resultado de synthesize()/transcribe()
todavía: con una sola implementación real de cada lado, tipificar el
resultado sería una abstracción prematura (ver ChatResponse en
provider.py, que sí lo justifica con DOS implementaciones reales de
wire formats distintos). Se revisita si aparece una segunda
implementación real (p.ej. una API en la nube).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TTSProvider(Protocol):
    """Todo motor de texto-a-voz que un adaptador pueda usar implementa esta forma."""

    def synthesize(self, text: str, **kwargs: Any) -> dict[str, Any]: ...


@runtime_checkable
class STTProvider(Protocol):
    """Todo motor de voz-a-texto que un adaptador pueda usar implementa esta forma."""

    def transcribe(self, audio_path: str, **kwargs: Any) -> dict[str, Any]: ...

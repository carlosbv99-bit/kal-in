"""
Tests de contrato de tool_integration/provider.py — confirman que
AudioService/STTService (tool_integration/services.py) satisfacen
estructuralmente TTSProvider/STTProvider, mismo espíritu que
tests/test_llm_provider.py para LLMProvider.
"""
from __future__ import annotations

from tool_integration.provider import STTProvider, TTSProvider
from tool_integration.services import AudioService, STTService


def test_audio_service_satisfies_the_tts_provider_protocol():
    assert isinstance(AudioService(), TTSProvider)


def test_stt_service_satisfies_the_stt_provider_protocol():
    assert isinstance(STTService(), STTProvider)

"""
Skill de referencia para el Kernel Service Bus — espejo directo de
skills/image_via_kernel/tool.py, aplicado a audio: genera voz SIN
NINGUNA dependencia de ML propia (ni piper-tts), pidiéndosela al
servicio "audio" del kernel.

Nota para autores de skills: no declara `manifest =` (ver
skills/system_info/tool.py) — nombre/descripción/permisos/
parameters_schema/kernel_services viven en skill.yaml.
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.context import call as kernel_call
from sdk.skill import Tool


class AudioViaKernelTool(Tool):
    def execute(self, text: str, **kwargs) -> Artifact:
        result = kernel_call("audio.synthesize", text=text)
        return Artifact(modality="audio", uri=result["artifact"], metadata=result.get("metadata", {}))

"""
Skill de referencia para el Kernel Service Bus (ver
kernel/__init__.py y sdk/context.py): genera una
imagen SIN NINGUNA dependencia de ML propia (ni torch ni diffusers) —
le pide la imagen al servicio "image" del kernel, que comparte el mismo
pipeline de SDXL-Turbo que ya usa
tool_integration/adapters/image_gen.py (una sola instancia cargada,
nunca una copia por consumidor).

Nota para autores de skills: no declara `manifest =` (ver
skills/system_info/tool.py) — nombre/descripción/permisos/
parameters_schema/kernel_services viven en skill.yaml.
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.context import call as kernel_call
from sdk.skill import Tool


class ImageViaKernelTool(Tool):
    def execute(self, prompt: str, **kwargs) -> Artifact:
        result = kernel_call("image.generate", prompt=prompt)
        return Artifact(modality="image", uri=result["artifact"], metadata=result.get("metadata", {}))

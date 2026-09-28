"""
Skill de referencia que ENCADENA dos acciones del servicio "image" del
Kernel Service Bus en una sola ejecución: genera una imagen base
(image.generate) y reemplaza una región de ella con IA (image.inpaint)
— sin ninguna dependencia de ML propia (ni torch ni diffusers).

El "artifact://image/<uuid>" que devuelve la primera llamada se pasa
TAL CUAL como image_path de la segunda — la skill nunca ve una ruta
real de host (ver
kernel/api/bus.py::KernelServiceBus._resolve_input_artifacts()).
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.context import call as kernel_call
from sdk.skill import Tool

# Rectángulo centrado razonable para la imagen base de 1024x1024 que
# genera image.generate por defecto (ver settings.multimodal.image).
_DEFAULT_BOX = [384, 384, 640, 640]


class ImageInpaintViaKernelTool(Tool):
    def execute(self, prompt: str, inpaint_prompt: str, box: list[int] | None = None, **kwargs) -> Artifact:
        base_result = kernel_call("image.generate", prompt=prompt)
        inpaint_result = kernel_call(
            "image.inpaint",
            image_path=base_result["artifact"],
            box=box or _DEFAULT_BOX,
            prompt=inpaint_prompt,
        )
        return Artifact(
            modality="image", uri=inpaint_result["artifact"], metadata=inpaint_result.get("metadata", {})
        )

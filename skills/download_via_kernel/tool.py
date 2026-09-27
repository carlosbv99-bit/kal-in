"""
Skill de referencia para el Kernel Download Service — espejo directo de
skills/audio_via_kernel/tool.py, aplicado a download.fetch: descarga un
archivo real de Internet SIN ninguna dependencia de red propia,
pidiéndosela al servicio "download" del kernel (que hace la validación
real: dominio permitido, IP segura, tamaño, malware, contenido).

Nota para autores de skills: no declara `manifest =` (ver
skills/system_info/tool.py) — nombre/descripción/permisos/
parameters_schema/kernel_services viven en skill.yaml.
"""
from __future__ import annotations

from sdk.artifacts import Artifact
from sdk.context import call as kernel_call
from sdk.skill import Tool


class DownloadViaKernelTool(Tool):
    def execute(self, url: str, expected_type: str, **kwargs) -> Artifact:
        result = kernel_call("download.fetch", url=url, expected_type=expected_type)
        return Artifact(modality=expected_type, uri=result["artifact"], metadata=result.get("metadata", {}))

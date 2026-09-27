"""
Skill de ejemplo/plantilla del sistema de skills (ver
kernel/registry/skills.py). Sin dependencias externas ni credenciales:
sirve para probar el pipeline completo (manifiesto -> registro ->
disponible para el agente) de punta a punta con algo real.

Nota para autores de skills: esta clase NO declara `manifest =
ToolManifest(...)` como atributo — nombre/descripción/permisos/
parameters_schema viven en skill.yaml, la única fuente de verdad
(load_skills() nunca importa este archivo en el proceso principal, ver
kernel/registry/skills.py). Un atributo `manifest` acá no rompería
nada, pero tampoco lo lee nadie — se omite para no sugerir que importa.
"""
from __future__ import annotations

import platform
import shutil

from sdk.artifacts import Artifact
from sdk.skill import Tool


class SystemInfoTool(Tool):
    def execute(self, **kwargs) -> Artifact:
        disk = shutil.disk_usage("/")
        summary = (
            f"SO: {platform.system()} {platform.release()}\n"
            f"Python: {platform.python_version()}\n"
            f"Disco libre: {disk.free // (1024 ** 3)} GB de {disk.total // (1024 ** 3)} GB"
        )
        return Artifact(modality="text", uri="", metadata={"summary": summary})

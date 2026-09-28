"""
Skill de referencia para el pipeline de aislamiento real de skills (ver
kernel/registry/skills.py). Dos cosas a propósito, para validar el
pipeline completo con algo real:
  - Depende de un paquete externo (qrcode + pillow, ver skill.yaml
    "requirements") -> valida la imagen Docker derivada
    (kernel/lifecycle/skill_image_builder.py).
  - Devuelve un archivo real (PNG), no solo texto -> valida la
    convención KAL_SKILL_OUTPUT_DIR (ver kernel/lifecycle/skill_runner.py).

Cualquier autor de una skill nueva que necesite devolver un archivo
puede copiar este patrón: leer KAL_SKILL_OUTPUT_DIR (con un fallback a
"." para poder probar la skill fuera del sandbox, como hace el test de
esta carpeta), escribir ahí, y devolver en Artifact.uri SOLO el nombre
de archivo (nunca una ruta absoluta — dentro del contenedor esa ruta no
significa nada para el host).

Nota para autores de skills: esta clase NO declara `manifest =
ToolManifest(...)` — nombre/descripción/permisos/parameters_schema
viven en skill.yaml, la única fuente de verdad (load_skills() nunca
importa este archivo en el proceso principal, ver
kernel/registry/skills.py).
"""
from __future__ import annotations

import os
import uuid

from sdk.artifacts import Artifact
from sdk.skill import Tool


class QRCodeTool(Tool):
    def execute(self, text: str, **kwargs) -> Artifact:
        import qrcode

        image = qrcode.make(text)

        output_dir = os.environ.get("KAL_SKILL_OUTPUT_DIR", ".")
        os.makedirs(output_dir, exist_ok=True)
        filename = f"{uuid.uuid4()}.png"
        image.save(os.path.join(output_dir, filename))

        return Artifact(modality="image", uri=filename, metadata={"text": text})

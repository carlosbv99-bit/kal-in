"""
Adaptador de análisis de imágenes: describe/responde preguntas sobre el
CONTENIDO de una imagen ya existente, vía un modelo de visión local
(llama3.2-vision u otro que soporte el campo "images" de Ollama).

A diferencia de los demás adaptadores multimodales (image_gen.py,
image_editing.py), esto no genera ni modifica ninguna imagen — solo
devuelve texto (mismo patrón que speech_to_text.py: Artifact con
modality="text"). Y a diferencia de esos, el modelo no corre dentro de
este proceso vía un Kernel Service (ImageService/STTService): corre en
el servidor Ollama ya en ejecución, igual que el modelo de lenguaje del
agente — por eso usa OllamaClient directo en vez de un servicio nuevo.

Deliberadamente usa OllamaClient() SIEMPRE, sin pasar por la fábrica de
settings.llm.provider (que puede apuntar a un proveedor en la nube):
las capacidades multimedia locales quedan fijas a su propio modelo
local, igual que image_gen/audio_gen/speech_to_text, independiente de
qué proveedor use el agente para decidir qué herramienta llamar.
"""
from __future__ import annotations

import base64
from pathlib import Path

from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.provider import ProviderError
from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class ImageAnalysisTool(Tool):
    manifest = ToolManifest(
        name="analyze_image",
        description=(
            "Analiza/describe el CONTENIDO de una imagen ya existente usando un modelo de "
            "visión (no la edita ni genera una nueva) — usar cuando el pedido es entender qué "
            "hay en una foto/imagen (describirla, identificar objetos, leer texto visible, "
            "responder una pregunta sobre ella), nunca para crear o modificar una imagen (para "
            "eso: image_generation/image_editing)."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Ruta a la imagen ya existente a analizar"},
                "question": {
                    "type": "string",
                    "description": "Qué preguntar o pedir sobre la imagen (p.ej. 'Describí esta imagen en detalle')",
                },
            },
            "required": ["image_path", "question"],
        },
    )

    def __init__(self, llm_client: OllamaClient | None = None):
        self.cfg = settings.multimodal.vision
        # base_url explícito: NUNCA el default de OllamaClient (que cae
        # a settings.llm.base_url, apuntando a un proveedor en la nube
        # si ese perfil está activo) — ver VisionConfig.base_url.
        self.llm_client = llm_client or OllamaClient(base_url=self.cfg.base_url)

    def execute(self, image_path: str, question: str, **kwargs) -> Artifact:
        path = Path(image_path)
        if not path.is_file():
            return Artifact(
                modality="text", uri="",
                metadata={"status": "error", "stderr": f"No existe el archivo de imagen '{image_path}'"},
            )

        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        try:
            response = self.llm_client.chat(
                messages=[{"role": "user", "content": question}],
                model=self.cfg.model,
                images=[image_b64],
            )
        except ProviderError as e:
            return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": str(e)})

        # BUG REAL ENCONTRADO EN USO: con "status": "success" acá,
        # agent_loop.py::_artifact_to_observation() toma la rama de
        # run_code (espera "stdout") y devuelve "(sin salida)" — el
        # modelo nunca ve la descripción real. "summary" es la
        # convención correcta para texto genérico (ver
        # speech_to_text.py, mismo patrón), NUNCA "status": "success"
        # a menos que también se mande "stdout".
        return Artifact(
            modality="text", uri="",
            metadata={"summary": response.content, "image_path": image_path},
        )

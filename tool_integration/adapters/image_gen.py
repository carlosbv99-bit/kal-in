"""
Adaptador de generación de imágenes: dos backends.

- "local" (default): 100% local, sin GPU, sin API keys. Usa
  stabilityai/sdxl-turbo (SDXL destilado a 1-4 pasos de inferencia, mismo
  criterio de distilación que sd-turbo/SD1.5 pero con la arquitectura SDXL
  — mejor calidad, nativo a 1024x1024) en vez de un modelo de difusión
  completo (~50 pasos), que sería impráctico en CPU. Sustancialmente más
  pesado y lento que sd-turbo — la primera vez que se instancia esta clase
  se descarga el modelo desde HuggingFace Hub (requiere red esa vez,
  ~14GB en fp32; float16 no es fiable en CPU), luego queda cacheado en
  disco (~/.cache/huggingface) y no vuelve a tocar la red. Segundos a
  varios minutos por imagen según el hardware — no lo esperes instantáneo,
  y esperá bastante más que con sd-turbo. Para volver al modelo liviano
  anterior: `config.yaml: multimodal.image.model: "stabilityai/sd-turbo"`
  (+ height/width a 512).

- "api": OpenAI Images (dall-e-3 por defecto). Requiere IMAGE_GEN_API_KEY
  en el entorno (ver .env.example) — sin ella, devuelve un error claro en
  vez de fallar de forma confusa o caer en silencio al backend local. Sin
  credenciales reales de OpenAI en el entorno de desarrollo de este
  proyecto, este backend se probó con un POST HTTP inyectado/falso (ver
  tests/test_image_gen.py), nunca contra la API real — mismo criterio de
  transparencia que Playwright en tool_integration/adapters/browser.py.

`manifest.requires_network=False` describe el backend LOCAL en su
funcionamiento estable (post-descarga inicial) — con backend="api", cada
ejecución sí requiere red real, independientemente de lo que declare el
manifiesto estático (misma limitación ya documentada para el backend local
respecto de la descarga inicial).
"""
from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path
from typing import Any, Callable

import requests

from tool_integration.services import ImageService
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"


class ImageGenerationTool(Tool):
    manifest = ToolManifest(
        name="image_generation",
        description="Genera una imagen a partir de un prompt de texto (local, SDXL-Turbo, CPU, o API si se configura)",
        requires_network=False,  # tras la descarga inicial del modelo (backend local)
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Descripción de la imagen"},
                "width": {
                    "type": "integer",
                    "description": (
                        "Ancho en píxeles (solo backend local, múltiplo de 8; default "
                        "1024). Usar junto con 'height' para pedir otra relación de "
                        "aspecto, p.ej. 1024x576 para 16:9."
                    ),
                },
                "height": {
                    "type": "integer",
                    "description": "Alto en píxeles (solo backend local, múltiplo de 8; default 1024)",
                },
            },
            "required": ["prompt"],
        },
    )

    def __init__(self, http_post: Callable[..., Any] | None = None, image_service: ImageService | None = None):
        self.cfg = settings.multimodal.image
        # Inyectable para tests (evita red real) — por defecto, requests.post real.
        self.http_post = http_post or requests.post
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)
        # La carga/generación real vive en ImageService (tool_integration/services.py)
        # — antes este pipeline era privado de esta instancia; ahora, en
        # producción, es la MISMA instancia que usa el Kernel Service Bus
        # para las skills que declaran kernel_services: ["image.generate"]
        # (ver kernel/registry/registry.py) — se comparte el modelo
        # cargado, no se recarga por cada consumidor. Por defecto (sin
        # inyectar), cada ImageGenerationTool() arma su PROPIO ImageService
        # con su MISMO self.cfg (mismo objeto que settings.multimodal.image
        # — un test que monkeypatchea artifact_dir antes de instanciar esta
        # clase sigue funcionando exactamente igual que antes).
        self.image_service = image_service or ImageService(cfg=self.cfg)

    def execute(self, prompt: str, **kwargs) -> Artifact:
        if self.cfg.backend == "api":
            return self._generate_via_api(prompt)
        return self._generate_locally(prompt, **kwargs)

    # --- Backend local (diffusers, vía ImageService compartido) ---

    def _get_pipeline(self):
        # Delegado — se preserva el nombre por compatibilidad (nada más
        # de este archivo lo llama, pero es la forma establecida de
        # forzar la carga antes de tiempo en otros adaptadores).
        return self.image_service._get_pipeline()

    def _generate_locally(self, prompt: str, **kwargs) -> Artifact:
        result = self.image_service.generate(prompt, **kwargs)
        return Artifact(modality="image", uri=result["path"], metadata=result["metadata"])

    # --- Backend API (OpenAI Images) ---

    def _generate_via_api(self, prompt: str) -> Artifact:
        api_key = os.environ.get("IMAGE_GEN_API_KEY")
        if not api_key:
            return self._error(
                "IMAGE_GEN_API_KEY no configurada — completá .env (ver .env.example) "
                "para usar multimodal.image.backend: api."
            )

        try:
            response = self.http_post(
                OPENAI_IMAGES_URL,
                json={
                    "model": self.cfg.api_model,
                    "prompt": prompt,
                    "size": self.cfg.api_size,
                    "n": 1,
                    "response_format": "b64_json",
                },
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=60,
            )
            response.raise_for_status()
            image_bytes = base64.b64decode(response.json()["data"][0]["b64_json"])
        except Exception as e:
            logger.warning(f"Fallo generando imagen vía API: {e}")
            return self._error(f"Fallo llamando a la API de imágenes: {e}")

        artifact_id = str(uuid.uuid4())
        path = Path(self.cfg.artifact_dir) / f"{artifact_id}.png"
        path.write_bytes(image_bytes)

        return Artifact(
            modality="image",
            uri=str(path),
            metadata={"prompt": prompt, "model": self.cfg.api_model, "backend": "api"},
        )

    @staticmethod
    def _error(message: str) -> Artifact:
        return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": message})

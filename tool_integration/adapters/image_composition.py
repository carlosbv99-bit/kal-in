"""
Adaptador de composición de imágenes: combinar dos o más imágenes ya
generadas en una sola (superponer una sobre otra, o concatenarlas en
fila/columna). 100% Pillow — sin ningún modelo nuevo, misma dependencia
que ya usa image_editing.py para crop/upscale.

- "overlay": pega `overlay_image_path` sobre `base_image_path`. Si el
  overlay tiene canal alfa (transparencia real — p.ej. viene de
  remove_background), se respeta: solo se pega lo opaco, nunca un
  rectángulo sólido.
- "side_by_side": concatena 2+ imágenes en fila o columna,
  redimensionando cada una para que compartan alto (fila) o ancho
  (columna) antes de unirlas.

MISMA LIMITACIÓN que "inpaint" en image_editing.py: el LLM no puede ver
las imágenes. En "overlay", si el pedido depende de una posición EXACTA
("en la esquina de arriba a la derecha, pegado al borde"), 'position'
es una estimación a ciegas — por eso el default sin 'position' es
centrar el overlay, que no requiere adivinar nada y da un resultado
razonable en la mayoría de los casos.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class ImageCompositionTool(Tool):
    manifest = ToolManifest(
        name="image_composition",
        description=(
            "Combina dos o más imágenes ya generadas en una sola: superponer una imagen "
            "sobre otra ('overlay' — p.ej. un logo sin fondo sobre una foto), o "
            "concatenar varias en fila/columna ('side_by_side' — p.ej. un collage). "
            "En 'overlay', si el overlay viene de remove_background (con transparencia "
            "real), se respeta esa transparencia al pegarlo. "
            "IMPORTANTE sobre 'position' en 'overlay': NO podés ver las imágenes — si el "
            "usuario pide una posición exacta ('en la esquina', 'pegado al borde "
            "derecho'), es una estimación a ciegas; avisale en tu respuesta. Si no te "
            "piden una posición específica, no la especifiques — el default centra el "
            "overlay automáticamente, sin necesidad de adivinar nada."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["overlay", "side_by_side"],
                    "description": "Qué operación aplicar",
                },
                "base_image_path": {
                    "type": "string",
                    "description": "Solo para 'overlay': ruta de la imagen de fondo",
                },
                "overlay_image_path": {
                    "type": "string",
                    "description": "Solo para 'overlay': ruta de la imagen a superponer",
                },
                "position": {
                    "type": "array",
                    "description": (
                        "Solo para 'overlay': [x, y] de la esquina superior izquierda donde "
                        "pegar. Estimación a ciegas si se especifica — omitir para centrar "
                        "automáticamente (recomendado salvo pedido explícito de posición)."
                    ),
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "scale": {
                    "type": "number",
                    "description": "Solo para 'overlay': factor de escala del overlay antes de pegarlo (default 1, sin cambio)",
                },
                "image_paths": {
                    "type": "array",
                    "description": "Solo para 'side_by_side': rutas de las imágenes a concatenar, en orden (mínimo 2)",
                    "items": {"type": "string"},
                    "minItems": 2,
                },
                "direction": {
                    "type": "string",
                    "enum": ["horizontal", "vertical"],
                    "default": "horizontal",
                    "description": "Solo para 'side_by_side': en fila o en columna",
                },
            },
            "required": ["operation"],
        },
    )

    def __init__(self):
        self.cfg = settings.multimodal.composition
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        operation: str,
        base_image_path: str | None = None,
        overlay_image_path: str | None = None,
        position: list[int] | None = None,
        scale: float = 1,
        image_paths: list[str] | None = None,
        direction: str = "horizontal",
        **kwargs,
    ) -> Artifact:
        if operation == "overlay":
            return self._overlay(base_image_path, overlay_image_path, position, scale)
        if operation == "side_by_side":
            return self._side_by_side(image_paths, direction)
        return self._error(f"Operación desconocida: '{operation}' (usar overlay/side_by_side)")

    def _overlay(
        self,
        base_image_path: str | None,
        overlay_image_path: str | None,
        position: list[int] | None,
        scale: float,
    ) -> Artifact:
        if not base_image_path:
            return self._error("'overlay' requiere 'base_image_path'")
        if not overlay_image_path:
            return self._error("'overlay' requiere 'overlay_image_path'")
        if not Path(base_image_path).exists():
            return self._error(f"No existe la imagen: {base_image_path}")
        if not Path(overlay_image_path).exists():
            return self._error(f"No existe la imagen: {overlay_image_path}")

        from PIL import Image

        with Image.open(base_image_path) as base_source:
            base = base_source.convert("RGBA")
        with Image.open(overlay_image_path) as overlay_source:
            overlay = overlay_source.convert("RGBA")

        if scale != 1:
            new_size = (round(overlay.width * scale), round(overlay.height * scale))
            overlay = overlay.resize(new_size, Image.Resampling.LANCZOS)

        if position is None:
            resolved_position = (
                (base.width - overlay.width) // 2,
                (base.height - overlay.height) // 2,
            )
        else:
            resolved_position = tuple(position)

        # `overlay` como su propia máscara: respeta el canal alfa (solo
        # pega lo opaco) en vez de un rectángulo sólido.
        composed = base.copy()
        composed.paste(overlay, resolved_position, overlay)

        return self._save(
            composed, "overlay", base_image_path,
            {"overlay_path": overlay_image_path, "position": list(resolved_position), "scale": scale},
        )

    def _side_by_side(self, image_paths: list[str] | None, direction: str) -> Artifact:
        if image_paths is None or len(image_paths) < 2:
            return self._error("'side_by_side' requiere 'image_paths' con al menos 2 rutas")
        for path in image_paths:
            if not Path(path).exists():
                return self._error(f"No existe la imagen: {path}")

        from PIL import Image

        images = []
        for path in image_paths:
            with Image.open(path) as img:
                images.append(img.convert("RGBA"))

        if direction == "vertical":
            target_width = images[0].width
            resized = [
                img if img.width == target_width
                else img.resize((target_width, round(img.height * target_width / img.width)), Image.Resampling.LANCZOS)
                for img in images
            ]
            total_height = sum(img.height for img in resized)
            canvas = Image.new("RGBA", (target_width, total_height))
            y = 0
            for img in resized:
                canvas.paste(img, (0, y))
                y += img.height
        else:
            target_height = images[0].height
            resized = [
                img if img.height == target_height
                else img.resize((round(img.width * target_height / img.height), target_height), Image.Resampling.LANCZOS)
                for img in images
            ]
            total_width = sum(img.width for img in resized)
            canvas = Image.new("RGBA", (total_width, target_height))
            x = 0
            for img in resized:
                canvas.paste(img, (x, 0))
                x += img.width

        return self._save(canvas, "side_by_side", image_paths[0], {"image_paths": image_paths, "direction": direction})

    def _save(self, image, operation: str, source_path: str, extra_metadata: dict) -> Artifact:
        artifact_id = str(uuid.uuid4())
        path = Path(self.cfg.artifact_dir) / f"{artifact_id}.png"
        image.save(path)
        return Artifact(
            modality="image",
            uri=str(path),
            metadata={"operation": operation, "source_path": source_path, **extra_metadata},
        )

    @staticmethod
    def _error(message: str) -> Artifact:
        return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": message})

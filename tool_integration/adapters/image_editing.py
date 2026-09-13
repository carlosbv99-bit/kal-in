"""
Adaptador de edición de imágenes: recortar, eliminar fondo, "upscale", e
inpainting real con IA. 100% local, sin API keys.

- "crop": Pillow puro, sin modelo.
- "remove_background": rembg (modelo u2net, ONNX, ~176MB la primera vez,
  carga perezosa igual que los demás adaptadores).
- "upscale": resize Lanczos de alta calidad. IMPORTANTE — esto NO es
  super-resolución con IA (no interpola detalle nuevo, solo escala con un
  filtro de calidad). Se investigó `realesrgan` (super-resolución real)
  pero su dependencia núcleo `basicsr` no instala en este entorno (bug
  conocido de basicsr con versiones recientes de torchvision). En vez de
  fingir que esto es IA, se documenta así de forma honesta — mismo
  espíritu que `video_gen.py`, que se declara "composición, no generación
  real". Para upscale con IA real a futuro: resolver basicsr, o evaluar
  alternativas más nuevas (p.ej. el paquete `super-image`).
- "inpaint": relleno real con IA sobre una región rectangular (`box`),
  vía un modelo de difusión de inpainting COMPLETO (no distilado como
  sd-turbo) — mucho más lento en CPU, del orden de minutos por edición,
  no segundos. Nace de un caso real: sin esto, pedirle a kal "agregá un
  queso delante del ratón" no tenía ninguna herramienta real que llamar,
  y terminaba improvisando algo sin sentido (ver historial). Permite
  edición iterativa: cada llamada genera un artefacto nuevo a partir del
  anterior, nunca sobreescribe (mismo principio que crop/upscale).
  `safety_checker=None` al cargar — evita bajar/cargar ese componente
  extra (~1.2GB) innecesario para este caso de uso 100% local.
- "add_text": escribe texto real y legible sobre la imagen (título o
  pie), 100% Pillow — sin ningún modelo. BUG REAL ENCONTRADO EN USO:
  pedido "escribí como título 'EL COLIBRI' arriba de la imagen", el
  modelo llamó a image_composition/overlay pasando LA MISMA imagen como
  base_image_path y overlay_image_path (no existía ninguna herramienta
  real para escribir texto) — el resultado era visualmente idéntico al
  original, sin texto, y aun así kal afirmó en su respuesta que el
  título se había agregado. Los modelos de difusión (sd-turbo/SDXL-turbo
  ya usados en image_generation.py) tampoco renderizan texto de forma
  confiable — por eso esto usa Pillow con una fuente vectorial
  (ImageFont.load_default con tamaño, soportado desde Pillow 10.1,
  sin depender de que haya fuentes del sistema instaladas) y contorno
  negro sobre relleno blanco, legible sin importar el color de fondo.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from tool_integration.services import ImageService
from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class ImageEditingTool(Tool):
    manifest = ToolManifest(
        name="image_editing",
        description=(
            "Edita una imagen ya existente: recortar ('crop'), eliminar el fondo "
            "('remove_background'), agrandarla ('upscale' — resize de alta calidad, "
            "NO es super-resolución con IA), rellenar/reemplazar una región con IA "
            "('inpaint' — para agregar o cambiar algo dentro de una imagen ya generada, "
            "p.ej. 'agregar un queso delante del ratón'; mucho más lento que generar una "
            "imagen nueva, del orden de minutos), o escribir texto real y legible como "
            "título o pie ('add_text' — p.ej. 'escribí ARRIBA como título EL COLIBRI'). "
            "USAR SIEMPRE 'add_text' (nunca image_composition, nunca pedirle el texto a "
            "image_generation) para cualquier pedido de escribir texto/título/letras SOBRE "
            "una imagen — ni la generación por difusión ni 'overlay' de image_composition "
            "pueden escribir texto de forma confiable o legible. "
            "IMPORTANTE sobre 'inpaint': vos mismo NO podés ver la imagen — el 'box' que pases "
            "siempre es una estimación, nunca una medición directa tuya. Si el pedido depende de "
            "acertar la posición de un objeto específico en una imagen YA EXISTENTE (no una que "
            "generaste en este mismo turno — p.ej. 'borrá la paloma de la izquierda', 'sacá el "
            "segundo perro', 'hay dos X, borrá una'), llamá primero a analyze_image preguntando "
            "por la ubicación aproximada de ese objeto (arriba/abajo/izquierda/derecha/centro) y "
            "usá esa descripción para elegir un 'box' informado — NUNCA adivines a ciegas sin "
            "haber consultado analyze_image antes cuando la posición importa. BUG REAL ENCONTRADO "
            "EN USO: 'hay dos orcas en la imagen, borra una de ellas' fue directo a un 'box' "
            "adivinado sin llamar a analyze_image — el resultado terminó siendo una composición "
            "totalmente distinta, sin ninguna orca borrada de verdad. Aun con esa consulta previa, "
            "la posición sigue siendo una estimación (ahora informada, no una medición exacta) — "
            "aclaralo en tu respuesta final. NUNCA afirmes que la edición salió bien como si "
            "hubieras confirmado el resultado viéndolo vos mismo — no podés."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Ruta a la imagen a editar"},
                "operation": {
                    "type": "string",
                    "enum": ["crop", "remove_background", "upscale", "inpaint", "add_text"],
                    "description": "Qué operación aplicar",
                },
                "box": {
                    "type": "array",
                    "description": (
                        "Para 'crop' e 'inpaint': [izquierda, arriba, derecha, abajo] en píxeles — "
                        "en 'inpaint', la región que se va a rellenar/reemplazar. Para 'inpaint', si "
                        "la posición de un objeto específico importa, consultá antes a analyze_image "
                        "para ubicarlo aproximadamente (ver descripción de la herramienta) — el 'box' "
                        "sigue siendo una ESTIMACIÓN, ahora informada en vez de completamente a "
                        "ciegas. Avisale igual al usuario en tu respuesta si el pedido dependía de "
                        "acertar la posición exacta de algo."
                    ),
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                },
                "scale": {
                    "type": "number",
                    "description": "Solo para 'upscale': factor de escala (default 2)",
                    "default": 2,
                },
                "text": {
                    "type": "string",
                    "description": "Solo para 'add_text': el texto exacto a escribir sobre la imagen",
                },
                "text_position": {
                    "type": "string",
                    "enum": ["top", "bottom"],
                    "default": "top",
                    "description": "Solo para 'add_text': franja donde escribir el texto (arriba o abajo), centrado horizontalmente",
                },
                "prompt": {
                    "type": "string",
                    "description": "Solo para 'inpaint': qué debe aparecer en la región de 'box'",
                },
            },
            "required": ["image_path", "operation"],
        },
    )

    def __init__(self, image_service: ImageService | None = None):
        self.cfg = settings.multimodal.image_editing
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)
        # El inpainting real vive en ImageService (tool_integration/services.py)
        # — mismo servicio COMPARTIDO que usa image_generation.py (mismo
        # dominio "image", dos acciones), no una copia por adaptador. Sin
        # inyectar, arma su propia instancia con su MISMO editing_cfg
        # (self.cfg) — un test que monkeypatchea settings.multimodal.image_editing
        # antes de instanciar esta clase sigue funcionando igual.
        self.image_service = image_service or ImageService(editing_cfg=self.cfg)

    def execute(
        self,
        image_path: str,
        operation: str,
        box: list[int] | None = None,
        scale: float = 2,
        prompt: str | None = None,
        text: str | None = None,
        text_position: str = "top",
        **kwargs,
    ) -> Artifact:
        if not Path(image_path).exists():
            return self._error(f"No existe la imagen: {image_path}")

        if operation == "crop":
            return self._crop(image_path, box)
        if operation == "remove_background":
            return self._remove_background(image_path)
        if operation == "upscale":
            return self._upscale(image_path, scale)
        if operation == "inpaint":
            return self._inpaint(image_path, box, prompt)
        if operation == "add_text":
            return self._add_text(image_path, text, text_position)
        return self._error(f"Operación desconocida: '{operation}' (usar crop/remove_background/upscale/inpaint/add_text)")

    def _crop(self, image_path: str, box: list[int] | None) -> Artifact:
        if box is None or len(box) != 4:
            return self._error("'crop' requiere 'box' con exactamente 4 valores [izquierda, arriba, derecha, abajo]")

        from PIL import Image

        with Image.open(image_path) as img:
            cropped = img.crop(tuple(box))
            return self._save(cropped, "crop", image_path, {"box": box})

    def _remove_background(self, image_path: str) -> Artifact:
        from PIL import Image
        from rembg import remove

        with Image.open(image_path) as img:
            result = remove(img)
            return self._save(result, "remove_background", image_path, {})

    def _upscale(self, image_path: str, scale: float) -> Artifact:
        from PIL import Image

        with Image.open(image_path) as img:
            new_size = (round(img.width * scale), round(img.height * scale))
            upscaled = img.resize(new_size, Image.Resampling.LANCZOS)
            return self._save(
                upscaled, "upscale", image_path,
                {"scale": scale, "method": "lanczos_resize", "ai_based": False},
            )

    def _add_text(self, image_path: str, text: str | None, text_position: str) -> Artifact:
        if not text:
            return self._error("'add_text' requiere 'text'")

        from PIL import Image, ImageDraw, ImageFont

        with Image.open(image_path) as source:
            img = source.convert("RGBA")

        draw = ImageDraw.Draw(img)
        # Proporcional al ancho de la imagen: legible sin importar la
        # resolución real, sin que el modelo tenga que adivinar un tamaño
        # en píxeles para una imagen que no puede ver.
        font_size = max(24, round(img.width * 0.06))
        font = ImageFont.load_default(size=font_size)
        # Contorno negro sobre relleno blanco: legible sobre cualquier
        # fondo, sin necesidad de saber qué color tiene esa zona de la
        # imagen (que el modelo tampoco puede ver).
        stroke_width = max(2, font_size // 12)

        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        margin = round(img.height * 0.04)

        x = (img.width - text_width) // 2 - bbox[0]
        if text_position == "bottom":
            y = img.height - text_height - margin - bbox[1]
        else:
            y = margin - bbox[1]

        draw.text((x, y), text, font=font, fill="white", stroke_width=stroke_width, stroke_fill="black")

        return self._save(img, "add_text", image_path, {"text": text, "text_position": text_position})

    # --- Inpainting real con IA ---

    def _get_inpaint_pipeline(self):
        # Delegado — se preserva el nombre por compatibilidad (varios
        # tests lo llaman directo para forzar la carga antes de tiempo).
        return self.image_service._get_inpaint_pipeline()

    def _inpaint(self, image_path: str, box: list[int] | None, prompt: str | None) -> Artifact:
        if box is None or len(box) != 4:
            return self._error("'inpaint' requiere 'box' con exactamente 4 valores [izquierda, arriba, derecha, abajo]")
        if not prompt:
            return self._error("'inpaint' requiere 'prompt' describiendo qué debe aparecer en esa región")

        result = self.image_service.inpaint(image_path, box, prompt)
        return Artifact(modality="image", uri=result["path"], metadata=result["metadata"])

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

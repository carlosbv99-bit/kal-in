"""
Adaptador de extracción de texto de una imagen: OCR DEDICADO (detección
+ reconocimiento de caracteres, `rapidocr` sobre ONNXRuntime — misma
familia de modelos PP-OCR de Baidu, reimplementados sin depender del
framework PaddlePaddle) en vez de pedirle a un modelo de visión-lenguaje
que "describa" el texto.

BUG REAL ENCONTRADO EN USO (2026-09-22) que motiva este adaptador
separado de analyze_image (image_analysis.py): un modelo generativo
puede alucinar contenido que no está en la imagen — probado en vivo,
minicpm-v (multimodal.vision.model) inventó una sección de RAM COMPLETA
(marca, capacidad, números de parte) frente a una foto real de CPU-Z
que no tiene ninguna pestaña de Memoria visible. Un pipeline de OCR
clasificatorio (detección de regiones + reconocimiento de caracteres,
NUNCA generación de texto libre) no tiene ese modo de falla: como mucho
lee mal un carácter puntual (p.ej. "i3" como "13"), nunca inventa una
sección entera. Ver utils/config.py::OCRConfig para la comparación
completa en vivo (incluida la razón de usar rapidocr y no
paddleocr/paddlepaddle directo: paddlepaddle no tiene build para Python
3.14 todavía).

analyze_image (image_analysis.py) sigue siendo la herramienta correcta
para preguntas visuales que NO son de texto ("qué hay en esta imagen",
"identificá este objeto") — este adaptador es específicamente para
"transcribí/leé el texto que aparece en esta imagen".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from sdk.skill import Tool, ToolManifest
from sdk.artifacts import Artifact
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


class OCREngine(Protocol):
    def __call__(self, img_path: str) -> Any: ...


def _build_default_engine() -> OCREngine:
    # Import perezoso: rapidocr (y sus dependencias, onnxruntime/opencv)
    # no deberían hacer falta hasta que se llama execute() de verdad —
    # mismo criterio que diffusers/piper/moviepy en el resto de este
    # paquete (ver default_tools.py::register_default_static_tools()).
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    cfg = settings.multimodal.ocr
    return RapidOCR(
        params={
            "Rec.lang_type": LangRec(cfg.lang_type),
            "Rec.ocr_version": OCRVersion(cfg.ocr_version),
            "Rec.model_type": ModelType(cfg.model_type),
        }
    )


class TextExtractionTool(Tool):
    manifest = ToolManifest(
        name="extract_text_from_image",
        description=(
            "Transcribe/lee el texto que aparece ESCRITO dentro de una imagen ya existente "
            "(letras de una canción en una captura, texto de un cartel, una tabla de datos en "
            "una foto de pantalla, etc.) usando un pipeline de OCR dedicado — no un modelo "
            "generativo, así que no inventa contenido que no esté ahí. Usar esto (no "
            "analyze_image) cuando el pedido es EXTRAER/TRANSCRIBIR texto real, incluido "
            "identificar algo (una canción, una cita) A PARTIR de ese texto. Para preguntas "
            "visuales que no son de texto (describir la escena, identificar un objeto), usar "
            "analyze_image en cambio."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Ruta a la imagen ya existente con el texto a extraer"},
            },
            "required": ["image_path"],
        },
    )

    def __init__(self, engine: OCREngine | None = None):
        # Carga perezosa: el engine inyectado (tests) evita descargar
        # modelos reales; sin inyectar, se arma recién en el primer
        # execute() — mismo patrón que ImageService/STTService (evita
        # importar rapidocr/onnxruntime solo por instanciar esta clase).
        self._engine = engine

    def _get_engine(self) -> OCREngine:
        if self._engine is None:
            self._engine = _build_default_engine()
        return self._engine

    def execute(self, image_path: str, **kwargs) -> Artifact:
        path = Path(image_path)
        if not path.is_file():
            return Artifact(
                modality="text", uri="",
                metadata={"status": "error", "stderr": f"No existe el archivo de imagen '{image_path}'"},
            )

        try:
            result = self._get_engine()(str(path))
        except Exception as e:  # noqa: BLE001 — cualquier falla de decodificación/inferencia es un error real
            logger.warning(f"Fallo extrayendo texto de '{image_path}': {e}")
            return Artifact(modality="text", uri="", metadata={"status": "error", "stderr": str(e)})

        lines = list(result.txts) if result.txts else []
        # Texto vacío es una respuesta REAL (la imagen no tiene texto
        # legible), no un error — decirlo explícito en vez de devolver
        # un summary vacío que el modelo podría malinterpretar como "no
        # se ejecutó nada" (mismo motivo que el comentario de
        # image_analysis.py sobre "status": "success" vs "summary").
        summary = "\n".join(lines) if lines else "(no se detectó ningún texto legible en la imagen)"

        return Artifact(
            modality="text", uri="",
            metadata={"summary": summary, "image_path": image_path},
        )

"""
Test de integración REAL de tool_integration/adapters/ocr.py::
TextExtractionTool — motor rapidocr real (latin_PP-OCRv5_mobile_rec,
ver settings.multimodal.ocr), no un doble (eso lo cubre
tests/test_ocr_tool.py).

Se salta si rapidocr no está instalado. La primera corrida descarga los
modelos ONNX (unos pocos MB, no comparable a diffusers/torch) — no es
instantáneo la primera vez, no es un bug si tarda un poco más esa vez.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rapidocr")

from PIL import Image, ImageDraw  # noqa: E402

from tool_integration.adapters.ocr import TextExtractionTool  # noqa: E402


def test_reads_real_text_from_a_synthetic_image(tmp_path):
    # Texto grande, alto contraste, fuente por defecto de Pillow — un
    # caso fácil a propósito (el objetivo es confirmar que el pipeline
    # completo funciona de punta a punta, no medir precisión de OCR en
    # condiciones difíciles).
    image_path = tmp_path / "texto.png"
    img = Image.new("RGB", (400, 100), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 30), "HELLO WORLD", fill="black")
    img.save(image_path)

    tool = TextExtractionTool()
    artifact = tool.execute(image_path=str(image_path))

    assert artifact.modality == "text"
    assert "status" not in artifact.metadata
    assert "HELLO WORLD" in artifact.metadata["summary"].upper()

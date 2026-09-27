"""
Tests de tool_integration/adapters/image_editing.py.

Usa imágenes sintéticas creadas con Pillow directamente (no depende de
image_gen.py/diffusers) — más rápido y aísla lo que se está probando acá
(la lógica de edición, no la generación). "crop" y "upscale" no requieren
ningún modelo; "remove_background" sí (rembg, se salta si no está
instalado o si la descarga del modelo falla).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from tool_integration.adapters.image_editing import ImageEditingTool
from utils.config import settings


@pytest.fixture
def source_image(tmp_path):
    from PIL import Image

    path = tmp_path / "source.png"
    Image.new("RGB", (200, 100), color=(255, 0, 0)).save(path)
    return path


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.image_editing, "artifact_dir", str(tmp_path / "edited"))
    return ImageEditingTool()


def test_missing_source_image_returns_clear_error(tool):
    artifact = tool.execute(image_path="/no/existe.png", operation="crop", box=[0, 0, 10, 10])

    assert artifact.metadata["status"] == "error"
    assert "No existe" in artifact.metadata["stderr"]


def test_unknown_operation_returns_clear_error(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="algo_inventado")

    assert artifact.metadata["status"] == "error"
    assert "desconocida" in artifact.metadata["stderr"]


# --- crop ---


def test_crop_produces_image_with_requested_dimensions(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="crop", box=[0, 0, 50, 40])

    assert artifact.modality == "image"
    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.size == (50, 40)
    assert artifact.metadata["operation"] == "crop"


def test_crop_without_box_returns_clear_error(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="crop")

    assert artifact.metadata["status"] == "error"
    assert "box" in artifact.metadata["stderr"]


def test_crop_with_wrong_number_of_box_values_returns_error(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="crop", box=[0, 0, 10])

    assert artifact.metadata["status"] == "error"


# --- upscale ---


def test_upscale_doubles_dimensions_by_default(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="upscale")

    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.size == (400, 200)
    assert artifact.metadata["ai_based"] is False
    assert artifact.metadata["method"] == "lanczos_resize"


def test_upscale_respects_custom_scale(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="upscale", scale=1.5)

    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.size == (300, 150)


# --- add_text (100% Pillow, sin modelo) ---


def test_add_text_without_text_returns_clear_error(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="add_text")

    assert artifact.metadata["status"] == "error"
    assert "text" in artifact.metadata["stderr"]


def test_add_text_produces_image_with_same_dimensions(tool, source_image):
    artifact = tool.execute(image_path=str(source_image), operation="add_text", text="EL COLIBRI")

    assert artifact.modality == "image"
    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.size == (200, 100)
    assert artifact.metadata["operation"] == "add_text"
    assert artifact.metadata["text"] == "EL COLIBRI"


def test_add_text_actually_changes_pixels_near_the_requested_position(tool, source_image):
    """Bug real que esto reemplaza: pedirle a kal 'escribí un título arriba'
    terminaba llamando a image_composition/overlay con la MISMA imagen como
    base y overlay — el resultado no cambiaba ni un píxel. Este test
    confirma que add_text sí modifica de verdad la franja pedida."""
    from PIL import Image

    artifact = tool.execute(
        image_path=str(source_image), operation="add_text", text="EL COLIBRI", text_position="top",
    )

    with Image.open(str(source_image)) as original, Image.open(artifact.uri) as result:
        original_top_strip = original.convert("RGB").crop((0, 0, 200, 30)).tobytes()
        result_top_strip = result.convert("RGB").crop((0, 0, 200, 30)).tobytes()

    assert original_top_strip != result_top_strip


def test_add_text_default_position_is_top():

    from tool_integration.adapters.image_editing import ImageEditingTool

    assert (
        ImageEditingTool.manifest.parameters_schema["properties"]["text_position"]["default"] == "top"
    )


def test_add_text_bottom_position_changes_bottom_strip_not_top(tool, source_image):
    from PIL import Image

    artifact = tool.execute(
        image_path=str(source_image), operation="add_text", text="PIE", text_position="bottom",
    )

    with Image.open(str(source_image)) as original, Image.open(artifact.uri) as result:
        original_top_strip = original.convert("RGB").crop((0, 0, 200, 20)).tobytes()
        result_top_strip = result.convert("RGB").crop((0, 0, 200, 20)).tobytes()
        original_bottom_strip = original.convert("RGB").crop((0, 80, 200, 100)).tobytes()
        result_bottom_strip = result.convert("RGB").crop((0, 80, 200, 100)).tobytes()

    assert original_top_strip == result_top_strip  # arriba no se tocó
    assert original_bottom_strip != result_bottom_strip  # abajo sí


# --- remove_background (requiere rembg + descarga del modelo u2net) ---


@pytest.fixture
def rembg_tool(tool):
    pytest.importorskip("rembg")
    try:
        # Fuerza la carga real ahora (no en medio de un test) para poder
        # saltar con un mensaje claro si el modelo no se puede descargar.
        from PIL import Image
        from rembg import remove

        remove(Image.new("RGB", (8, 8)))
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar/descargar el modelo de rembg: {e}")
    return tool


def test_remove_background_produces_image_with_alpha_channel(rembg_tool, source_image):
    artifact = rembg_tool.execute(image_path=str(source_image), operation="remove_background")

    assert artifact.modality == "image"
    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.mode in ("RGBA", "LA")  # tiene canal de transparencia
    assert artifact.metadata["operation"] == "remove_background"


# --- inpaint (requiere diffusers/torch + descarga del modelo de inpainting, ~5.5GB) ---


@pytest.fixture
def inpaint_source_image(tmp_path):
    from PIL import Image

    # Múltiplo de 8 (requisito de los pipelines de difusión) y color sólido
    # para poder verificar después que lo de AFUERA de la máscara no cambió.
    path = tmp_path / "inpaint_source.png"
    Image.new("RGB", (256, 256), color=(0, 128, 0)).save(path)
    return path


@pytest.fixture
def inpaint_tool(tool, monkeypatch):
    pytest.importorskip("diffusers")
    pytest.importorskip("torch")
    # Pasos bajos A PROPÓSITO para mantener el test acotado en tiempo — no
    # representativo de la calidad real (mismo criterio que sd-turbo con
    # pocos pasos en tests/test_image_gen.py). Este modelo NO es distilado,
    # así que incluso con pocos pasos puede tardar bastante en CPU.
    monkeypatch.setattr(settings.multimodal.image_editing, "inpaint_num_inference_steps", 4)
    try:
        tool._get_inpaint_pipeline()  # fuerza la carga/descarga ahora
    except Exception as e: # noqa: BLE001 — cualquier falla al cargar un modelo real (red, caché corrupta) debe saltear el test, no romperlo
        pytest.skip(f"No se pudo cargar/descargar el modelo de inpainting: {e}")
    return tool


def test_inpaint_without_box_returns_clear_error(tool, inpaint_source_image):
    artifact = tool.execute(image_path=str(inpaint_source_image), operation="inpaint", prompt="algo")

    assert artifact.metadata["status"] == "error"
    assert "box" in artifact.metadata["stderr"]


def test_inpaint_without_prompt_returns_clear_error(tool, inpaint_source_image):
    artifact = tool.execute(image_path=str(inpaint_source_image), operation="inpaint", box=[50, 50, 150, 150])

    assert artifact.metadata["status"] == "error"
    assert "prompt" in artifact.metadata["stderr"]


def test_inpaint_produces_image_with_same_dimensions(inpaint_tool, inpaint_source_image):
    artifact = inpaint_tool.execute(
        image_path=str(inpaint_source_image), operation="inpaint",
        box=[64, 64, 192, 192], prompt="a bright yellow circle",
    )

    assert artifact.modality == "image"
    from PIL import Image

    with Image.open(artifact.uri) as img:
        assert img.size == (256, 256)
    assert artifact.metadata["operation"] == "inpaint"
    assert artifact.metadata["box"] == [64, 64, 192, 192]


def test_inpaint_leaves_area_outside_mask_largely_unchanged(inpaint_tool, inpaint_source_image):
    from PIL import Image

    artifact = inpaint_tool.execute(
        image_path=str(inpaint_source_image), operation="inpaint",
        box=[64, 64, 192, 192], prompt="a bright yellow circle",
    )

    with Image.open(artifact.uri) as result_img:
        result_img = result_img.convert("RGB")
        corner_pixel = result_img.getpixel((5, 5))  # fuera de la región de máscara

    # La fuente era verde puro (0,128,0) fuera de la máscara; el inpainting
    # no debería tocarla de forma perceptible (se admite algo de variación
    # por el VAE, no un cambio de color completo a otra tonalidad).
    assert corner_pixel[1] > corner_pixel[0] and corner_pixel[1] > corner_pixel[2]


def test_manifest_tells_the_model_to_locate_the_object_with_vision_before_a_positional_inpaint():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-30): "hay dos orcas en la imagen,
    borra una de ellas" fue directo a un 'box' adivinado a ciegas, sin
    llamar nunca a analyze_image — el resultado terminó siendo una
    composición totalmente distinta, sin ninguna orca borrada de verdad.
    """
    description = ImageEditingTool.manifest.description
    assert "analyze_image" in description
    assert "ubicación aproximada" in description

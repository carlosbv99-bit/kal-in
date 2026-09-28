"""
Tests de tool_integration/adapters/image_composition.py.

Usa imágenes sintéticas creadas con Pillow directamente (no depende de
image_gen.py/diffusers) — 100% Pillow, sin ningún modelo, así que corre
siempre sin saltarse nada.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from PIL import Image

from tool_integration.adapters.image_composition import (
    ImageCompositionTool,
)
from utils.config import settings


@pytest.fixture
def tool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.multimodal.composition, "artifact_dir", str(tmp_path / "composed"))
    return ImageCompositionTool()


@pytest.fixture
def base_image(tmp_path):
    path = tmp_path / "base.png"
    Image.new("RGB", (200, 100), color=(0, 0, 255)).save(path)  # azul sólido
    return path


@pytest.fixture
def opaque_overlay(tmp_path):
    path = tmp_path / "overlay_opaque.png"
    Image.new("RGBA", (40, 20), color=(255, 0, 0, 255)).save(path)  # rojo opaco
    return path


@pytest.fixture
def transparent_overlay(tmp_path):
    # Centro rojo opaco, borde completamente transparente — para probar
    # que pegar respeta el canal alfa (no un rectángulo sólido).
    path = tmp_path / "overlay_transparent.png"
    img = Image.new("RGBA", (40, 40), color=(255, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            img.putpixel((x, y), (255, 0, 0, 255))
    img.save(path)
    return path


def test_unknown_operation_returns_clear_error(tool, base_image):
    artifact = tool.execute(operation="algo_inventado")

    assert artifact.metadata["status"] == "error"
    assert "desconocida" in artifact.metadata["stderr"]


# --- overlay ---


def test_overlay_missing_base_image_path_returns_clear_error(tool, opaque_overlay):
    artifact = tool.execute(operation="overlay", overlay_image_path=str(opaque_overlay))

    assert artifact.metadata["status"] == "error"
    assert "base_image_path" in artifact.metadata["stderr"]


def test_overlay_missing_overlay_image_path_returns_clear_error(tool, base_image):
    artifact = tool.execute(operation="overlay", base_image_path=str(base_image))

    assert artifact.metadata["status"] == "error"
    assert "overlay_image_path" in artifact.metadata["stderr"]


def test_overlay_nonexistent_base_image_returns_clear_error(tool, opaque_overlay):
    artifact = tool.execute(
        operation="overlay", base_image_path="/no/existe.png", overlay_image_path=str(opaque_overlay),
    )

    assert artifact.metadata["status"] == "error"
    assert "No existe" in artifact.metadata["stderr"]


def test_overlay_without_position_centers_it(tool, base_image, opaque_overlay):
    artifact = tool.execute(
        operation="overlay", base_image_path=str(base_image), overlay_image_path=str(opaque_overlay),
    )

    assert artifact.modality == "image"
    # base 200x100, overlay 40x20 -> centrado en ((200-40)//2, (100-20)//2) = (80, 40)
    assert artifact.metadata["position"] == [80, 40]
    with Image.open(artifact.uri) as result:
        assert result.getpixel((100, 50))[:3] == (255, 0, 0)  # centro: rojo del overlay
        assert result.getpixel((5, 5))[:3] == (0, 0, 255)  # esquina: azul del fondo, sin tocar


def test_overlay_with_explicit_position_places_it_there(tool, base_image, opaque_overlay):
    artifact = tool.execute(
        operation="overlay", base_image_path=str(base_image), overlay_image_path=str(opaque_overlay),
        position=[0, 0],
    )

    assert artifact.metadata["position"] == [0, 0]
    with Image.open(artifact.uri) as result:
        assert result.getpixel((5, 5))[:3] == (255, 0, 0)  # esquina: ahora sí rojo del overlay


def test_overlay_respects_transparency_instead_of_pasting_a_solid_rectangle(tool, base_image, transparent_overlay):
    artifact = tool.execute(
        operation="overlay", base_image_path=str(base_image), overlay_image_path=str(transparent_overlay),
        position=[0, 0],
    )

    with Image.open(artifact.uri) as result:
        # Centro del overlay (opaco): rojo.
        assert result.getpixel((20, 20))[:3] == (255, 0, 0)
        # Borde del overlay (transparente): debe seguir siendo el azul
        # del fondo, NO un rectángulo rojo sólido de 40x40.
        assert result.getpixel((2, 2))[:3] == (0, 0, 255)


def test_overlay_applies_scale_before_pasting(tool, base_image, opaque_overlay):
    artifact = tool.execute(
        operation="overlay", base_image_path=str(base_image), overlay_image_path=str(opaque_overlay),
        position=[0, 0], scale=2,
    )

    with Image.open(artifact.uri) as result:
        # Overlay original 40x20, escalado x2 = 80x40 -> (70, 30) debe
        # seguir siendo rojo (dentro del overlay agrandado).
        assert result.getpixel((70, 30))[:3] == (255, 0, 0)


# --- side_by_side ---


def test_side_by_side_requires_at_least_two_images(tool, base_image):
    artifact = tool.execute(operation="side_by_side", image_paths=[str(base_image)])

    assert artifact.metadata["status"] == "error"
    assert "image_paths" in artifact.metadata["stderr"]


def test_side_by_side_missing_image_returns_clear_error(tool, base_image):
    artifact = tool.execute(operation="side_by_side", image_paths=[str(base_image), "/no/existe.png"])

    assert artifact.metadata["status"] == "error"
    assert "No existe" in artifact.metadata["stderr"]


def test_side_by_side_horizontal_concatenates_matching_height(tool, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (100, 50), color=(255, 0, 0)).save(a)
    Image.new("RGB", (100, 50), color=(0, 255, 0)).save(b)

    artifact = tool.execute(operation="side_by_side", image_paths=[str(a), str(b)], direction="horizontal")

    with Image.open(artifact.uri) as result:
        assert result.height == 50
        assert result.width == 200
        assert result.getpixel((10, 10))[:3] == (255, 0, 0)
        assert result.getpixel((150, 10))[:3] == (0, 255, 0)


def test_side_by_side_vertical_concatenates_matching_width(tool, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (100, 50), color=(255, 0, 0)).save(a)
    Image.new("RGB", (100, 50), color=(0, 255, 0)).save(b)

    artifact = tool.execute(operation="side_by_side", image_paths=[str(a), str(b)], direction="vertical")

    with Image.open(artifact.uri) as result:
        assert result.width == 100
        assert result.height == 100
        assert result.getpixel((10, 10))[:3] == (255, 0, 0)
        assert result.getpixel((10, 75))[:3] == (0, 255, 0)


def test_manifest_declares_no_special_permissions():
    from sdk.permissions import Permission

    manifest = ImageCompositionTool.manifest
    assert manifest.permissions == frozenset({Permission.FILESYSTEM_READ})

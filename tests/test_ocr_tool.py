"""
Tests de tool_integration/adapters/ocr.py::TextExtractionTool. Sin red
real ni rapidocr/onnxruntime real: `engine` inyectado como un doble
mínimo (mismo patrón que ImageAnalysisTool con llm_client inyectado).
"""
from __future__ import annotations

from dataclasses import dataclass

from tool_integration.adapters.ocr import TextExtractionTool


@dataclass
class FakeOCRResult:
    txts: tuple[str, ...] | None


class FakeOCREngine:
    def __init__(self, result: FakeOCRResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def __call__(self, img_path: str):
        self.calls.append(img_path)
        if self.error is not None:
            raise self.error
        return self.result


def test_execute_returns_error_when_image_file_does_not_exist(tmp_path):
    tool = TextExtractionTool(engine=FakeOCREngine())

    result = tool.execute(image_path=str(tmp_path / "no_existe.png"))

    assert result.metadata["status"] == "error"
    assert "no_existe.png" in result.metadata["stderr"]


def test_execute_joins_recognized_lines_in_order(tmp_path):
    image_path = tmp_path / "letras.png"
    image_path.write_bytes(b"contenido-de-prueba-no-es-un-png-real")

    fake_engine = FakeOCREngine(result=FakeOCRResult(txts=("Step by step", "Heart to heart")))
    tool = TextExtractionTool(engine=fake_engine)

    result = tool.execute(image_path=str(image_path))

    assert result.modality == "text"
    # "summary", NUNCA "status": "success" — mismo motivo que
    # image_analysis.py (agent_loop.py::_artifact_to_observation()
    # toma la rama de run_code con "status": "success").
    assert "status" not in result.metadata
    assert result.metadata["summary"] == "Step by step\nHeart to heart"
    assert result.metadata["image_path"] == str(image_path)
    assert fake_engine.calls == [str(image_path)]


def test_execute_is_honest_when_no_text_is_detected(tmp_path):
    image_path = tmp_path / "foto.png"
    image_path.write_bytes(b"algo")

    tool = TextExtractionTool(engine=FakeOCREngine(result=FakeOCRResult(txts=())))

    result = tool.execute(image_path=str(image_path))

    assert "status" not in result.metadata
    assert "no se detectó ningún texto" in result.metadata["summary"]


def test_execute_returns_error_when_engine_raises(tmp_path):
    image_path = tmp_path / "foto.png"
    image_path.write_bytes(b"algo")

    fake_engine = FakeOCREngine(error=RuntimeError("no se pudo decodificar la imagen"))
    tool = TextExtractionTool(engine=fake_engine)

    result = tool.execute(image_path=str(image_path))

    assert result.metadata["status"] == "error"
    assert "no se pudo decodificar" in result.metadata["stderr"]


def test_manifest_declares_required_parameters():
    manifest = TextExtractionTool.manifest
    assert manifest.name == "extract_text_from_image"
    assert set(manifest.parameters_schema["required"]) == {"image_path"}


def test_engine_is_not_built_until_the_first_execute_call(monkeypatch, tmp_path):
    """
    Carga perezosa (mismo criterio que ImageService/STTService, ver
    default_tools.py): instanciar TextExtractionTool() sin inyectar un
    engine no debe importar/descargar nada de rapidocr todavía.
    """
    built = []

    def fake_build():
        built.append(True)
        return FakeOCREngine(result=FakeOCRResult(txts=("ok",)))

    monkeypatch.setattr("tool_integration.adapters.ocr._build_default_engine", fake_build)

    tool = TextExtractionTool()
    assert built == []

    image_path = tmp_path / "foto.png"
    image_path.write_bytes(b"algo")
    tool.execute(image_path=str(image_path))

    assert built == [True]

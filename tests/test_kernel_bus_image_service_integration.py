"""
Test de integración REAL del Kernel Service Bus + ImageService — sin
dobles de prueba en ningún lado: Docker real, socket real, y el
pipeline de SDXL-Turbo REAL (ya cacheado en este entorno de sesiones
anteriores, no baja nada nuevo). Corre skills/image_via_kernel/, la
skill de referencia sin NINGUNA dependencia de ML propia, de punta a
punta — confirma que puede generar una imagen real pidiéndosela al
kernel, sin cargar torch/diffusers ella misma.

Separado de test_sandboxed_skill.py::test_end_to_end_kernel_*
(que usan un servicio FALSO, rápido) porque este SÍ tarda lo mismo que
cualquier generación real de imagen — segundos a minutos según
hardware, no instantáneo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("diffusers")
pytest.importorskip("torch")

from kernel.lifecycle.docker_runner import DockerSandboxRunner
from kernel.lifecycle.executor import SandboxExecutor
from kernel.registry.sandboxed_skill import SandboxedSkillTool
from sdk.skill import ToolManifest
from tests.conftest import requires_docker

pytestmark = requires_docker

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "image_via_kernel"


def test_image_via_kernel_skill_generates_a_real_image(tmp_path):
    manifest = ToolManifest(
        name="image_via_kernel", description="genera una imagen vía el kernel", created_by="skill",
    )
    real_sandbox = SandboxExecutor(runner=DockerSandboxRunner())
    skill_tool = SandboxedSkillTool(
        manifest=manifest, skill_dir=SKILL_DIR, entry_point="tool:ImageViaKernelTool",
        image="python:3.11-slim", sandbox=real_sandbox, artifacts_root=tmp_path / "artifacts",
        kernel_services=["image.generate"],
        # kernel_instance NO se inyecta a propósito: usa el bus real
        # de producción (kernel.api.bus.kernel), con el ImageService
        # real — es justo lo que se quiere validar acá.
    )

    artifact = skill_tool.execute(prompt="a small red boat on a calm lake")

    assert artifact.modality == "image"
    path = Path(artifact.uri)
    assert path.exists()
    assert path.suffix == ".png"
    assert path.stat().st_size > 0
    assert artifact.metadata["prompt"] == "a small red boat on a calm lake"

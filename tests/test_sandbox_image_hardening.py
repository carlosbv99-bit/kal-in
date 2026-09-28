"""
Tests de la imagen minimizada de kernel/lifecycle/images/minimal/Dockerfile.

Se saltan si Docker no está disponible O si la imagen no ha sido
construida aún (docker build -t kal-sandbox-minimal:latest ... o
./scripts/build_sandbox_image.sh). No fuerza la construcción de la
imagen automáticamente porque eso pertenece a un paso explícito de
setup, no a la ejecución de tests.
"""
from __future__ import annotations

import docker
import pytest

from tests.conftest import requires_docker

MINIMAL_IMAGE = "kal-sandbox-minimal:latest"


def _minimal_image_built() -> bool:
    try:
        client = docker.from_env()
        client.images.get(MINIMAL_IMAGE)
        return True
    except Exception:  # noqa: BLE001 — best-effort: Docker no disponible degrada a False, nunca rompe
        return False


pytestmark = [
    requires_docker,
    pytest.mark.skipif(
        not _minimal_image_built(),
        reason=f"Imagen {MINIMAL_IMAGE} no construida. Correr scripts/build_sandbox_image.sh primero.",
    ),
]


def test_pip_is_not_available(runner):
    code = "import shutil\nprint('PIP_FOUND' if shutil.which('pip') else 'PIP_ABSENT')"
    result = runner.run(code, image=MINIMAL_IMAGE)
    assert "PIP_ABSENT" in result.stdout


def test_apt_and_dpkg_are_not_available(runner):
    code = (
        "import shutil\n"
        "tools = ['apt', 'apt-get', 'dpkg']\n"
        "found = [t for t in tools if shutil.which(t)]\n"
        "print('FOUND:', found)\n"
    )
    result = runner.run(code, image=MINIMAL_IMAGE)
    assert "FOUND: []" in result.stdout


def test_runs_as_non_root_fixed_uid(runner):
    code = "import os\nprint('UID:', os.getuid())"
    result = runner.run(code, image=MINIMAL_IMAGE)
    assert "UID: 1000" in result.stdout


def test_python_still_works_normally(runner):
    """Sanity check: quitar pip/apt no debe romper Python en sí."""
    code = "print(sum(range(100)))"
    result = runner.run(code, image=MINIMAL_IMAGE)
    assert result.status == "success"
    assert "4950" in result.stdout

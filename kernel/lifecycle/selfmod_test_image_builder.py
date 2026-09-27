"""
Construye (o reusa, cacheada) la imagen Docker que corre el test suite
de self-modification DENTRO del sandbox — ver
agent_core/self_modification.py::_run_tests().

Misma técnica que kernel/lifecycle/skill_image_builder.py (build una
vez, tag por hash de contenido, se reusa mientras no cambie): acá el
contenido que determina el tag es requirements-core.txt +
requirements-dev.txt (el mismo set liviano que ya usa CI, ver
.github/workflows/ci.yml — sin el stack de ML pesado; los tests que lo
necesitan se saltan solos con pytest.importorskip). Si esos archivos
cambian, el hash cambia y se dispara un build nuevo; si no, nunca se
reconstruye.

A diferencia de skill_image_builder.py, esta imagen SÍ deja pip
instalado — no hace falta la misma dureza que la imagen de ejecución
general (kernel/lifecycle/images/minimal/Dockerfile), porque acá lo que
se ejecuta con network_mode="none" es SIEMPRE un run de pytest, nunca
código arbitrario elegido por quien construyó la imagen.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import docker
from docker.errors import APIError, BuildError, DockerException, ImageNotFound

from utils.logger import get_logger

logger = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_REQUIREMENTS_FILES = ("requirements-core.txt", "requirements-dev.txt")

_DOCKERFILE = """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements-core.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-core.txt -r requirements-dev.txt
WORKDIR /workspace
"""


class SelfModTestImageBuildError(Exception):
    """La imagen del test runner de self-modification no se pudo construir."""


class SelfModTestImageBuilder:
    def __init__(self, client: docker.DockerClient | None = None, project_root: Path | None = None):
        try:
            self.client = client or docker.from_env()
        except DockerException as e:
            logger.error(f"No se pudo conectar al daemon de Docker: {e}")
            raise
        self.project_root = project_root or _PROJECT_ROOT

    def build_or_get_image(self) -> str:
        requirements_contents = [
            (self.project_root / name).read_text(encoding="utf-8") for name in _REQUIREMENTS_FILES
        ]
        tag = self._tag_for(requirements_contents)

        if self._image_exists(tag):
            logger.info(f"Imagen del test runner de self-modification ya existe ({tag}), se reusa sin reconstruir")
            return tag

        logger.info(f"Construyendo imagen del test runner de self-modification ({tag})...")
        context = _build_context_tar(_DOCKERFILE, dict(zip(_REQUIREMENTS_FILES, requirements_contents)))
        try:
            self.client.images.build(fileobj=context, custom_context=True, tag=tag, rm=True)
        except (BuildError, APIError) as e:
            raise SelfModTestImageBuildError(
                f"No se pudo construir la imagen del test runner de self-modification: {e}"
            ) from e
        return tag

    def _image_exists(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
            return True
        except ImageNotFound:
            return False

    @staticmethod
    def _tag_for(requirements_contents: list[str]) -> str:
        digest = hashlib.sha256("\n".join(requirements_contents).encode("utf-8")).hexdigest()[:12]
        return f"kal-selfmod-test-runner:{digest}"


def _build_context_tar(dockerfile_content: str, requirements_files: dict[str, str]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        _add_tar_file(tar, "Dockerfile", dockerfile_content)
        for name, content in requirements_files.items():
            _add_tar_file(tar, name, content)
    buf.seek(0)
    return buf


def _add_tar_file(tar: tarfile.TarFile, name: str, content: str) -> None:
    data = content.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))

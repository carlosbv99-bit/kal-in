"""
Construye (o reusa, cacheada) una imagen Docker derivada por skill,
para las skills que declaran `requirements` (paquetes de pip) en su
skill.yaml — ver kernel/registry/skills.py.

Misma técnica de endurecimiento que kernel/lifecycle/images/minimal/Dockerfile
(usuario 1000:1000 sin privilegios, sin pip/apt utilizables en
runtime): la diferencia es que acá se instala primero lo que la skill
declaró, y RECIÉN DESPUÉS se desinstala pip/apt — la imagen FINAL
sigue sin poder instalar nada más en tiempo de ejecución, pero ya tiene
adentro lo que la skill necesita. No se puede partir de
kal-sandbox-minimal:latest como base (esa imagen YA no tiene pip), por
eso esta plantilla parte de python:3.11-slim, igual que la minimal.

El build en sí (`docker build`) necesita red para bajar paquetes de
PyPI — igual que cualquier `pip install` normal. Esto es una excepción
de red acotada al momento de CONSTRUIR la imagen, nunca al de EJECUTAR
el contenedor de la skill (que sigue siendo network_mode="none" salvo
que la skill declare el permiso NETWORK) — mismo principio ya usado en
error_handling/strategies.py::ImportErrorStrategy.
"""
from __future__ import annotations

import hashlib
import io
import tarfile

import docker
from docker.errors import APIError, BuildError, DockerException, ImageNotFound

from utils.logger import get_logger

logger = get_logger(__name__)

# Sin requirements declarados: se usa esta imagen ya endurecida tal
# cual (ver kernel/lifecycle/images/minimal/Dockerfile, scripts/build_sandbox_image.sh),
# sin build nuevo. Si no existe todavía en este host, se cae al default
# de DockerSandboxRunner (SANDBOX_IMAGE) — build_or_get_image() nunca
# la exige como precondición dura para el caso sin dependencias.
MINIMAL_IMAGE = "kal-sandbox-minimal:latest"

_DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim

RUN groupadd -g 1000 sandboxuser && \\
    useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin sandboxuser

{requirements_install}
RUN python3 -m pip uninstall pip setuptools wheel -y 2>/dev/null || true && \\
    rm -rf /usr/local/lib/python3.11/ensurepip \\
           /usr/local/lib/python3.11/site-packages/pip* \\
           /usr/local/lib/python3.11/site-packages/setuptools* \\
           /usr/local/lib/python3.11/site-packages/wheel* \\
           /usr/local/bin/pip*

RUN rm -rf /usr/bin/apt* /usr/bin/dpkg* /usr/lib/apt /usr/lib/dpkg \\
           /var/lib/dpkg /var/lib/apt /etc/apt /usr/sbin/dpkg*

RUN find / -xdev -perm /6000 -type f -exec chmod a-s {{}} \\; 2>/dev/null || true

WORKDIR /workspace
USER sandboxuser
"""


class SkillImageBuildError(Exception):
    """La imagen derivada de una skill no se pudo construir."""


class SkillImageBuilder:
    def __init__(self, client: docker.DockerClient | None = None):
        try:
            self.client = client or docker.from_env()
        except DockerException as e:
            logger.error(f"No se pudo conectar al daemon de Docker: {e}")
            raise

    def build_or_get_image(self, skill_name: str, requirements: list[str]) -> str:
        if not requirements:
            return MINIMAL_IMAGE if self._image_exists(MINIMAL_IMAGE) else "python:3.11-slim"

        tag = self._tag_for(skill_name, requirements)
        if self._image_exists(tag):
            logger.info(f"Imagen de la skill '{skill_name}' ya existe ({tag}), se reusa sin reconstruir")
            return tag

        logger.info(f"Construyendo imagen para la skill '{skill_name}' ({tag}), requirements={requirements}")
        dockerfile = _DOCKERFILE_TEMPLATE.format(
            requirements_install="COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
        )
        requirements_txt = "\n".join(requirements) + "\n"
        context = _build_context_tar(dockerfile, requirements_txt)
        try:
            self.client.images.build(fileobj=context, custom_context=True, tag=tag, rm=True)
        except (BuildError, APIError) as e:
            raise SkillImageBuildError(
                f"No se pudo construir la imagen para la skill '{skill_name}': {e}"
            ) from e
        return tag

    def _image_exists(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
            return True
        except ImageNotFound:
            return False

    @staticmethod
    def _tag_for(skill_name: str, requirements: list[str]) -> str:
        # Hash de las requirements (ordenadas, para que el orden en el
        # YAML no invalide la caché) -> si no cambian, no se reconstruye
        # nunca; si cambian, el tag cambia y dispara un build nuevo.
        digest = hashlib.sha256("\n".join(sorted(requirements)).encode("utf-8")).hexdigest()[:12]
        safe_name = skill_name.lower().replace("_", "-")
        return f"kal-skill-{safe_name}:{digest}"


def _build_context_tar(dockerfile_content: str, requirements_content: str) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        _add_tar_file(tar, "Dockerfile", dockerfile_content)
        _add_tar_file(tar, "requirements.txt", requirements_content)
    buf.seek(0)
    return buf


def _add_tar_file(tar: tarfile.TarFile, name: str, content: str) -> None:
    data = content.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))

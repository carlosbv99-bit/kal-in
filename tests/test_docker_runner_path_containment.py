"""
Tests de kernel/lifecycle/docker_runner.py::DockerSandboxRunner._join_within()
— defensa en profundidad contra `workspace_files`/`output_dir` con
claves tipo "../../etc/algo" (footgun latente señalado en la auditoría
externa del 2026-09-26, relacionado a A-1): hoy ningún llamador real
alimenta esto con input no confiable, pero un llamador futuro o un
refactor sí podría.
"""
from __future__ import annotations

import pytest

from kernel.lifecycle.docker_runner import DockerSandboxRunner


def test_a_normal_relative_path_is_allowed(tmp_path):
    result = DockerSandboxRunner._join_within(tmp_path, "sub/archivo.txt")
    assert result == (tmp_path / "sub" / "archivo.txt").resolve()


def test_path_traversal_via_dotdot_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="fuera del workdir"):
        DockerSandboxRunner._join_within(tmp_path, "../../../../etc/passwd")


def test_an_absolute_path_that_escapes_base_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="fuera del workdir"):
        DockerSandboxRunner._join_within(tmp_path, "/etc/passwd")


def test_prepare_workdir_rejects_a_workspace_files_key_that_escapes(tmp_path):
    with pytest.raises(ValueError, match="fuera del workdir"):
        DockerSandboxRunner._prepare_workdir(
            tmp_path, "print('hola')", {"../../fuera_del_workdir.txt": "contenido"}
        )


def test_run_rejects_an_output_dir_that_escapes_the_workdir(monkeypatch):
    """
    No hace falta Docker real para este chequeo: la validación de
    output_dir corre ANTES de tocar containers.run().
    """
    runner = DockerSandboxRunner()

    result = runner.run("print('hola')", output_dir="../../fuera_del_workdir")

    assert result.status == "error"
    assert "fuera del workdir" in result.stderr


# --- M-5 (auditoría externa de kal, 2026-09-27, portado acá vía
# scripts/check_kernel_drift.py): extra_mounts sin validar ---


def test_extra_mounts_with_a_relative_host_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="host_path"):
        DockerSandboxRunner._validate_extra_mounts({"relativo/no/absoluto": "/workspace/.kal"})


def test_extra_mounts_with_a_nonexistent_host_path_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="host_path"):
        DockerSandboxRunner._validate_extra_mounts({str(tmp_path / "no_existe"): "/workspace/.kal"})


def test_extra_mounts_with_a_container_path_outside_workspace_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="container_path"):
        DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/etc"})


def test_extra_mounts_with_a_container_path_that_is_workspace_itself_is_allowed(tmp_path):
    # No debería lanzar — /workspace en sí (no solo un subdirectorio) es válido.
    DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/workspace"})


def test_extra_mounts_matching_the_one_real_caller_is_allowed(tmp_path):
    # Mismo patrón exacto que SandboxedSkillTool: tempdir real del host -> /workspace/.kal
    DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/workspace/.kal"})


def test_extra_mounts_matching_self_modification_is_allowed(tmp_path):
    """
    BUG REAL ENCONTRADO PORTANDO M-5 A kal-in (2026-09-28): la primera
    versión de este fix solo aceptaba /workspace, hardcodeado — kal (de
    donde se portó) es kernel puro y no tiene
    agent_core/self_modification.py, así que nunca vio este segundo
    llamador real. Rompía DE VERDAD la suite de self-modification acá
    (extra_mounts={root: "/project"}), detectado corriendo la suite
    completa antes de commitear.
    """
    DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/project"})


def test_run_rejects_an_extra_mounts_with_an_invalid_host_path():
    runner = DockerSandboxRunner()

    result = runner.run("print('hola')", extra_mounts={"relativo": "/workspace/.kal"})

    assert result.status == "error"
    assert "host_path" in result.stderr


def test_run_rejects_an_extra_mounts_with_a_container_path_outside_workspace(tmp_path):
    runner = DockerSandboxRunner()

    result = runner.run("print('hola')", extra_mounts={str(tmp_path): "/etc"})

    assert result.status == "error"
    assert "container_path" in result.stderr


def test_extra_mounts_container_path_with_dotdot_that_escapes_workspace_is_rejected(tmp_path):
    """
    BUG REAL ENCONTRADO EN LA RE-AUDITORÍA de kal (2026-09-28), en el
    propio fix de M-5: comparar
    `PurePosixPath(container_path).is_relative_to("/workspace")`
    SIN normalizar primero es una comparación LÉXICA de segmentos, no
    resuelve `..` — verificado con un PoC real:
    `PurePosixPath("/workspace/../etc/passwd").is_relative_to("/workspace")`
    da `True` (los primeros dos segmentos coinciden), aunque la ruta
    REAL resuelta esté fuera de /workspace por completo. Acá se
    normaliza con `posixpath.normpath()` ANTES de comparar — cierra
    esta evasión específica sin tocar filesystem (container_path nunca
    existe en el host, solo tiene sentido dentro del contenedor).
    """
    with pytest.raises(ValueError, match="container_path"):
        DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/workspace/../etc/passwd"})


def test_extra_mounts_container_path_with_a_workspace_prefix_trick_is_rejected(tmp_path):
    """
    Ancla adicional: "/workspace-evil" empieza con el string
    "/workspace" pero NO es un descendiente real — is_relative_to()
    sobre Path/PurePosixPath ya compara por SEGMENTOS (no por prefijo
    de string), así que este caso ya estaba cubierto antes del fix de
    normalización de arriba; se prueba igual como ancla contra una
    futura regresión que cambiara la comparación a un prefijo de texto.
    """
    with pytest.raises(ValueError, match="container_path"):
        DockerSandboxRunner._validate_extra_mounts({str(tmp_path): "/workspace-evil"})

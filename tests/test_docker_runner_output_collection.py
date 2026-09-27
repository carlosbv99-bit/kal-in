"""
Tests de kernel/lifecycle/docker_runner.py::DockerSandboxRunner.
_collect_output_files() — sin Docker real, prueba directa de la
función estática (no depende de un contenedor corriendo).
"""
from __future__ import annotations

import os

from kernel.lifecycle.docker_runner import DockerSandboxRunner


def test_collects_regular_files_normally(tmp_path):
    output_path = tmp_path / "output"
    output_path.mkdir()
    (output_path / "resultado.txt").write_bytes(b"contenido real")

    collected = DockerSandboxRunner._collect_output_files(output_path)

    assert collected == {"resultado.txt": b"contenido real"}


def test_ignores_a_symlink_pointing_outside_the_output_dir(tmp_path):
    """
    K-2 (auditoría externa Likay-OS, 2026-09-26): código NO CONFIABLE
    corriendo dentro del contenedor puede crear un symlink en el bind
    mount de salida (rw) apuntando a cualquier archivo del HOST — sin
    este fix, el proceso host lo seguía y cargaba ese contenido
    arbitrario en memoria (lectura arbitraria de archivos del host).
    """
    output_path = tmp_path / "output"
    output_path.mkdir()
    secreto = tmp_path / "secreto_del_host.txt"
    secreto.write_bytes(b"informacion sensible del host, nunca deberia salir del host")

    os.symlink(secreto, output_path / "resultado.txt")

    collected = DockerSandboxRunner._collect_output_files(output_path)

    assert collected == {}


def test_collects_regular_files_alongside_an_ignored_symlink(tmp_path):
    output_path = tmp_path / "output"
    output_path.mkdir()
    (output_path / "legitimo.txt").write_bytes(b"esto si es del propio contenedor")
    secreto = tmp_path / "secreto_del_host.txt"
    secreto.write_bytes(b"informacion sensible")
    os.symlink(secreto, output_path / "malicioso.txt")

    collected = DockerSandboxRunner._collect_output_files(output_path)

    assert collected == {"legitimo.txt": b"esto si es del propio contenedor"}


def test_ignores_a_symlinked_subdirectory_pointing_outside(tmp_path):
    """
    Defensa adicional: aunque el archivo hoja no sea un symlink, un
    directorio INTERMEDIO que sí lo sea (apuntando fuera de
    output_path) tampoco debe filtrar contenido del host.
    """
    output_path = tmp_path / "output"
    output_path.mkdir()
    fuera = tmp_path / "fuera_del_output"
    fuera.mkdir()
    (fuera / "otro_secreto.txt").write_bytes(b"tambien sensible")

    os.symlink(fuera, output_path / "subcarpeta", target_is_directory=True)

    collected = DockerSandboxRunner._collect_output_files(output_path)

    assert collected == {}

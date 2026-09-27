"""
Tests de kernel/lifecycle/skill_image_builder.py — imagen Docker derivada por
skill, para las que declaran `requirements` (paquetes de pip) en su
skill.yaml.

Los tests de tag/caché no necesitan Docker real (lógica pura). Los de
build sí (`requires_docker`), y usan un paquete pequeño y sin
dependencias (six) para no volver la suite impráctica.
"""
from __future__ import annotations

import docker
import pytest

from kernel.lifecycle.skill_image_builder import (
    MINIMAL_IMAGE,
    SkillImageBuilder,
    SkillImageBuildError,
)
from tests.conftest import requires_docker

# --- Tag determinístico (lógica pura, sin Docker) ---


def test_tag_is_deterministic_for_same_requirements():
    tag1 = SkillImageBuilder._tag_for("mi_skill", ["requests==2.31.0", "pyyaml==6.0"])
    tag2 = SkillImageBuilder._tag_for("mi_skill", ["requests==2.31.0", "pyyaml==6.0"])
    assert tag1 == tag2


def test_tag_is_order_independent():
    """El orden en el YAML no debería invalidar la caché."""
    tag1 = SkillImageBuilder._tag_for("mi_skill", ["requests==2.31.0", "pyyaml==6.0"])
    tag2 = SkillImageBuilder._tag_for("mi_skill", ["pyyaml==6.0", "requests==2.31.0"])
    assert tag1 == tag2


def test_tag_differs_when_requirements_change():
    tag1 = SkillImageBuilder._tag_for("mi_skill", ["requests==2.31.0"])
    tag2 = SkillImageBuilder._tag_for("mi_skill", ["requests==2.32.0"])
    assert tag1 != tag2


def test_tag_differs_by_skill_name():
    tag1 = SkillImageBuilder._tag_for("skill_a", ["requests==2.31.0"])
    tag2 = SkillImageBuilder._tag_for("skill_b", ["requests==2.31.0"])
    assert tag1 != tag2


def test_tag_starts_with_kal_skill_prefix():
    tag = SkillImageBuilder._tag_for("Mi_Skill", [])
    assert tag.startswith("kal-skill-mi-skill:")


# --- Sin requirements: no construye nada ---


@requires_docker
def test_no_requirements_returns_minimal_or_slim_without_building(monkeypatch):
    builder = SkillImageBuilder()
    build_calls = []
    monkeypatch.setattr(builder.client.images, "build", lambda **kw: build_calls.append(kw))

    image = builder.build_or_get_image("system_info", [])

    assert image in (MINIMAL_IMAGE, "python:3.11-slim")
    assert build_calls == []


# --- Build real (requiere Docker + red para pip install) ---


@requires_docker
def test_builds_image_with_declared_dependency():
    builder = SkillImageBuilder()
    image = builder.build_or_get_image("test_qr_ref", ["six==1.16.0"])

    try:
        client = docker.from_env()
        output = client.containers.run(
            image, command=["python", "-c", "import six; print('SIX_OK', six.__version__)"],
            remove=True,
        )
        assert b"SIX_OK 1.16.0" in output
    finally:
        docker.from_env().images.remove(image, force=True)


@requires_docker
def test_second_call_with_same_requirements_reuses_cached_image(monkeypatch):
    builder = SkillImageBuilder()
    try:
        image1 = builder.build_or_get_image("test_qr_cache", ["six==1.16.0"])

        build_calls = []
        real_build = builder.client.images.build
        monkeypatch.setattr(
            builder.client.images, "build",
            lambda **kw: (build_calls.append(kw), real_build(**kw))[1],
        )

        image2 = builder.build_or_get_image("test_qr_cache", ["six==1.16.0"])

        assert image1 == image2
        assert build_calls == []  # no se reconstruyó
    finally:
        docker.from_env().images.remove(image1, force=True)


@requires_docker
def test_build_failure_for_nonexistent_package_raises_clear_error():
    builder = SkillImageBuilder()
    with pytest.raises(SkillImageBuildError):
        builder.build_or_get_image("test_qr_broken", ["este-paquete-no-existe-seguro-12345"])

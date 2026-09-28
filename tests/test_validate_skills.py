"""
Tests de scripts/validate_skills.py (Fase C del plan de comunidad):
el chequeo de integridad que corre en CI antes de mergear un PR que
toque skills/**.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from validate_skills import validate_all_skills

from kernel.registry.skill_signing import SkillSigner

_SKILL_YAML_TEMPLATE = """name: {name}
description: "una skill de prueba"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""


def _make_skill(skills_dir, name):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name), encoding="utf-8")
    (skill_dir / "tool.py").write_text("class GreetTool:\n    pass\n", encoding="utf-8")
    return skill_dir


def test_empty_skills_dir_has_no_errors(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    assert validate_all_skills(skills_dir) == []


def test_nonexistent_skills_dir_has_no_errors(tmp_path):
    assert validate_all_skills(tmp_path / "no_existe") == []


def test_unsigned_skill_is_an_error(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter")

    errors = validate_all_skills(skills_dir)

    assert len(errors) == 1
    assert "greeter" in errors[0]
    assert "unsigned" in errors[0]


def test_signed_and_verified_skill_has_no_errors(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = _make_skill(skills_dir, "greeter")
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    assert validate_all_skills(skills_dir) == []


def test_tampered_skill_is_an_error(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = _make_skill(skills_dir, "greeter")
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)
    (skill_dir / "tool.py").write_text("class GreetTool:\n    pass\n# alterado\n", encoding="utf-8")

    errors = validate_all_skills(skills_dir)

    assert len(errors) == 1
    assert "tampered" in errors[0]


def test_broken_manifest_is_an_error_identifying_the_folder(tmp_path):
    skills_dir = tmp_path / "skills"
    broken_dir = skills_dir / "roto"
    broken_dir.mkdir(parents=True)
    (broken_dir / "skill.yaml").write_text("esto: [no cierra\n", encoding="utf-8")

    errors = validate_all_skills(skills_dir)

    assert len(errors) == 1
    assert "roto" in errors[0]


def test_one_bad_skill_does_not_hide_others(tmp_path):
    skills_dir = tmp_path / "skills"
    good_dir = _make_skill(skills_dir, "greeter")
    SkillSigner(key_dir=tmp_path / "keys").write_signature(good_dir)
    _make_skill(skills_dir, "sin_firmar")

    errors = validate_all_skills(skills_dir)

    assert len(errors) == 1
    assert "sin_firmar" in errors[0]


def test_real_project_skills_are_all_verified():
    """
    Corre el chequeo real sobre skills/ del proyecto (las 6 ya
    firmadas desde la Fase A) — confirma que el propio repo pasaría
    hoy el chequeo de CI que se está agregando en esta Fase C.
    """
    from kernel.registry.skills import DEFAULT_SKILLS_DIR

    assert validate_all_skills(DEFAULT_SKILLS_DIR) == []

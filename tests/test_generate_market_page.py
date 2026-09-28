"""
Tests de scripts/generate_market_page.py (Fase B del plan de
comunidad): generación de la página estática del market a partir de
las skills reales del repo — sin servidor, sin JS.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from generate_market_page import render_market_html

from kernel.registry.skill_signing import SkillSigner

_SKILL_YAML_TEMPLATE = """name: {name}
description: "{description}"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""


def _make_skill(skills_dir, name, description="una skill de prueba"):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name, description=description), encoding="utf-8")
    (skill_dir / "tool.py").write_text("class GreetTool:\n    pass\n", encoding="utf-8")
    return skill_dir


def test_empty_skills_dir_renders_placeholder(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    html_out = render_market_html(skills_dir)

    assert "No skills published yet" in html_out
    assert "0 skill(s) published" in html_out


def test_nonexistent_skills_dir_renders_placeholder(tmp_path):
    html_out = render_market_html(tmp_path / "no_existe")
    assert "No skills published yet" in html_out


def test_unsigned_skill_shows_unsigned_badge(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter", description="saluda")

    html_out = render_market_html(skills_dir)

    assert "greeter" in html_out
    assert "v0.1.0" in html_out
    assert "saluda" in html_out
    assert 'badge unsigned">unsigned' in html_out
    assert "1 skill(s) published" in html_out


def test_signed_skill_shows_verified_badge(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = _make_skill(skills_dir, "greeter")
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    html_out = render_market_html(skills_dir)

    assert "signature verified" in html_out
    assert 'badge unsigned">unsigned' not in html_out  # el badge en sí, no la clase CSS (que siempre está definida)


def test_install_command_is_shown_per_skill(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter")

    html_out = render_market_html(skills_dir)

    assert "scripts/install_from_market.py greeter" in html_out


def test_html_special_characters_are_escaped(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter", description="edita <img> & cosas")

    html_out = render_market_html(skills_dir)

    assert "<img>" not in html_out
    assert "&lt;img&gt;" in html_out


def test_multiple_skills_all_appear(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter")
    _make_skill(skills_dir, "qr_maker")

    html_out = render_market_html(skills_dir)

    assert "greeter" in html_out
    assert "qr_maker" in html_out
    assert "2 skill(s) published" in html_out


def test_broken_manifest_is_skipped_not_fatal(tmp_path):
    skills_dir = tmp_path / "skills"
    _make_skill(skills_dir, "greeter")
    broken_dir = skills_dir / "roto"
    broken_dir.mkdir()
    (broken_dir / "skill.yaml").write_text("esto: [no cierra\n", encoding="utf-8")

    html_out = render_market_html(skills_dir)

    assert "greeter" in html_out
    assert "1 skill(s) published" in html_out

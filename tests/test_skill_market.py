"""
Tests de kernel/registry/skill_market.py (Fase A del plan de
comunidad): listar/traer skills desde un "market" Git remoto.

Usa un repositorio Git LOCAL sintético bajo tmp_path como "market" —
`git clone` funciona igual contra un path local que contra una URL
remota real, así que no hace falta red para probar la lógica.
"""
from __future__ import annotations

import subprocess

import pytest

from kernel.registry.skill_market import (
    MarketError,
    fetch_skill_from_market,
    list_market_skills,
)
from kernel.registry.skill_signing import SkillSigner, verify_skill_signature

_SKILL_YAML_TEMPLATE = """name: {name}
description: "{description}"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""

_TOOL_SOURCE = "class GreetTool:\n    pass\n"


def _add_skill(repo_dir, name, description="una skill de prueba"):
    skill_dir = repo_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name, description=description), encoding="utf-8")
    (skill_dir / "tool.py").write_text(_TOOL_SOURCE, encoding="utf-8")
    return skill_dir


def _git(repo_dir, *args):
    subprocess.run(["git", *args], cwd=str(repo_dir), check=True, capture_output=True)


def _make_market_repo(tmp_path, skill_names=("greeter",)):
    repo_dir = tmp_path / "market_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    for name in skill_names:
        _add_skill(repo_dir, name)
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "skills de prueba")
    return repo_dir


def test_list_market_skills_returns_manifests(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter", "qr_maker"))

    manifests = list_market_skills(str(repo_dir))

    names = sorted(m.name for m in manifests)
    assert names == ["greeter", "qr_maker"]


def test_list_market_skills_empty_when_no_skills_dir(tmp_path):
    repo_dir = tmp_path / "empty_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "README.md").write_text("nada de skills acá\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "sin skills")

    assert list_market_skills(str(repo_dir)) == []


def test_fetch_skill_from_market_copies_files(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))
    dest = tmp_path / "destino"

    fetch_skill_from_market(str(repo_dir), "greeter", dest)

    assert (dest / "skill.yaml").exists()
    assert (dest / "tool.py").read_text(encoding="utf-8") == _TOOL_SOURCE
    assert not (dest / ".git").exists()  # nunca se copian metadatos de git


def test_fetch_skill_from_market_raises_for_unknown_skill(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))
    dest = tmp_path / "destino"

    with pytest.raises(MarketError, match="greeter"):
        fetch_skill_from_market(str(repo_dir), "no_existe", dest)


def test_clone_raises_clear_market_error_for_unknown_ref(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))

    with pytest.raises(MarketError):
        list_market_skills(str(repo_dir), ref="rama-que-no-existe")


# --- Política: una skill remota debe estar firmada y verificar (Fase A) ---
# Se prueba la interacción real skill_market.py + skill_signing.py, no el
# script CLI (sin test automatizado, mismo criterio que scripts/enable_skill.py).


def test_unsigned_skill_from_market_fails_verification(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))
    dest = tmp_path / "destino"

    fetch_skill_from_market(str(repo_dir), "greeter", dest)

    assert verify_skill_signature(dest) == "unsigned"


def test_verified_skill_from_market_passes_verification(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))
    SkillSigner(key_dir=tmp_path / "keys").write_signature(repo_dir / "skills" / "greeter")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "firmada")

    dest = tmp_path / "destino"
    fetch_skill_from_market(str(repo_dir), "greeter", dest)

    assert verify_skill_signature(dest) == "verified"


def test_tampered_skill_from_market_fails_verification(tmp_path):
    repo_dir = _make_market_repo(tmp_path, skill_names=("greeter",))
    skill_dir = repo_dir / "skills" / "greeter"
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "firmada")

    # Alterar DESPUÉS de firmar y commitear — el paquete cambió desde que se firmó.
    (skill_dir / "tool.py").write_text(_TOOL_SOURCE + "\n# alterado\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "alterada tras firmar")

    dest = tmp_path / "destino"
    fetch_skill_from_market(str(repo_dir), "greeter", dest)

    assert verify_skill_signature(dest) == "tampered"

"""
Tests de scripts/install_from_market.py.

VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (A-1 en kal,
2026-09-27, portada acá vía scripts/check_kernel_drift.py):
args.skill_name (input de línea de comandos) se usaba SIN NINGUNA
sanitización para armar local_dest = DEFAULT_SKILLS_DIR / skill_name —
un --skill-name "../.algo" escribía y HABILITABA una skill fuera de
skills/ por completo. Mismo patrón exacto que K-1 (skills.py) y K-6
(registry.py/versioning.py), solo que en el instalador de market.

PoC reproducido con un repo "market" LOCAL sintético (git clone
funciona igual contra un path local que contra una URL real, mismo
patrón que tests/test_skill_market.py) — sin red.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import install_from_market

from kernel.registry.skill_signing import SkillSigner

_SKILL_YAML_TEMPLATE = """name: {name}
description: "una skill de prueba"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""

_TOOL_SOURCE = "class GreetTool:\n    pass\n"


def _git(repo_dir, *args):
    subprocess.run(["git", *args], cwd=str(repo_dir), check=True, capture_output=True)


def _write_skill(skill_dir, name, key_dir):
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name), encoding="utf-8")
    (skill_dir / "tool.py").write_text(_TOOL_SOURCE, encoding="utf-8")
    SkillSigner(key_dir=key_dir).write_signature(skill_dir)


def _make_market_repo_with_traversal_skill(tmp_path):
    """
    Repo "market" con una skill LEGÍTIMA en skills/greeter/, más una
    "maliciosa" en un directorio HERMANO de skills/ (no adentro) — el
    ataque real: pedir --skill-name "../.audit_pwned" hace que
    fetch_skill_from_market busque en repo_dir/skills/../.audit_pwned,
    que resuelve a repo_dir/.audit_pwned (un nivel arriba de skills/).
    """
    repo_dir = tmp_path / "market_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")

    key_dir = tmp_path / "attacker_keys"
    _write_skill(repo_dir / "skills" / "greeter", "greeter", key_dir)
    _write_skill(repo_dir / ".audit_pwned", "pwned", key_dir)

    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "market con un intento de path traversal")
    return repo_dir


def test_traversal_skill_name_is_rejected_before_touching_the_network(tmp_path, monkeypatch, capsys):
    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "../.audit_pwned", "--market", str(market_repo), "--yes"],
    )

    try:
        install_from_market.main()
        raised = False
    except SystemExit:
        raised = True

    assert raised, "debería rechazar el nombre inválido con SystemExit"
    assert "inválido" in capsys.readouterr().out
    # Nada se escribió ni dentro ni fuera de skills/ — se rechazó ANTES
    # de tocar la red (fetch_skill_from_market ni se llegó a invocar).
    assert not (workdir / "skills").exists()
    assert not (workdir / ".audit_pwned").exists()
    assert not (workdir.parent / ".audit_pwned").exists()


def test_a_legitimate_skill_name_still_installs_normally(tmp_path, monkeypatch):
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion_legitima"
    workdir.mkdir()
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "greeter", "--market", str(market_repo), "--yes"],
    )

    install_from_market.main()

    assert (workdir / "skills" / "greeter" / "tool.py").exists()

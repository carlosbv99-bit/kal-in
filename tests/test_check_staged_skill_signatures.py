"""
Tests de scripts/check_staged_skill_signatures.py — el hook de
pre-commit local que complementa validate-skills.yml (CI): mismo
chequeo, pero corrido ANTES de que el commit exista, para no depender
del viaje de ida y vuelta a CI (ver el docstring del script para el
bug real que lo motivó: un ruff --fix rompió 7 firmas sin que nada lo
avisara hasta que la suite completa falló).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from check_staged_skill_signatures import _skill_dirs_with_staged_content_changes

from kernel.registry.skill_signing import SkillSigner

_SKILL_YAML_TEMPLATE = """name: {name}
description: "una skill de prueba"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""


def _make_skill(skills_dir: Path, name: str, sign: bool = True) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name), encoding="utf-8")
    (skill_dir / "tool.py").write_text("class GreetTool:\n    pass\n", encoding="utf-8")
    if sign:
        SkillSigner(key_dir=skills_dir / "keys").write_signature(skill_dir)
    return skill_dir


# --- _skill_dirs_with_staged_content_changes(): lógica pura, sin git real ---


def test_ignores_staged_files_outside_skills_dir():
    staged = ["agent_core/orchestrator.py", "docs/HISTORY.md"]
    assert _skill_dirs_with_staged_content_changes(staged, Path("skills")) == set()


def test_ignores_a_bare_skills_dir_path_with_no_file():
    staged = ["skills/qr_code"]
    assert _skill_dirs_with_staged_content_changes(staged, Path("skills")) == set()


def test_detects_a_skill_with_a_staged_content_file():
    staged = ["skills/qr_code/tool.py"]
    assert _skill_dirs_with_staged_content_changes(staged, Path("skills")) == {"qr_code"}


def test_ignores_skill_sig_itself_as_the_only_staged_file():
    """Un skill.sig staged SOLO (p.ej. alguien re-firmando sin tocar
    nada más) no necesita reverificarse — no puede invalidar su propia
    firma."""
    staged = ["skills/qr_code/skill.sig"]
    assert _skill_dirs_with_staged_content_changes(staged, Path("skills")) == set()


def test_detects_multiple_skills_and_deduplicates():
    staged = [
        "skills/qr_code/tool.py",
        "skills/qr_code/skill.yaml",
        "skills/system_info/tool.py",
    ]
    assert _skill_dirs_with_staged_content_changes(staged, Path("skills")) == {"qr_code", "system_info"}


# --- main(): de punta a punta, con skills reales bajo un cwd temporal
# (sin git real: _staged_files() mockeado con rutas relativas, igual a
# como git diff --cached --name-only las reporta de verdad) ---


def _run_main_with_skills_dir(tmp_path, monkeypatch, staged_relative_paths):
    import check_staged_skill_signatures as module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "DEFAULT_SKILLS_DIR", Path("skills"))
    monkeypatch.setattr(module, "_staged_files", lambda: staged_relative_paths)
    return module.main()


def test_main_passes_when_all_staged_skills_verify(tmp_path, monkeypatch):
    _make_skill(tmp_path / "skills", "buena", sign=True)
    assert _run_main_with_skills_dir(tmp_path, monkeypatch, ["skills/buena/tool.py"]) == 0


def test_main_fails_when_a_staged_skill_has_a_stale_signature(tmp_path, monkeypatch, capsys):
    skill_dir = _make_skill(tmp_path / "skills", "mala", sign=True)
    (skill_dir / "tool.py").write_text("class GreetTool:\n    pass  # cambio sin re-firmar\n", encoding="utf-8")

    exit_code = _run_main_with_skills_dir(tmp_path, monkeypatch, ["skills/mala/tool.py"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "mala" in output
    assert "sign_skill.py" in output


def test_main_is_a_fast_noop_when_nothing_staged_touches_skills(tmp_path, monkeypatch):
    assert _run_main_with_skills_dir(tmp_path, monkeypatch, ["agent_core/orchestrator.py"]) == 0


def test_main_ignores_an_unsigned_skill(tmp_path, monkeypatch):
    """unsigned no es tampered — mismo criterio que verify_skill_signature()
    en todo el resto del proyecto: sin firma, compatibilidad total."""
    _make_skill(tmp_path / "skills", "sin_firmar", sign=False)
    assert _run_main_with_skills_dir(tmp_path, monkeypatch, ["skills/sin_firmar/tool.py"]) == 0

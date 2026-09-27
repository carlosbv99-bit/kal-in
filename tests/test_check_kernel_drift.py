"""
Tests de scripts/check_kernel_drift.py::check_drift() — la lógica de
comparación en sí, contra repos sintéticos armados en tmp_path. No hay
un test acá que clone el kal real: el estado real de deriva entre kal
y kal-in cambia con el tiempo (ver docs/HISTORY.md) y no debería poder
poner en rojo esta suite — para eso está el workflow de CI
(kernel_drift.yml), informativo y no bloqueante.
"""
from __future__ import annotations

from scripts.check_kernel_drift import EXCLUDED_FILES, SHARED_DIRS, check_drift


def _make_repo(root, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        p = root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_identical_repos_report_no_drift(tmp_path):
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    shared_files = {"kernel/foo.py": "print('hola')\n", "sdk/bar.py": "x = 1\n"}
    _make_repo(kal_in, shared_files)
    _make_repo(kal, shared_files)

    assert check_drift(kal_in, kal) == []


def test_a_file_that_differs_is_reported(tmp_path):
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    _make_repo(kal_in, {"kernel/foo.py": "print('version vieja')\n"})
    _make_repo(kal, {"kernel/foo.py": "print('version nueva, con un fix')\n"})

    problems = check_drift(kal_in, kal)

    assert any("DIFIERE de kal: kernel/foo.py" in p for p in problems)


def test_a_file_missing_from_kal_in_is_reported(tmp_path):
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    _make_repo(kal_in, {})
    _make_repo(kal, {"kernel/nuevo_modulo.py": "# recién agregado en kal\n"})

    problems = check_drift(kal_in, kal)

    assert any("FALTA en kal-in" in p and "kernel/nuevo_modulo.py" in p for p in problems)


def test_utils_config_py_is_excluded_even_if_it_differs(tmp_path):
    """
    BUG REAL ENCONTRADO EN USO: utils/config.py de kal-in tiene MÁS
    campos a propósito (LLM/memoria/multimodal) — no es drift, es
    diseño (ver docs/HISTORY.md de kal, limpieza del 2026-09-27). Sin
    esta exclusión, el chequeo reportaría una divergencia permanente
    que nunca hay que "corregir".
    """
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    assert "utils/config.py" in EXCLUDED_FILES
    _make_repo(kal_in, {"utils/config.py": "class LLMConfig: ...\nclass Settings: ...\n"})
    _make_repo(kal, {"utils/config.py": "class Settings: ...\n"})

    assert check_drift(kal_in, kal) == []


def test_skill_sig_differences_are_never_reported(tmp_path):
    """
    Un skill.sig SIEMPRE difiere entre repos que re-firmaron con claves
    distintas (p.ej. tras perder una clave privada, ver docs/HISTORY.md
    de kal) aunque el código real de la skill sea idéntico — comparar
    la firma en sí sería puro ruido, no una señal de drift real.
    """
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    _make_repo(kal_in, {
        "skills/qr_code/tool.py": "def run(): return 'igual'\n",
        "skills/qr_code/skill.sig": '{"author_public_key": "aaaa"}',
    })
    _make_repo(kal, {
        "skills/qr_code/tool.py": "def run(): return 'igual'\n",
        "skills/qr_code/skill.sig": '{"author_public_key": "bbbb"}',
    })

    problems = check_drift(kal_in, kal)

    assert problems == []


def test_only_skills_present_in_both_repos_get_compared(tmp_path):
    """
    kal-in tiene su propio Skill Market, más grande que el de kal (ver
    CONTRIBUTING.md de kal) — una skill que solo existe de un lado no
    debería reportarse como "falta", es esperado.
    """
    kal_in = tmp_path / "kal_in"
    kal = tmp_path / "kal"
    _make_repo(kal_in, {
        "skills/qr_code/tool.py": "codigo identico\n",
        "skills/solo_en_kal_in/tool.py": "una skill propia de kal-in\n",
    })
    _make_repo(kal, {
        "skills/qr_code/tool.py": "codigo identico\n",
    })

    assert check_drift(kal_in, kal) == []


def test_shared_dirs_covers_the_documented_pure_kernel_boundary():
    """Ancla contra un descuido silencioso: si CONTRIBUTING.md agrega un
    directorio nuevo a "mecanismo puro", este chequeo debería crecer con él."""
    assert set(SHARED_DIRS) == {"kernel", "sdk", "audit", "code_analysis"}


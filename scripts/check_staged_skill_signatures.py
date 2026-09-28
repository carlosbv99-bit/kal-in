"""
Pre-commit local: rechaza un commit que deja una skill con firma
desactualizada, ANTES de que exista el commit — no después, en CI o en
la próxima corrida de la suite.

BUG REAL ENCONTRADO EN USO (2026-09-27, ver docs/HISTORY.md,
"Reconciliación con 3 commits remotos + bug real de firmas rotas"): un
`ruff check . --fix` reordenó imports en los 7 `skills/*/tool.py`
—cambiando su contenido— sin volver a firmarlos. Nada lo impidió en el
momento: el commit se creó igual, y recién 6 tests fallaron después
(`"firma tampered, se requiere 'verified'"`), cuando la suite completa
ejerció `verify_skill_signature()` de verdad contra el contenido
actual. `.github/workflows/validate-skills.yml` +
`scripts/validate_skills.py` ya cubren esto en CI (dispara en cada
push/PR que toque `skills/**`) — este script es el complemento de
FEEDBACK INMEDIATO: mismo chequeo, corrido LOCAL antes de que el
commit exista, para no depender del viaje de ida y vuelta a CI.

No firma nada por sí solo (eso requiere la clave privada del autor,
nunca algo que un hook deba tener) — solo avisa y bloquea, con el
comando exacto para arreglarlo (`scripts/sign_skill.py`).

Instalación (una vez por clon, ver también CONTRIBUTING.md):
    python3 scripts/install_git_hooks.py

Uso manual (sin pasar por el hook):
    python3 scripts/check_staged_skill_signatures.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_signing import verify_skill_signature
from kernel.registry.skills import DEFAULT_SKILLS_DIR


def _staged_files() -> list[str]:
    # --diff-filter=ACMR: agregado/copiado/modificado/renombrado — nunca
    # un archivo BORRADO (D), que ni siquiera existe ya para verificar.
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


def _skill_dirs_with_staged_content_changes(staged: list[str], skills_dir: Path) -> set[str]:
    """
    Nombres de skill con al menos un archivo de CONTENIDO (nunca
    skill.sig en sí, que no invalida su propia firma) staged en este
    commit — son las únicas que hace falta reverificar, no las 7+ del
    repo entero en cada commit que no toque skills/ para nada.

    Asume que el hook corre desde la raíz del repo (git siempre invoca
    los hooks así) — mismo supuesto que DEFAULT_SKILLS_DIR = Path("skills"),
    ya relativo a la raíz, sin necesidad de resolver rutas absolutas.
    """
    skills_root_name = skills_dir.parts[0]  # "skills"
    names: set[str] = set()
    for path in staged:
        parts = Path(path).parts
        # ("skills", "<nombre>", "<archivo>", ...) — al menos 3 partes,
        # nunca la carpeta de la skill en sí ni skills/ suelto.
        if len(parts) < 3 or parts[0] != skills_root_name:
            continue
        if parts[2] == "skill.sig":
            continue
        names.add(parts[1])
    return names


def main() -> int:
    staged = _staged_files()
    skill_names = _skill_dirs_with_staged_content_changes(staged, DEFAULT_SKILLS_DIR)
    if not skill_names:
        return 0  # el commit no toca contenido de ninguna skill — nada que revisar

    errors: list[str] = []
    for name in sorted(skill_names):
        skill_dir = DEFAULT_SKILLS_DIR / name
        if not skill_dir.is_dir():
            continue  # skill borrada en este mismo commit, no hay nada que verificar
        status = verify_skill_signature(skill_dir)
        if status == "tampered":
            errors.append(name)

    if errors:
        print("✗ Commit rechazado: firma desactualizada para " + ", ".join(errors))
        print(
            "  El contenido cambió pero skill.sig no se volvió a generar — "
            "re-firmá con el keypair original antes de commitear, p.ej.:"
        )
        for name in errors:
            print(f"    python3 scripts/sign_skill.py skills/{name} --key-dir <tu-key-dir-original>")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Detecta divergencia entre la copia embebida del kernel en este repo
(kernel/, sdk/, audit/, code_analysis/, más las skills que existen en
ambos por nombre) y el estado real de kal
(https://github.com/carlosbv99-bit/kal).

Por qué existe: kal-in todavía embebe su propia copia del kernel en vez
de depender del paquete kal (ver CONTRIBUTING.md) — un fix real hecho
en un repo no llega automáticamente al otro. Bug real encontrado en uso
(2026-09-27): K-2 (lectura arbitraria de archivos del host vía symlink
en docker_runner.py) se corrigió acá, pero kal (extraído dos semanas
antes) se quedó sin el fix durante todo ese tiempo, sin que nadie lo
notara hasta una auditoría manual del otro repo. Este chequeo está
pensado para acortar esa ventana de semanas a minutos — SOLO detecta y
reporta, nunca sincroniza nada solo (actualizar dependencias sin
revisión humana en código que llega a producción es un riesgo real de
cadena de suministro, no una mejora).

Uso:
    python3 scripts/check_kernel_drift.py                       # clona kal fresco
    python3 scripts/check_kernel_drift.py --kal-repo /ruta/local  # reusa un clon ya existente
"""
from __future__ import annotations

import argparse
import filecmp
import subprocess
import sys
import tempfile
from pathlib import Path

KAL_REPO_URL = "https://github.com/carlosbv99-bit/kal.git"

# Directorios que DEBEN ser mecanismo puro, sin extensión específica de
# kal-in (ver CONTRIBUTING.md de ambos repos: "kernel/... zero
# dependency on agent_core/tool_integration", "sdk/... 100% stdlib") —
# se comparan byte a byte, archivo por archivo.
SHARED_DIRS = ["kernel", "sdk", "audit", "code_analysis"]

# utils/config.py es la única excepción documentada: kal-in
# deliberadamente tiene MÁS campos que kal (LLMConfig, MemoryConfig,
# MultimodalConfig, etc. — ver docs/HISTORY.md de kal, "Limpieza de
# utils/config.py/config/config.yaml", 2026-09-27) — eso no es drift,
# es diseño. Excluido a propósito para no generar ruido permanente.
EXCLUDED_FILES = {"utils/config.py"}


def _clone_kal(dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", KAL_REPO_URL, str(dest)],
        check=True, capture_output=True, text=True,
    )


def _iter_repo_files(root: Path, subdir: str) -> set[str]:
    base = root / subdir
    if not base.exists():
        return set()
    return {
        str(p.relative_to(root)) for p in base.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
        # skill.sig es un blob de firma criptográfica, no código — SIEMPRE
        # difiere entre repos que re-firmaron con claves distintas (p.ej.
        # tras la pérdida de clave privada del 2026-09-27, ver
        # docs/HISTORY.md de kal), aunque el tool.py/skill.yaml de abajo
        # sea idéntico. No es una señal real de drift, sería puro ruido
        # permanente.
        and p.suffix != ".sig"
    }


def _shared_skill_dirs(kal_in_root: Path, kal_root: Path) -> list[str]:
    """
    Solo las skills que existen en AMBOS repos por nombre — kal-in
    tiene su propio Skill Market, más grande y en crecimiento (ver
    CONTRIBUTING.md de kal), no tiene sentido exigir que las listas
    completas coincidan.
    """
    kal_in_skills_dir = kal_in_root / "skills"
    kal_skills_dir = kal_root / "skills"
    kal_in_skills = {p.name for p in kal_in_skills_dir.iterdir() if p.is_dir()} if kal_in_skills_dir.exists() else set()
    kal_skills = {p.name for p in kal_skills_dir.iterdir() if p.is_dir()} if kal_skills_dir.exists() else set()
    return sorted(kal_in_skills & kal_skills)


def check_drift(kal_in_root: Path, kal_root: Path) -> list[str]:
    """Devuelve una lista de líneas describiendo la divergencia encontrada (vacía si no hay ninguna)."""
    problems: list[str] = []

    compare_dirs = list(SHARED_DIRS)
    for skill_name in _shared_skill_dirs(kal_in_root, kal_root):
        compare_dirs.append(f"skills/{skill_name}")

    for subdir in compare_dirs:
        kal_in_files = _iter_repo_files(kal_in_root, subdir) - EXCLUDED_FILES
        kal_files = _iter_repo_files(kal_root, subdir) - EXCLUDED_FILES

        for rel in sorted(kal_files - kal_in_files):
            problems.append(f"FALTA en kal-in (existe en kal): {rel}")

        for rel in sorted(kal_in_files & kal_files):
            if not filecmp.cmp(kal_in_root / rel, kal_root / rel, shallow=False):
                problems.append(f"DIFIERE de kal: {rel}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kal-repo", type=Path, default=None,
        help="Ruta local a un clon de kal ya existente (si no se pasa, se clona fresco a un directorio temporal)",
    )
    args = parser.parse_args()

    kal_in_root = Path(__file__).resolve().parent.parent

    if args.kal_repo is not None:
        problems = check_drift(kal_in_root, args.kal_repo)
    else:
        with tempfile.TemporaryDirectory(prefix="kal_drift_check_") as tmp:
            kal_root = Path(tmp) / "kal"
            print(f"Clonando {KAL_REPO_URL}...")
            _clone_kal(kal_root)
            problems = check_drift(kal_in_root, kal_root)

    if not problems:
        print("Sin divergencia: la copia embebida del kernel coincide con kal.")
        return 0

    print(f"Divergencia encontrada ({len(problems)}):")
    for p in problems:
        print(f"  - {p}")
    print("\nEsto NO se corrige solo — revisar a mano si el cambio en kal es un fix real que hace falta portar acá (o viceversa).")
    return 1


if __name__ == "__main__":
    sys.exit(main())

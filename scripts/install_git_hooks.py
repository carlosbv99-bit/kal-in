"""
Instala los git hooks locales de este repo (hoy: solo pre-commit, ver
scripts/hooks/pre-commit) — copia, nunca symlink (Windows/algunos
filesystems no lo soportan bien para .git/hooks/), desde
scripts/hooks/ (sí versionado) hacia .git/hooks/ (nunca versionado por
git, cada clon lo pierde y tiene que reinstalarlo).

Uso (una vez por clon):
    python3 scripts/install_git_hooks.py
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

_SOURCE_DIR = Path(__file__).parent / "hooks"


def _git_dir() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"], capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def main() -> None:
    hooks_dir = _git_dir() / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    installed = []
    for source in sorted(_SOURCE_DIR.iterdir()):
        if not source.is_file():
            continue
        dest = hooks_dir / source.name
        shutil.copyfile(source, dest)
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(source.name)

    print(f"Hooks instalados en {hooks_dir}: {', '.join(installed)}")


if __name__ == "__main__":
    main()

"""
Fase A del plan de comunidad (ver memoria del proyecto): traer una
skill desde un repositorio Git remoto ("market"), en vez de que un
humano copie la carpeta a mano. El "market" es, deliberadamente, SOLO
un repositorio Git con la misma estructura que ya usa `skills/`
localmente (`skills/<nombre>/skill.yaml` + `tool.py` + `skill.sig`) —
no hay ningún índice separado que mantener sincronizado: listar las
skills disponibles es clonar el repo y parsear cada `skill.yaml` con
`parse_manifest()` (kernel/registry/skills.py), ya existente.

La verificación de integridad (`skill.sig`, ver
kernel/registry/skill_signing.py) es responsabilidad de quien
instala (scripts/install_from_market.py) — este módulo solo sabe
descargar, no decide política de seguridad.

Limitación aceptada: `--depth 1 --branch <ref>` solo acepta ramas o
tags, no un commit SHA arbitrario — suficiente para esta fase.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from kernel.registry.skills import MANIFEST_FILENAME, SkillManifest, parse_manifest
from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_REF = "main"


class MarketError(Exception):
    """No se pudo listar/traer una skill del market: red, ref inexistente, o nombre desconocido."""


def _clone(market_url: str, ref: str, dest: Path) -> None:
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, market_url, str(dest)],
        capture_output=True, text=True, timeout=60,
        check=False,  # returncode se interpreta a mano abajo, nunca con una excepción genérica
    )
    if result.returncode != 0:
        raise MarketError(f"No se pudo clonar '{market_url}' (rama/ref '{ref}'): {result.stderr.strip()}")


def list_market_skills(market_url: str, ref: str = DEFAULT_REF) -> list[SkillManifest]:
    """Clona el market y devuelve el manifiesto de cada skill que contiene (posiblemente vacío)."""
    with tempfile.TemporaryDirectory(prefix="kal_market_") as tmp:
        repo_dir = Path(tmp) / "repo"
        _clone(market_url, ref, repo_dir)
        skills_dir = repo_dir / "skills"
        if not skills_dir.exists():
            return []

        manifests: list[SkillManifest] = []
        for manifest_path in sorted(skills_dir.glob(f"*/{MANIFEST_FILENAME}")):
            try:
                manifests.append(parse_manifest(manifest_path))
            except Exception as e:  # noqa: BLE001 — un manifiesto roto no debe romper el listado del resto
                logger.debug(f"Manifiesto roto en el market, se ignora al listar: {manifest_path}: {e}")
                continue
        return manifests


def fetch_skill_from_market(market_url: str, skill_name: str, dest_dir: Path, ref: str = DEFAULT_REF) -> Path:
    """
    Descarga skills/<skill_name>/ del market a dest_dir (que ya debe
    existir, típicamente un directorio temporal del llamador — la
    verificación de firma se hace DESPUÉS de esta llamada, nunca acá).
    """
    with tempfile.TemporaryDirectory(prefix="kal_market_") as tmp:
        repo_dir = Path(tmp) / "repo"
        _clone(market_url, ref, repo_dir)
        source_dir = repo_dir / "skills" / skill_name

        if not (source_dir / MANIFEST_FILENAME).exists():
            skills_root = repo_dir / "skills"
            available = sorted(p.name for p in skills_root.iterdir() if p.is_dir()) if skills_root.exists() else []
            raise MarketError(
                f"No existe la skill '{skill_name}' en '{market_url}' (rama/ref '{ref}'). "
                f"Disponibles: {', '.join(available) or '(ninguna)'}"
            )

        shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)
    return dest_dir

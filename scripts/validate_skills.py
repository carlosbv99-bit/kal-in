"""
Chequeo de integridad de TODAS las skills del repo (Fase C del plan
de comunidad — curación de quién puede publicar en el market). Pensado
para correr en CI (.github/workflows/validate-skills.yml) en cada PR
que toque skills/**, y también a mano antes de abrir uno.

Valida exactamente lo mismo que ya exige la instalación remota
(kernel/registry/skill_market.py + scripts/install_from_market.py):
cada skill.yaml debe parsear, y cada skill.sig debe verificar. Esto
prueba integridad del paquete — NUNCA autoridad del autor (ver
CONTRIBUTING.md): un PR puede pasar este chequeo perfecto y aun así
necesitar revisión humana antes de mergear.

Uso:
    python3 scripts/validate_skills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_signing import verify_skill_signature
from kernel.registry.skills import (
    DEFAULT_SKILLS_DIR,
    MANIFEST_FILENAME,
    parse_manifest,
)


def validate_all_skills(skills_dir: Path = DEFAULT_SKILLS_DIR) -> list[str]:
    """Devuelve la lista de errores encontrados (vacía si todo está bien)."""
    errors: list[str] = []
    if not skills_dir.exists():
        return errors

    for manifest_path in sorted(skills_dir.glob(f"*/{MANIFEST_FILENAME}")):
        skill_dir = manifest_path.parent
        try:
            manifest = parse_manifest(manifest_path)
        except Exception as e:  # noqa: BLE001 — el objetivo de este script es justamente reportar manifiestos rotos
            errors.append(f"{skill_dir.name}: manifiesto inválido ({e})")
            continue

        signature_status = verify_skill_signature(skill_dir)
        if signature_status != "verified":
            errors.append(f"{skill_dir.name} ('{manifest.name}'): firma {signature_status}, se requiere 'verified'")

    return errors


def main() -> None:
    errors = validate_all_skills()
    if errors:
        print("Validación de skills FALLÓ:")
        for error in errors:
            print(f"  - {error}")
        raise SystemExit(1)

    print("Todas las skills verificadas correctamente.")


if __name__ == "__main__":
    main()

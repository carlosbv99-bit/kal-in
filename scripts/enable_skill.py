"""
Habilita o deshabilita una skill ya instalada bajo skills/<nombre>/,
reemplazando la edición manual de `enabled:` en su skill.yaml (F4 del
plan de marketplace, ver kernel/registry/skills.py::set_skill_enabled).

Antes de habilitar, muestra en un solo lugar todo lo que la edición a
mano no mostraba: permisos, paquetes de pip que se van a instalar
(requirements), servicios del kernel que puede llamar, y el estado de
su firma de integridad (F3, ver kernel/registry/skill_signing.py) —
y pide confirmación explícita. Una skill cuya firma no verifica
("tampered") nunca se habilita, ni siquiera con --yes.

Uso:
    python3 scripts/enable_skill.py skills/mi_skill/            # pide confirmación
    python3 scripts/enable_skill.py skills/mi_skill/ --yes      # sin preguntar
    python3 scripts/enable_skill.py skills/mi_skill/ --disable  # deshabilita, sin confirmación
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_signing import signer_fingerprint, verify_skill_signature
from kernel.registry.skills import (
    MANIFEST_FILENAME,
    audit_skill_enable_change,
    parse_manifest,
    set_skill_enabled,
)

_CONFIRM_YES = {"s", "si", "sí", "y", "yes"}


def _print_summary(manifest, signature_status: str, skill_dir: Path) -> None:
    print(f"Skill: {manifest.name} (v{manifest.version})")
    print(f"Descripción: {manifest.description}")
    print(f"Permisos: {manifest.permissions or '(ninguno)'}")
    if manifest.requirements:
        print(f"Paquetes de pip que se van a instalar: {manifest.requirements}")
        print("  (corren dentro de un contenedor aislado, pero con la capacidad de estas dependencias)")
    else:
        print("Paquetes de pip: (ninguno, solo librería estándar)")
    print(f"Servicios del kernel permitidos: {manifest.kernel_services or '(ninguno)'}")
    if signature_status == "verified":
        # HALLAZGO REAL DE AUDITORÍA EXTERNA (M-8 en kal, 2026-09-27,
        # portado acá vía scripts/check_kernel_drift.py): "verificada
        # (... su autor lo firmó)" daba a entender que se verificó QUIÉN
        # es el autor — falso. Solo prueba integridad contra la clave que
        # sea que firmó, cualquiera puede tener una. Se muestra el
        # fingerprint para que un humano lo compare contra lo que el
        # autor real haya publicado en otro canal de confianza.
        fingerprint = signer_fingerprint(skill_dir)
        print(
            f"Firma: integridad verificada — el paquete no cambió desde que se firmó con "
            f"la clave {fingerprint}. Esto NO confirma quién es el autor: cualquiera puede "
            f"firmar con su propia clave. Compará este fingerprint contra el que el autor "
            f"real haya publicado en otro canal de confianza."
        )
    else:
        print("Firma: SIN FIRMAR — no se puede verificar que el contenido no fue alterado desde que se escribió.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill_dir", type=Path, help="Carpeta de la skill (debe contener skill.yaml)")
    parser.add_argument("--yes", action="store_true", help="Habilitar sin pedir confirmación interactiva")
    parser.add_argument("--disable", action="store_true", help="Deshabilitar en vez de habilitar")
    args = parser.parse_args()

    manifest_path = args.skill_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        print(f"ERROR: no se encontró {MANIFEST_FILENAME} en '{args.skill_dir}' — ¿es la carpeta correcta?")
        raise SystemExit(1)

    manifest = parse_manifest(manifest_path)

    if args.disable:
        if not manifest.enabled:
            print(f"'{manifest.name}' ya está deshabilitada, no se hizo ningún cambio.")
            return
        set_skill_enabled(args.skill_dir, False)
        audit_skill_enable_change(manifest.name, args.skill_dir.name, False)
        print(f"'{manifest.name}' deshabilitada.")
        return

    if manifest.enabled:
        print(f"'{manifest.name}' ya está habilitada, no se hizo ningún cambio.")
        return

    signature_status = verify_skill_signature(args.skill_dir)
    if signature_status == "tampered":
        print(
            f"ERROR: la firma de '{manifest.name}' no verifica contra el contenido actual de la carpeta "
            f"— el paquete fue alterado desde que se firmó (o skill.sig está corrupto). No se habilita."
        )
        raise SystemExit(1)

    _print_summary(manifest, signature_status, args.skill_dir)

    if not args.yes:
        answer = input("\n¿Confirmás habilitar esta skill? [s/N]: ").strip().lower()
        if answer not in _CONFIRM_YES:
            print("Cancelado, no se hizo ningún cambio.")
            return

    set_skill_enabled(args.skill_dir, True)
    audit_skill_enable_change(manifest.name, args.skill_dir.name, True)
    print(f"\n'{manifest.name}' habilitada. Reiniciá kal (o esperá el --reload de uvicorn) para que la cargue.")


if __name__ == "__main__":
    main()

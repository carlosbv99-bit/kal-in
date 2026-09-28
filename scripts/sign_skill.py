"""
Firma una carpeta de skill con la identidad del AUTOR (ver
kernel/registry/skill_signing.py — F3 del plan de marketplace). Uso
pensado para quien ESCRIBE una skill y la va a distribuir/publicar,
no para quien solo la instala.

Uso:
    python3 scripts/sign_skill.py skills/mi_skill/
    python3 scripts/sign_skill.py skills/mi_skill/ --key-dir /otra/ruta

La primera vez que se corre (con un --key-dir dado) genera un keypair
Ed25519 propio del autor y lo persiste ahí — corridas siguientes con
el MISMO --key-dir reusan la misma identidad, así que firmar varias
skills (o nuevas versiones de la misma) queda atribuido al mismo
autor. Esa clave privada nunca debe compartirse ni subirse a ningún
lado — solo el archivo skill.sig (que sí incluye la clave PÚBLICA) va
junto con la skill.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_signing import SkillSigner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_dir", type=Path, help="Carpeta de la skill a firmar (debe contener skill.yaml)")
    parser.add_argument(
        "--key-dir", type=Path, default=Path("data/keys"),
        help="Dónde guardar/leer el keypair del autor (default: data/keys)",
    )
    args = parser.parse_args()

    if not (args.skill_dir / "skill.yaml").exists():
        print(f"ERROR: no se encontró skill.yaml en '{args.skill_dir}' — ¿es la carpeta correcta?")
        raise SystemExit(1)

    signer = SkillSigner(key_dir=args.key_dir)
    sig_path = signer.write_signature(args.skill_dir)

    print(f"Firmado: {sig_path}")
    print(f"Clave pública del autor (fingerprint): {signer.public_key_hex()}")
    print("Guardá esta clave en un lugar seguro y persistente — firmar una próxima versión")
    print("con el mismo --key-dir la atribuye al mismo autor.")


if __name__ == "__main__":
    main()

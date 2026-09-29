"""
Instala una skill desde un "market" remoto (Fase A del plan de
comunidad, ver kernel/registry/skill_market.py) — reemplaza el paso
manual de copiar una carpeta al proyecto antes de correr
scripts/enable_skill.py.

El market es, deliberadamente, solo un repositorio Git con la misma
estructura que ya usa skills/ localmente. Por default apunta al propio
repo de kal (dogfooding: sus 6 skills de referencia son el primer
catálogo real).

A diferencia de habilitar una skill LOCAL (donde "sin firmar" se
permite con una advertencia, porque un humano ya tuvo la carpeta en su
disco y pudo leerla), una skill traída de un market remoto DEBE estar
firmada y verificar — nunca se instala una sin firmar o con firma
alterada, sin excepción. Esto prueba integridad del paquete, NUNCA
autoridad del autor (ver docstring de kernel/registry/skill_signing.py)
— la curación de qué se puede publicar en un market queda para más
adelante (Fase C del plan), no resuelta acá.

Uso:
    python3 scripts/install_from_market.py --list
    python3 scripts/install_from_market.py qr_code
    python3 scripts/install_from_market.py qr_code --market https://github.com/otra-comunidad/skills.git --ref v2
    python3 scripts/install_from_market.py qr_code --yes
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_market import (
    DEFAULT_REF,
    MarketError,
    fetch_skill_from_market,
    list_market_skills,
)
from kernel.registry.skill_signing import signer_fingerprint, verify_skill_signature
from kernel.registry.skills import (
    DEFAULT_SKILLS_DIR,
    audit_skill_enable_change,
    parse_manifest,
    set_skill_enabled,
    validate_skill_name,
)

DEFAULT_MARKET_URL = "https://github.com/carlosbv99-bit/kal.git"
_CONFIRM_YES = {"s", "si", "sí", "y", "yes"}


def _print_available(market_url: str, ref: str) -> None:
    manifests = list_market_skills(market_url, ref=ref)
    if not manifests:
        print(f"El market '{market_url}' (rama/ref '{ref}') no tiene ninguna skill.")
        return
    print(f"Skills disponibles en '{market_url}' (rama/ref '{ref}'):")
    for manifest in manifests:
        print(f"  - {manifest.name} (v{manifest.version}): {manifest.description}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skill_name", nargs="?", help="Nombre de la skill a instalar (carpeta skills/<nombre>/ en el market)")
    parser.add_argument("--market", default=DEFAULT_MARKET_URL, help=f"URL del repo Git del market (default: {DEFAULT_MARKET_URL})")
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"Rama o tag del market a usar (default: {DEFAULT_REF})")
    parser.add_argument("--list", action="store_true", help="Listar las skills disponibles en el market y salir")
    parser.add_argument("--yes", action="store_true", help="Instalar sin pedir confirmación interactiva")
    args = parser.parse_args()

    try:
        if args.list:
            _print_available(args.market, args.ref)
            return

        if not args.skill_name:
            parser.error("falta el nombre de la skill (o usá --list para ver las disponibles)")

        # VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (A-1 en
        # kal, 2026-09-27, portada acá vía scripts/check_kernel_drift.py):
        # args.skill_name (input de línea de comandos, viene de afuera
        # del proceso) se usaba SIN NINGUNA sanitización para armar
        # local_dest — a diferencia de manifest.name (siempre validado
        # por validate_skill_name() antes de cargar/activar una skill
        # LOCAL). Un --skill-name "../../.venv/lib/pythonX.Y/site-packages/algo"
        # escribía y HABILITABA una skill fuera de skills/ por completo —
        # mismo patrón exacto que K-1/K-6, solo que en el instalador de
        # market en vez de en el registry. Rechazado temprano, antes de
        # tocar la red o el filesystem.
        name_error = validate_skill_name(args.skill_name)
        if name_error is not None:
            print(f"ERROR: {name_error}")
            raise SystemExit(1)

        local_dest = DEFAULT_SKILLS_DIR / args.skill_name
        if local_dest.exists():
            print(f"ERROR: ya existe '{local_dest}' — borrala primero si de verdad querés reinstalarla.")
            raise SystemExit(1)

        with tempfile.TemporaryDirectory(prefix="kal_market_install_") as tmp:
            staging_dir = Path(tmp) / args.skill_name
            fetch_skill_from_market(args.market, args.skill_name, staging_dir, ref=args.ref)

            signature_status = verify_skill_signature(staging_dir)
            if signature_status != "verified":
                print(
                    f"ERROR: '{args.skill_name}' no tiene una firma válida en el market "
                    f"(estado: {signature_status}). Una skill remota SIEMPRE debe estar firmada "
                    "y verificar — nunca se instala sin firma o con firma alterada."
                )
                raise SystemExit(1)

            manifest = parse_manifest(staging_dir / "skill.yaml")
            print(f"Skill: {manifest.name} (v{manifest.version})")
            print(f"Descripción: {manifest.description}")
            print(f"Permisos: {manifest.permissions or '(ninguno)'}")
            if manifest.requirements:
                print(f"Paquetes de pip que se van a instalar: {manifest.requirements}")
                print("  (corren dentro de un contenedor aislado, pero con la capacidad de estas dependencias)")
            else:
                print("Paquetes de pip: (ninguno, solo librería estándar)")
            print(f"Servicios del kernel permitidos: {manifest.kernel_services or '(ninguno)'}")
            # HALLAZGO REAL DE AUDITORÍA EXTERNA (M-8 en kal, 2026-09-27,
            # portado acá vía scripts/check_kernel_drift.py): "Firma:
            # verificada (... su autor lo firmó)" daba a entender que se
            # verificó QUIÉN es el autor — falso. "verified" solo prueba
            # que el paquete no cambió desde que ALGUIEN (cualquiera puede
            # generar su propio keypair) lo firmó con ESA clave concreta.
            # Se muestra el fingerprint para que un humano pueda comparar
            # contra lo que el autor real haya publicado en otro lado —
            # la firma en sí NUNCA prueba eso por su cuenta.
            fingerprint = signer_fingerprint(staging_dir)
            print(
                f"Firma: integridad verificada — el paquete no cambió desde que se firmó con "
                f"la clave {fingerprint}. Esto NO confirma quién es el autor: cualquiera puede "
                f"firmar con su propia clave. Compará este fingerprint contra el que el autor "
                f"real haya publicado en otro canal de confianza."
            )

            if not args.yes:
                answer = input("\n¿Confirmás instalar esta skill? [s/N]: ").strip().lower()
                if answer not in _CONFIRM_YES:
                    print("Cancelado, no se instaló nada.")
                    return

            DEFAULT_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            # Segunda capa de defensa, mismo criterio que
            # SandboxedSkillTool.__init__ y ToolVersionStore._tool_dir():
            # nunca confiar en un solo chequeo para una escritura a disco
            # derivada de input externo, aunque validate_skill_name() de
            # arriba ya lo cubra estructuralmente (la regex no admite '/').
            if not local_dest.resolve().is_relative_to(DEFAULT_SKILLS_DIR.resolve()):
                print(f"ERROR: '{args.skill_name}' resuelve fuera de skills/ — rechazado.")
                raise SystemExit(1)
            shutil.copytree(staging_dir, local_dest)

        set_skill_enabled(local_dest, True)
        audit_skill_enable_change(manifest.name, local_dest.name, True, source="market")
        print(f"\n'{manifest.name}' instalada y habilitada. Reiniciá kal (o esperá el --reload de uvicorn) para que la cargue.")
    except MarketError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

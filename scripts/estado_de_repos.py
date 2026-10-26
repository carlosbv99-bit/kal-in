"""
Estado de kal-in y de su repo hermano `kal`, para no mezclar commits.

Por qué existe: kal-in embebe una copia del kernel (`kernel/`, `sdk/`, `audit/`,
`code_analysis/`), y CONTRIBUTING.es.md exige portar cada cambio de esos directorios
al otro repo *en la misma sesión*. Ese flujo tiene un punto ciego que ya mordió una
vez: mientras el port está a medio hacer en un repo (archivos compartidos sin
commitear), alguien commitea en el otro con `git add -A` y se lleva puesto trabajo
que no era suyo — o lo deja huérfano. La regla (stages explícitos, nunca `-A`) es
fácil de olvidar; este chequeo hace que el olvido se vea antes de commitear.

Uso:
    python3 scripts/estado_de_repos.py            # reporte legible
    python3 scripts/estado_de_repos.py --check    # exit 1 si hay compartidos sucios

`--check` falla SOLO por cambios sin commitear en archivos compartidos: un archivo
compartido a medio hacer es el único estado en el que un commit puede mezclar dos
casos o perder el port. Un repo sucio en `agent_core/` o en docs es normal y no
bloquea nada.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Directorios que kal-in embebe de kal (misma lista que scripts/check_kernel_drift.py).
DIRECTORIOS_COMPARTIDOS = ("kernel", "sdk", "audit", "code_analysis")


def _git(repo: Path, *args: str) -> str:
    resultado = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return resultado.stdout if resultado.returncode == 0 else ""


def _es_compartido(ruta: str, skills_compartidas: tuple[str, ...] = ()) -> bool:
    """¿La ruta es de una superficie que existe en los dos repos?"""
    if any(ruta == d or ruta.startswith(d + "/") for d in DIRECTORIOS_COMPARTIDOS):
        return True
    # Una skill con el mismo nombre en ambos repos también es compartida
    # (CONTRIBUTING.es.md la nombra explícitamente): sus archivos son el port.
    return any(ruta == f"skills/{n}" or ruta.startswith(f"skills/{n}/") for n in skills_compartidas)


def estado(repo: Path, *, skills_compartidas: tuple[str, ...] = ()) -> dict:
    """Estado del repo: rama, upstream, ahead/behind, sucios (con marca de compartido)."""
    if not (repo / ".git").exists():
        return {"existe": False, "ruta": str(repo)}
    sucios = [linea[3:] for linea in _git(repo, "status", "--porcelain").splitlines() if linea.strip()]
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    adelante = _git(repo, "rev-list", "--count", "@{u}..HEAD").strip() if upstream else ""
    atras = _git(repo, "rev-list", "--count", "HEAD..@{u}").strip() if upstream else ""
    return {
        "existe": True,
        "ruta": str(repo),
        "rama": _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "upstream": upstream or "(sin upstream)",
        "adelante": int(adelante) if adelante.isdigit() else 0,
        "atras": int(atras) if atras.isdigit() else 0,
        "sucios": sucios,
        "compartidos_sucios": [s for s in sucios if _es_compartido(s, skills_compartidas)],
        "sin_pushear": _git(repo, "log", "--oneline", "@{u}..HEAD").strip().splitlines() if upstream else [],
    }


def compartidos_sucios(estados: dict[str, dict]) -> dict[str, list[str]]:
    """Los repos (de los que existen) con archivos compartidos sin commitear."""
    return {
        nombre: e["compartidos_sucios"]
        for nombre, e in estados.items()
        if e["existe"] and e["compartidos_sucios"]
    }


def _imprimir(nombre: str, e: dict) -> None:
    if not e["existe"]:
        print(f"\n== {nombre}: no existe en {e['ruta']} (se omite)")
        return
    print(f"\n== {nombre} ({e['ruta']})")
    print(f"   rama {e['rama']} | upstream {e['upstream']} | {e['adelante']} sin pushear, {e['atras']} detrás")
    if e["compartidos_sucios"]:
        print(f"   ⚠ compartidos SIN COMMITEAR ({len(e['compartidos_sucios'])}):")
        for ruta in e["compartidos_sucios"]:
            print(f"     - {ruta}")
    otros = [s for s in e["sucios"] if s not in e["compartidos_sucios"]]
    if otros:
        print(f"   sin commitear (no compartidos, {len(otros)}): {', '.join(otros[:5])}" + (" ..." if len(otros) > 5 else ""))
    if e["sin_pushear"]:
        print(f"   últimos sin pushear: {' | '.join(e['sin_pushear'][:3])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kal-repo", type=Path, default=None,
        help="Ruta al clon de kal (default: ../kal junto a este repo, si existe)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Exit 1 si algún repo tiene cambios sin commitear en archivos compartidos",
    )
    args = parser.parse_args()

    kal_in = Path(__file__).resolve().parent.parent
    kal = args.kal_repo or (kal_in.parent / "kal")

    # Skills con el mismo nombre en los dos repos: las calcula el drift checker, que ya
    # tiene esa lógica (_shared_skill_dirs). Si kal no existe o algo falla, seguimos solo
    # con los directorios: el chequeo de directorios no depende de esto.
    try:
        from scripts.check_kernel_drift import _shared_skill_dirs

        skills_compartidas = tuple(_shared_skill_dirs(kal_in, kal))
    except Exception:
        skills_compartidas = ()
    estados = {
        "kal-in": estado(kal_in, skills_compartidas=skills_compartidas),
        "kal": estado(kal, skills_compartidas=skills_compartidas),
    }
    for nombre, e in estados.items():
        _imprimir(nombre, e)

    sucios = compartidos_sucios(estados)
    if not sucios:
        print("\nOK: ningún archivo compartido quedó sin commitear en ningún repo.")
        return 0

    print(
        "\nArchivos COMPARTIDOS sin commitear — un commit en este estado puede mezclar dos "
        "casos o perder el port (ver CONTRIBUTING.es.md, 'Mantener kal-in y kal sincronizados'):"
    )
    for nombre, rutas in sucios.items():
        print(f"  {nombre}: {', '.join(rutas)}")
    print(
        "\nRegla: commiteá con rutas EXPLÍCITAS (nunca `git add -A`), y terminá el port en los "
        "dos repos antes de dar el caso por cerrado."
    )
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())

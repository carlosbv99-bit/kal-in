#!/usr/bin/env python3
"""
Regenera la medición de la capa epistémica (F6).

Uso:

    python scripts/measure_epistemics.py            # imprime la tabla de métricas
    python scripts/measure_epistemics.py --escribir # regenera el JSON y el markdown
    python scripts/measure_epistemics.py --verificar # falla si lo commiteado no coincide

`--verificar` es lo que corre la suite de tests: compara la medición recién
hecha contra `docs/medicion-capa-epistemica.json`. Sirve para lo que el roadmap
pide en F6 — que los marcadores de hedge y el umbral de respuesta larga se
toquen SOLO con números sobre la mesa: si alguien cambia `gate.py` sin volver a
medir, el JSON queda viejo y esto falla.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from agent_core.epistemics.measurement import (  # noqa: E402
    cargar_corpus,
    correr_suite,
    render_markdown,
)

JSON_POR_DEFECTO = RAIZ / "docs" / "medicion-capa-epistemica.json"
MD_POR_DEFECTO = RAIZ / "docs" / "MEDICION-CAPA-EPISTEMICA.md"


def _serializar(reporte) -> str:
    return json.dumps(reporte.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _clave_comparable(datos: dict) -> dict:
    """
    Lo comparable entre corridas. Se excluye el ancla del audit log por un
    motivo concreto: depende de que el proceso pueda ESCRIBIR el log (clave HMAC
    accesible). En un entorno sin esa clave todas las evidencias quedan sin
    ancla a la vez, y ese caso lo cubre un test propio ("todas o ninguna"), no
    la comparación contra lo commiteado.
    """
    copia = json.loads(json.dumps(datos))
    copia.get("estructural", {}).pop("ancla_audit_event_hash", None)
    return copia


def _imprimir(reporte) -> None:
    print(f"Corpus: huella {reporte.huella_corpus[:16]}  tamaños {dict(reporte.tamanos)}")
    for clave, valor in reporte.metricas.items():
        print(f"  {clave:38s} {valor if valor is None else round(valor, 3)}")
    fallos = [r for r in reporte.resultados if not r.ok]
    print(f"Items que no cumplen su expectativa: {len(fallos)}")
    for fallo in fallos:
        print(f"  - {fallo.id}: {'; '.join(fallo.fallos)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=JSON_POR_DEFECTO)
    parser.add_argument("--md", type=Path, default=MD_POR_DEFECTO)
    parser.add_argument(
        "--escribir", action="store_true", help="regenera el JSON y el markdown"
    )
    parser.add_argument(
        "--verificar",
        action="store_true",
        help="compara contra el JSON commiteado y falla si quedó viejo",
    )
    args = parser.parse_args(argv)

    reporte = correr_suite(cargar_corpus())
    datos = reporte.to_dict()

    if args.verificar:
        if not args.json.exists():
            print(f"FALTA {args.json}: no hay medición commiteada", file=sys.stderr)
            return 1
        commiteado = json.loads(args.json.read_text(encoding="utf-8"))
        if _clave_comparable(commiteado) != _clave_comparable(datos):
            print(
                "La medición commiteada no coincide con el código y el corpus de ahora.\n"
                "Si el cambio fue intencional: python scripts/measure_epistemics.py --escribir",
                file=sys.stderr,
            )
            return 1
        print("Medición consistente con lo commiteado.")
        return 0

    if args.escribir:
        args.json.write_text(_serializar(reporte), encoding="utf-8")
        args.md.write_text(render_markdown(reporte), encoding="utf-8")
        print(f"Escrito {args.json} y {args.md}")
        return 0

    _imprimir(reporte)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

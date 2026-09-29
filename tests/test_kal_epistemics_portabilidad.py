"""
Tests de portabilidad de kal_epistemics.

El paquete se presenta como "integrable en kal-in o en cualquier otro
agente", y esa afirmación tiene que ser verificable, no una convención — el
mismo criterio que usa kal cuando dice que no depende de ningún LLM y lo
prueba con un import que falla.

Dos propiedades, las dos mecánicas:

1. El paquete no importa NADA del host (`agent_core`, `kernel`, `sdk`, ...):
   se puede copiar a otro agente tal cual.
2. Solo usa stdlib. El día que alguien agregue una dependencia externa, esto
   falla y obliga a discutirlo en vez de arrastrarla en silencio dentro de
   cada contenedor que la copie (que es exactamente el problema que motivó
   que `sdk/` sea 100% stdlib).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "kal_epistemics"
MODULOS_DEL_HOST = ("agent_core", "kernel", "sdk", "tool_integration", "task_execution", "error_handling")


def _fuentes() -> list[Path]:
    return sorted(PAQUETE.glob("*.py"))


def test_el_paquete_no_importa_nada_del_host():
    patron = re.compile(
        r"^\s*(?:from|import)\s+(" + "|".join(MODULOS_DEL_HOST) + r")\b", re.MULTILINE
    )

    ofensores = [f.name for f in _fuentes() if patron.search(f.read_text(encoding="utf-8"))]

    assert ofensores == [], (
        f"{ofensores} importan módulos del host: kal_epistemics dejaría de ser "
        "copiable a otro agente."
    )


def test_importar_el_paquete_no_arrastra_al_host():
    """Verificación en un proceso limpio: en el proceso de pytest el host ya
    está importado por conftest.py, así que ahí la comprobación no diría
    nada."""
    codigo = (
        "import sys, kal_epistemics\n"
        f"prohibidos = sorted(m for m in sys.modules if m.split('.')[0] in {MODULOS_DEL_HOST!r})\n"
        "print('|'.join(prohibidos))\n"
    )

    resultado = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, capture_output=True, text=True
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "", (
        f"importar kal_epistemics arrastró {resultado.stdout.strip()} — el paquete "
        "dejaría de ser portable."
    )


def test_solo_usa_stdlib():
    patron = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)
    externos: set[str] = set()

    for archivo in _fuentes():
        for coincidencia in patron.finditer(archivo.read_text(encoding="utf-8")):
            raiz = coincidencia.group(1).split(".")[0]
            if raiz == "kal_epistemics" or raiz in sys.stdlib_module_names:
                continue
            externos.add(raiz)

    assert externos == set(), (
        f"kal_epistemics ganó dependencias externas: {sorted(externos)}. "
        "Lo que necesite algo más pesado pertenece a kal-in, no a este paquete."
    )

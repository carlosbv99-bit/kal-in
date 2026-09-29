"""
Test estructural del choke point de `agent_core/routers/chat.py`.

"El gate no se puede saltear" no puede ser una convención: el endpoint tenía
CUATRO salidas distintas, y un gate en una sola es un gate que se saltea por las
otras tres. Este test lee el archivo y falla si aparece una quinta puerta con su
propio `"final_answer"`.

Límite honesto: es un chequeo sobre el TEXTO fuente, así que detecta la clave
literal `"final_answer"` y no una construida dinámicamente (p.ej. un dict armado
con `**{...}` o con una variable). Cubre el caso real — alguien agregando un
`return` nuevo copiando el de al lado — que es como aparecerían las puertas.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
FUENTE = (RAIZ / "agent_core" / "routers" / "chat.py").read_text(encoding="utf-8")


CLAVE = '"final_answer":'


def test_no_hay_ninguna_salida_de_respuesta_fuera_del_choke_point():
    # Se cuenta la CLAVE del dict (`"final_answer":`), no cualquier mención:
    # leerla (`respuesta["final_answer"]`) es legítimo y ocurre fuera del choke
    # point; construirla, no.
    total = FUENTE.count(CLAVE)

    inicio = FUENTE.index("def _respuesta_final(")
    fin = FUENTE.index("@router.post(", inicio)
    dentro = FUENTE[inicio:fin].count(CLAVE)

    assert dentro == 1, "el choke point tiene que construir la respuesta él mismo"
    assert total == dentro, (
        f"Hay {total - dentro} respuesta(s) construida(s) fuera de _respuesta_final(). "
        "Toda salida de /chat tiene que pasar por ahí, o el gate se puede saltear."
    )


def test_el_gate_solo_se_aplica_a_la_ruta_del_planner():
    """
    D8: las otras tres rutas pasan por el choke point pero NO se bloquean.

    Se busca el par completo `gate=True, evidence_before=...` y no `gate=True`
    suelto: el suelto aparece también en la prosa, y un test que cuenta prosa
    da falsos positivos (pasó al escribirlo).
    """
    assert FUENTE.count("_respuesta_final(") == 5  # definición + 4 llamadas
    assert FUENTE.count("gate=True, evidence_before=evidence_before") == 1, (
        "solo la ruta del planner puede bloquear (D8)"
    )

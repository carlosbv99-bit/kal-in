"""
Tests de kal_epistemics/abstention.py — "no lo sé" estable.

Lo que se prueba: que la abstención sea REPETIBLE. Un modelo sin registro de
su propio "no lo sé" contesta distinto cada vez que se le pregunta lo mismo;
la segunda vez, con la pregunta reformulada, improvisa. Acá eso se fija con
tests: dos paráfrasis de la misma pregunta producen byte a byte el mismo
texto, y el orden de los faltantes no cambia la salida.

También se prueba la contracara, que casi nunca se mide: los tres tipos NO
son intercambiables. `ILL_POSED` ("falta una premisa en la pregunta") gana
sobre los otros dos, porque es el caso más barato de resolver y el que un
"no sé" genérico taparía.
"""
from __future__ import annotations

from kal_epistemics import (
    AbstentionKind,
    Hypothesis,
    Question,
    classify_abstention,
    canonical_key,
    render_abstention,
)


def _q(texto: str) -> Question:
    return Question(id="q1", text=texto, canonical_key=canonical_key(texto))


def test_la_abstencion_es_estable_entre_llamadas():
    q = _q("¿Cuál es la causa de B?")

    a = render_abstention(q, AbstentionKind.IGNORANCE, missing=["el informe de auditoría"])
    b = render_abstention(q, AbstentionKind.IGNORANCE, missing=["el informe de auditoría"])

    assert a.text == b.text


def test_parafrasis_de_la_misma_pregunta_producen_la_misma_abstencion():
    a = render_abstention(
        _q("¿Cuál es la causa de B?"), AbstentionKind.IGNORANCE, missing=["el informe"]
    )
    b = render_abstention(
        _q("cual es la causa de B"), AbstentionKind.IGNORANCE, missing=["el informe"]
    )

    assert a.text == b.text
    # La clave canónica es la misma: es la misma pregunta, no dos.
    assert a.question_key == b.question_key


def test_el_orden_de_los_faltantes_no_cambia_la_salida():
    q = _q("¿Cuál es la causa de B?")

    a = render_abstention(q, AbstentionKind.IGNORANCE, missing=["el acta", "el informe"])
    b = render_abstention(q, AbstentionKind.IGNORANCE, missing=["el informe", "el acta"])

    assert a.text == b.text
    assert a.missing == ("el acta", "el informe")


def test_la_abstencion_no_depende_del_texto_de_la_pregunta():
    """
    Garantía explícita de estabilidad: el host agrega el texto de la pregunta
    si quiere mostrarlo; la capa epistémica garantiza el CONTENIDO. Si el
    texto de la pregunta entrara acá, dos paráfrasis darían dos abstenciones
    distintas y la métrica de "sin deriva" sería inmedible.
    """
    q = _q("¿Cuál es la causa de B?")
    abstencion = render_abstention(q, AbstentionKind.IGNORANCE, missing=["el informe"])

    assert "causa de B" not in abstencion.text
    assert abstencion.question_key == q.canonical_key


def test_ignorancia_nombra_el_deficit():
    abstencion = render_abstention(
        _q("¿X?"), AbstentionKind.IGNORANCE, missing=["el informe de auditoría"]
    )

    assert "No lo sé" in abstencion.text
    assert "el informe de auditoría" in abstencion.text


def test_subdeterminacion_no_promete_una_respuesta_que_no_puede_dar():
    abstencion = render_abstention(_q("¿X?"), AbstentionKind.UNDERDETERMINATION)

    assert "No puedo determinarlo" in abstencion.text
    assert "más de un" in abstencion.text


def test_pregunta_mal_planteada_dice_que_falta_una_condicion():
    abstencion = render_abstention(
        _q("¿Cuánto tarda?"),
        AbstentionKind.ILL_POSED,
        needs_condition="desde dónde hasta dónde",
    )

    assert "condición adicional" in abstencion.text
    assert "desde dónde hasta dónde" in abstencion.text


def test_los_tres_tipos_son_distintos_entre_si():
    q = _q("¿X?")
    textos = {
        render_abstention(q, kind).text
        for kind in (AbstentionKind.IGNORANCE, AbstentionKind.UNDERDETERMINATION, AbstentionKind.ILL_POSED)
    }

    assert len(textos) == 3


def test_clasificacion_sin_hipotesis_es_ignorancia_conocida():
    assert classify_abstention([]) is AbstentionKind.IGNORANCE


def test_clasificacion_con_hipotesis_es_subdeterminacion():
    h = Hypothesis(id="h1", claim="A podría causar B")

    assert classify_abstention([h]) is AbstentionKind.UNDERDETERMINATION


def test_falta_una_condicion_gana_sobre_los_otros_dos():
    """
    El caso accionable no se puede tapar con un "no sé" genérico: si falta
    una premisa en la pregunta, eso es lo que hay que decir, haya o no
    hipótesis registradas.
    """
    h = Hypothesis(id="h1", claim="A podría causar B")

    assert (
        classify_abstention([h], needs_condition="el rango temporal")
        is AbstentionKind.ILL_POSED
    )
    assert (
        classify_abstention([], needs_condition="el rango temporal")
        is AbstentionKind.ILL_POSED
    )


def test_is_abstention_text_reconoce_los_tres_tipos_renderizados():
    """
    Test anti-deriva entre el renderer y el gate: si alguien cambia la redacción
    de una abstención, el gate dejaría de reconocerla y empezaría a bloquear
    abstenciones legítimas. Esto lo ata.
    """
    from kal_epistemics import AbstentionKind, is_abstention_text

    q = _q("¿X?")
    for kind in (
        AbstentionKind.IGNORANCE,
        AbstentionKind.UNDERDETERMINATION,
        AbstentionKind.ILL_POSED,
    ):
        assert is_abstention_text(render_abstention(q, kind).text), kind

    assert is_abstention_text("El servicio se cayó por falta de memoria.") is False

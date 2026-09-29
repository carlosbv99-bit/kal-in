"""
Corpus de medición (F6): los DATOS con los que se mide, separados del código.

Por qué el corpus es un archivo de datos y no una lista en Python: el corpus es
el INSTRUMENTO. Un instrumento que vive mezclado con la implementación se
"ajusta" sin que nadie lo note, y a partir de ahí los números dejan de medir
nada. En este proyecto ya pasó tres veces midiendo con proxies en vez de leer
la cosa (ver `docs/HISTORY.md`: contar marcadores en comentarios, un `head -8`
truncado, una alarma de severidad sin leer el diff). Acá se corta ese patrón de
tres formas concretas:

1. **Evaluación y calibración son conjuntos disjuntos.** Los conjuntos A/B/C/R/D
   se usan SOLO para reportar; el conjunto K (calibración) es el único que se
   usa para elegir los marcadores de hedge y el umbral de respuesta larga. El
   loader levanta `CorpusInvalido` si un texto aparece en los dos: calibrar y
   evaluar sobre lo mismo es cómo se produce un número que no generaliza.
2. **El loader falla cerrado.** Etiqueta desconocida, campo faltante, id
   duplicado o expectativa vacía levantan. Un corpus mal formado que se carga
   igual produce un número con apariencia de rigor, que es peor que no medir.
3. **La huella del corpus se registra con el reporte** (`huella_corpus`), para
   que un número se pueda atribuir a la versión exacta del instrumento que lo
   produjo.

Qué NO es esto: un benchmark independiente. Las etiquetas las escribió el mismo
autor que escribió el gate, así que mide consistencia y error de un heurístico
sobre una muestra etiquetada — no mide al modelo, y no reemplaza a un conjunto
de evaluación externo.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DIRECTORIO_CORPUS = Path(__file__).resolve().parent / "corpus"
SCHEMA_VERSION = 1

CONJUNTOS: frozenset[str] = frozenset({"A", "B", "C", "R", "D"})
"""Conjuntos de EVALUACIÓN. `K` (calibración) va aparte a propósito."""

ETIQUETAS_RESPUESTA: frozenset[str] = frozenset({"afirmativa", "hedge", "abstencion"})
ETIQUETAS_CALIBRACION: frozenset[str] = frozenset({"hedge", "afirmativa", "conversacional"})
CONTEXTOS_CALIBRACION: frozenset[str] = frozenset(
    {"deficit_declarado", "turno_sin_observaciones"}
)
HERRAMIENTAS_DEL_MODELO: frozenset[str] = frozenset({"open_question", "record_hypothesis"})
"""Las únicas acciones que un item puede prescribirle al modelo: son las dos
tools epistémicas reales (ver `tool_integration/adapters/epistemic_tools.py`).
Cualquier otra cosa sería un corpus describiendo algo que el modelo no puede
hacer. FAIL-CLOSED: lo que no está acá no se carga."""

_ESPERADOS_VALIDOS: frozenset[str] = frozenset(
    {
        "gate_no_permite",
        "estado_final",
        "reapertura",
        "reaperturas_exactas",
        "evidencia_exogena",
        "flag",
        "dedup_en_sesion",
        "texto_abstencion_identico",
    }
)


class CorpusInvalido(Exception):
    """El corpus no se puede usar. No hay modo degradado: medir con un
    instrumento roto es peor que no medir."""


@dataclass(frozen=True)
class Accion:
    """Lo que el modelo hizo, expresado como la llamada a la tool real."""

    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class Observacion:
    """Lo que devolvió el mundo. El contenido es DATO, nunca instrucción."""

    tool: str
    contenido: str


@dataclass(frozen=True)
class Esperado:
    """
    Qué tiene que pasar. Cada campo es una verificación MECÁNICA; `None`
    significa "no se verifica nada de esto", nunca "da igual".

    `gate_no_permite=True` significa "esto no puede salir como un hecho": se
    cumple con BLOQUEAR o con ADVERTIR (marcado). Los dos casos se cuentan
    aparte porque significan cosas distintas.
    """

    gate_no_permite: bool | None = None
    estado_final: str | None = None
    reapertura: bool | None = None
    reaperturas_exactas: int | None = None
    evidencia_exogena: int | None = None
    flag: str | None = None
    dedup_en_sesion: bool | None = None
    texto_abstencion_identico: bool | None = None


@dataclass(frozen=True)
class Item:
    """Un escenario: lo que preguntó el usuario, lo que hizo el modelo, lo que
    trajo el mundo, lo que el modelo quiso responder, y qué tiene que pasar."""

    id: str
    conjunto: str
    etiqueta: str
    esperado: Esperado
    pregunta: str = ""
    acciones: tuple[Accion, ...] = ()
    observaciones: tuple[Observacion, ...] = ()
    respuesta_final: str = ""
    etiqueta_respuesta: str = ""
    # Solo para el conjunto D (deriva): la misma pregunta con N redacciones.
    variantes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemCalibracion:
    """Una respuesta etiquetada a mano: tiene hedge o afirma. Es lo único que
    se usa para elegir los marcadores y el umbral."""

    id: str
    texto: str
    etiqueta: str  # hedge | afirmativa
    contexto: str  # deficit_declarado | turno_sin_observaciones


@dataclass(frozen=True)
class Corpus:
    items: tuple[Item, ...]
    calibracion: tuple[ItemCalibracion, ...]
    candidatos_marcador: tuple[str, ...]
    huella: str
    por_conjunto: Mapping[str, tuple[Item, ...]] = field(default_factory=dict)

    def del_conjunto(self, conjunto: str) -> tuple[Item, ...]:
        return self.por_conjunto.get(conjunto, ())


def _exigir(condicion: bool, mensaje: str) -> None:
    if not condicion:
        raise CorpusInvalido(mensaje)


def _texto_no_vacio(valor: Any, donde: str) -> str:
    _exigir(isinstance(valor, str) and valor.strip() != "", f"{donde}: texto vacío o ausente")
    return valor


def _leer_json(ruta: Path) -> Mapping[str, Any]:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorpusInvalido(f"{ruta.name}: JSON inválido — {e}") from None
    _exigir(isinstance(datos, dict), f"{ruta.name}: la raíz tiene que ser un objeto")
    _exigir(
        datos.get("schema_version") == SCHEMA_VERSION,
        f"{ruta.name}: schema_version {datos.get('schema_version')!r} != {SCHEMA_VERSION}",
    )
    return datos


def _parsear_accion(datos: Any, donde: str) -> Accion:
    _exigir(isinstance(datos, dict), f"{donde}: la acción tiene que ser un objeto")
    tool = _texto_no_vacio(datos.get("tool"), f"{donde}.tool")
    _exigir(
        tool in HERRAMIENTAS_DEL_MODELO,
        f"{donde}.tool='{tool}' no es una tool epistémica del modelo "
        f"(válidas: {sorted(HERRAMIENTAS_DEL_MODELO)})",
    )
    args = datos.get("args", {})
    _exigir(isinstance(args, dict), f"{donde}.args tiene que ser un objeto")
    if tool == "open_question":
        _texto_no_vacio(args.get("question"), f"{donde}.args.question")
    else:
        _texto_no_vacio(args.get("claim"), f"{donde}.args.claim")
    return Accion(tool=tool, args=args)


def _parsear_observacion(datos: Any, donde: str) -> Observacion:
    _exigir(isinstance(datos, dict), f"{donde}: la observación tiene que ser un objeto")
    return Observacion(
        tool=_texto_no_vacio(datos.get("tool"), f"{donde}.tool"),
        contenido=_texto_no_vacio(datos.get("contenido"), f"{donde}.contenido"),
    )


def _parsear_esperado(datos: Any, donde: str) -> Esperado:
    _exigir(isinstance(datos, dict), f"{donde}.esperado tiene que ser un objeto")
    _exigir(datos != {}, f"{donde}: un item sin ninguna expectativa no mide nada")
    desconocidas = set(datos) - _ESPERADOS_VALIDOS
    _exigir(not desconocidas, f"{donde}: expectativas desconocidas {sorted(desconocidas)}")
    for clave in ("gate_no_permite", "reapertura", "dedup_en_sesion", "texto_abstencion_identico"):
        if clave in datos:
            _exigir(
                isinstance(datos[clave], bool),
                f"{donde}.esperado.{clave} tiene que ser booleano",
            )
    if "evidencia_exogena" in datos:
        _exigir(
            isinstance(datos["evidencia_exogena"], int) and datos["evidencia_exogena"] >= 0,
            f"{donde}.esperado.evidencia_exogena tiene que ser un entero >= 0",
        )
    if "reaperturas_exactas" in datos:
        _exigir(
            isinstance(datos["reaperturas_exactas"], int) and datos["reaperturas_exactas"] >= 0,
            f"{donde}.esperado.reaperturas_exactas tiene que ser un entero >= 0",
        )
    return Esperado(
        gate_no_permite=datos.get("gate_no_permite"),
        estado_final=datos.get("estado_final"),
        reapertura=datos.get("reapertura"),
        reaperturas_exactas=datos.get("reaperturas_exactas"),
        evidencia_exogena=datos.get("evidencia_exogena"),
        flag=datos.get("flag"),
        dedup_en_sesion=datos.get("dedup_en_sesion"),
        texto_abstencion_identico=datos.get("texto_abstencion_identico"),
    )


def _parsear_item(datos: Any, conjunto: str, ruta: Path) -> Item:
    _exigir(isinstance(datos, dict), f"{ruta.name}: cada item tiene que ser un objeto")
    ident = _texto_no_vacio(datos.get("id"), f"{ruta.name}: item sin id")
    donde = f"{ruta.name}:{ident}"
    _exigir(
        ident.startswith(conjunto),
        f"{donde}: el id tiene que empezar con el conjunto '{conjunto}'",
    )
    esperado = _parsear_esperado(datos.get("esperado"), donde)
    etiqueta_respuesta = datos.get("etiqueta_respuesta", "")
    if etiqueta_respuesta:
        _exigir(
            etiqueta_respuesta in ETIQUETAS_RESPUESTA,
            f"{donde}: etiqueta_respuesta '{etiqueta_respuesta}' desconocida",
        )
    variantes = tuple(datos.get("variantes", ()))
    if conjunto == "D":
        _exigir(len(variantes) >= 2, f"{donde}: un item de deriva necesita >= 2 variantes")
        for v in variantes:
            _texto_no_vacio(v, f"{donde}.variantes")
    acciones = tuple(
        _parsear_accion(a, f"{donde}.acciones[{i}]")
        for i, a in enumerate(datos.get("acciones", ()))
    )
    observaciones = tuple(
        _parsear_observacion(o, f"{donde}.observaciones[{i}]")
        for i, o in enumerate(datos.get("observaciones", ()))
    )
    if conjunto != "D":
        # Un item tiene que ejercitar ALGO: o el camino del ledger (acciones del
        # modelo y observaciones del mundo) o el gate (una respuesta final). Un
        # item vacío daría un `pass` que no mide nada.
        _exigir(
            acciones or observaciones or datos.get("respuesta_final"),
            f"{donde}: un item sin acciones, observaciones ni respuesta no ejercita nada",
        )
    respuesta = datos.get("respuesta_final", "")
    _exigir(isinstance(respuesta, str), f"{donde}: respuesta_final tiene que ser texto")
    if conjunto in {"A", "B", "C"}:
        _texto_no_vacio(respuesta, f"{donde}.respuesta_final")
        _exigir(
            etiqueta_respuesta != "",
            f"{donde}: falta etiqueta_respuesta (afirmativa/hedge/abstencion)",
        )
    return Item(
        id=ident,
        conjunto=conjunto,
        etiqueta=_texto_no_vacio(datos.get("etiqueta"), f"{donde}.etiqueta"),
        esperado=esperado,
        pregunta=datos.get("pregunta", ""),
        acciones=acciones,
        observaciones=observaciones,
        respuesta_final=respuesta,
        etiqueta_respuesta=etiqueta_respuesta,
        variantes=variantes,
        missing=tuple(datos.get("missing", ())),
    )


def _parsear_calibracion(datos: Any, ruta: Path) -> ItemCalibracion:
    _exigir(isinstance(datos, dict), f"{ruta.name}: item de calibración inválido")
    ident = _texto_no_vacio(datos.get("id"), f"{ruta.name}: item sin id")
    donde = f"{ruta.name}:{ident}"
    _exigir(ident.startswith("K"), f"{donde}: los ids de calibración empiezan con 'K'")
    etiqueta = _texto_no_vacio(datos.get("etiqueta"), f"{donde}.etiqueta")
    _exigir(
        etiqueta in ETIQUETAS_CALIBRACION,
        f"{donde}: etiqueta '{etiqueta}' inválida ({sorted(ETIQUETAS_CALIBRACION)})",
    )
    contexto = _texto_no_vacio(datos.get("contexto"), f"{donde}.contexto")
    _exigir(
        contexto in CONTEXTOS_CALIBRACION,
        f"{donde}: contexto '{contexto}' inválido ({sorted(CONTEXTOS_CALIBRACION)})",
    )
    return ItemCalibracion(
        id=ident,
        texto=_texto_no_vacio(datos.get("texto"), f"{donde}.texto"),
        etiqueta=etiqueta,
        contexto=contexto,
    )


def _normalizar(texto: str) -> str:
    return " ".join(texto.casefold().split())


def _validar_disjuncion(corpus: Corpus) -> None:
    """
    El mismo texto no puede estar en el conjunto de calibración y en uno de
    evaluación. Sin este chequeo, el "resultado" del conjunto B puede ser
    simplemente el recuerdo de lo que se usó para calibrar.
    """
    de_evaluacion = {_normalizar(i.respuesta_final) for i in corpus.items if i.respuesta_final}
    for item in corpus.calibracion:
        _exigir(
            _normalizar(item.texto) not in de_evaluacion,
            f"calibración/evaluación comparten el texto de '{item.id}': calibrar y "
            "evaluar sobre lo mismo invalida la medición",
        )


def cargar_corpus(directorio: Path | None = None) -> Corpus:
    """Carga y valida TODOS los archivos del corpus. Fail-closed."""
    raiz = Path(directorio) if directorio is not None else DIRECTORIO_CORPUS
    rutas = sorted(raiz.glob("*.json"))
    _exigir(bool(rutas), f"no hay archivos de corpus en {raiz}")

    items: list[Item] = []
    calibracion: list[ItemCalibracion] = []
    candidatos: tuple[str, ...] = ()
    ids: set[str] = set()
    partes: list[str] = []

    for ruta in rutas:
        datos = _leer_json(ruta)
        partes.append(json.dumps(datos, sort_keys=True, ensure_ascii=False))
        conjunto = datos.get("conjunto")
        if conjunto == "K":
            for crudo in datos.get("items", ()):
                item_c = _parsear_calibracion(crudo, ruta)
                _exigir(item_c.id not in ids, f"id duplicado en el corpus: {item_c.id}")
                ids.add(item_c.id)
                calibracion.append(item_c)
            candidatos = tuple(datos.get("candidatos_marcador", ()))
            continue

        _exigir(
            conjunto in CONJUNTOS,
            f"{ruta.name}: conjunto '{conjunto}' desconocido (válidos: {sorted(CONJUNTOS)})",
        )
        for crudo in datos.get("items", ()):
            item = _parsear_item(crudo, conjunto, ruta)
            _exigir(item.id not in ids, f"id duplicado en el corpus: {item.id}")
            ids.add(item.id)
            items.append(item)

    _exigir(bool(items), "el corpus no tiene items de evaluación")
    _exigir(bool(calibracion), "el corpus no tiene items de calibración")

    por_conjunto: dict[str, tuple[Item, ...]] = {}
    for conjunto in sorted(CONJUNTOS):
        del_conjunto = tuple(i for i in items if i.conjunto == conjunto)
        if del_conjunto:
            por_conjunto[conjunto] = del_conjunto

    huella = hashlib.sha256("\n".join(partes).encode("utf-8")).hexdigest()
    corpus = Corpus(
        items=tuple(items),
        calibracion=tuple(calibracion),
        candidatos_marcador=candidatos,
        huella=huella,
        por_conjunto=por_conjunto,
    )
    _validar_disjuncion(corpus)
    return corpus


def tamanos(corpus: Corpus) -> Mapping[str, int]:
    """Cuántos items hay por conjunto. Va en el reporte: un porcentaje sin su
    denominador no dice nada."""
    resultado = {c: len(corpus.del_conjunto(c)) for c in sorted(CONJUNTOS) if corpus.del_conjunto(c)}
    resultado["K"] = len(corpus.calibracion)
    return resultado

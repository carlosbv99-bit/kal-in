"""
Compactación por presupuesto: qué se tira y qué NO se tira NUNCA.

Un ledger durable sin compactación crece para siempre, y uno que compacta mal
pierde justo lo que lo hacía útil. La regla que ordena todo este módulo está en
el roadmap de F4 y es una sola:

    Se tira el DETALLE de las hipótesis largas; se conservan el DÉFICIT y la
    razón de la abstención, que es la parte que evita reinventar la misma
    investigación dentro de seis meses.

De ahí salen las decisiones, que son deliberadas y no cosméticas:

- **Nunca se compacta una hipótesis viva** (`OPEN`, `REACTIVATED`, `WEAKENED`,
  `SUSPENDED`): es una deuda abierta, y borrarle el texto es perder el hilo de
  algo que sigue pendiente. Solo se compactan las terminales (`REFUTED`,
  `SUPERSEDED`, `UNDETERMINABLE`), y solo cuando hay más de las que el
  presupuesto permite por pregunta.
- **Nunca se toca nada estructural**: estado, vínculos de evidencia,
  dependencias, condiciones de reapertura y `question_id` quedan intactos. Si se
  tiraran los vínculos, una hipótesis `SUPPORTED` dejaría de poder validarse al
  recargar (el ledger exige respaldo exógeno) y la compactación se convertiría en
  una forma de corromper el estado. Si se tiraran las condiciones de reapertura,
  una hipótesis refutada nunca más podría reabrirse — que es exactamente lo
  contrario de para qué se conserva.
- **Una hipótesis refutada sigue siendo reabrible después de compactada.** Es el
  criterio que separa "compactar" de "tirar".
- **Nunca promueve**. La compactación no cambia ningún estado: no hay ningún
  camino acá que suba una hipótesis a conocimiento, ni por antigüedad, ni por
  repetición, ni por haber sobrevivido sin ser refutada (los tres casos que el
  roadmap prohíbe explícitamente).
- **Es idempotente**: compactar dos veces con el mismo presupuesto no cambia
  nada. Sin eso, cada escritura del store durable dejaría una marca nueva en el
  historial y el archivo crecería igual que sin compactar.

Este módulo es lógica pura: no sabe dónde se guarda nada (eso es la decisión D4,
todavía abierta). El ledger lo llama cuando el store lo pida.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from kal_epistemics.models import (
    Flag,
    HistoryEntry,
    Hypothesis,
    HypothesisStatus,
)

ESTADOS_TERMINALES: frozenset[HypothesisStatus] = frozenset(
    {
        HypothesisStatus.REFUTED,
        HypothesisStatus.SUPERSEDED,
        HypothesisStatus.UNDETERMINABLE,
    }
)
"""Estados que ya no son una deuda abierta. `REFUTED` está acá y sigue siendo
reabrible (tiene condiciones de reapertura): terminar no es desaparecer."""

_KIND_MARCA = "compacted"


@dataclass(frozen=True)
class PresupuestoDeCompactacion:
    """Cuánto detalle se conserva. Todo explícito: una compactación con
    parámetros implícitos es imposible de auditar."""

    max_hipotesis_terminales_por_pregunta: int = 3
    max_caracteres_de_claim: int = 200
    max_entradas_de_historial: int = 3

    def __post_init__(self) -> None:
        if self.max_hipotesis_terminales_por_pregunta < 0:
            raise ValueError("max_hipotesis_terminales_por_pregunta no puede ser negativo")
        if self.max_caracteres_de_claim < 1:
            raise ValueError("max_caracteres_de_claim tiene que ser >= 1")
        if self.max_entradas_de_historial < 1:
            raise ValueError("max_entradas_de_historial tiene que ser >= 1")


@dataclass(frozen=True)
class CompactionReport:
    """Qué se hizo, para que quede en el log y se pueda auditar. Una
    compactación silenciosa es indistinguible de una pérdida de datos."""

    hipotesis_compactadas: tuple[str, ...] = ()
    caracteres_ahorrados: int = 0
    entradas_de_historial_quitadas: int = 0
    deficits_pendientes_conservados: int = 0

    @property
    def hubo_cambios(self) -> bool:
        return bool(self.hipotesis_compactadas)


def planificar(
    hipotesis: Sequence[Hypothesis], presupuesto: PresupuestoDeCompactacion
) -> tuple[str, ...]:
    """
    Qué hipótesis compactar, en orden determinista (primero las más viejas de
    cada pregunta). Pura: no toca nada.

    Se agrupa por pregunta —el déficit y su razón viven ahí— y dentro de cada
    grupo se conservan las terminales más RECIENTES: las viejas son las que ya
    no aportan y las que más espacio ocupan.
    """
    por_pregunta: dict[str, list[Hypothesis]] = {}
    for h in hipotesis:
        if h.status not in ESTADOS_TERMINALES:
            continue  # una hipótesis viva no se compacta nunca
        por_pregunta.setdefault(h.question_id, []).append(h)

    elegidas: list[str] = []
    for question_id in sorted(por_pregunta):
        del_grupo = sorted(por_pregunta[question_id], key=lambda h: (h.created_at, h.id))
        sobran = len(del_grupo) - presupuesto.max_hipotesis_terminales_por_pregunta
        if sobran > 0:
            elegidas.extend(h.id for h in del_grupo[:sobran])
    return tuple(elegidas)


def _marca_previa(h: Hypothesis) -> HistoryEntry | None:
    for entrada in reversed(h.history):
        if entrada.kind == _KIND_MARCA:
            return entrada
    return None


def _valor(entrada: HistoryEntry, clave: str) -> str:
    for k, v in entrada.detail:
        if k == clave:
            return v
    return ""


def compactar_hipotesis(
    h: Hypothesis, presupuesto: PresupuestoDeCompactacion, *, at: float
) -> Hypothesis:
    """
    Devuelve la hipótesis compactada, o LA MISMA si no había nada que hacer.

    Que devuelva el mismo objeto cuando no cambia nada es lo que hace la
    operación idempotente y detectable por identidad: el ledger cuenta cambios
    comparando, no adivinando.
    """
    marca = _marca_previa(h)

    claim = h.claim
    if len(claim) > presupuesto.max_caracteres_de_claim:
        claim = claim[: presupuesto.max_caracteres_de_claim].rstrip() + "…"

    # Las marcas de compactaciones anteriores no cuentan como historial: si no,
    # cada corrida agregaría una y el archivo crecería igual que sin compactar.
    sin_marcas = tuple(e for e in h.history if e.kind != _KIND_MARCA)
    maximo = presupuesto.max_entradas_de_historial
    if maximo == 1:
        # Presupuesto mínimo: el historial se reduce a la marca. Sigue sin
        # perderse ni el estado ni las condiciones de reapertura.
        conservar: tuple[HistoryEntry, ...] = ()
    else:
        cupo = maximo - 1  # una entrada se reserva para la marca
        if len(sin_marcas) <= cupo:
            conservar = sin_marcas
        else:
            primera = sin_marcas[:1]
            ultimas = sin_marcas[-(cupo - 1) :] if cupo > 1 else ()
            conservar = primera + tuple(e for e in ultimas if e not in primera)

    if claim == h.claim and conservar == sin_marcas:
        return h  # nada que tirar (ya está compactada o nunca hizo falta)

    # La marca conserva el tamaño ORIGINAL, no el de la última corrida: es el
    # dato que dice cuánto se perdió, y perderlo sería perder la auditoría.
    caracteres_originales = _valor(marca, "claim_chars_originales") if marca else str(len(h.claim))
    entradas_originales = _valor(marca, "history_entries_originales") if marca else str(len(h.history))
    nueva_marca = HistoryEntry(
        at=at,
        kind=_KIND_MARCA,
        reason="detalle descartado por presupuesto; el déficit y la razón de la abstención se conservan",
        detail=(
            ("claim_chars_originales", caracteres_originales),
            ("claim_chars_conservados", str(len(claim))),
            ("history_entries_originales", entradas_originales),
            ("history_entries_conservadas", str(len(conservar))),
        ),
    )
    return replace(h, claim=claim, history=conservar + (nueva_marca,))


def aplicar(
    hipotesis: Iterable[Hypothesis],
    presupuesto: PresupuestoDeCompactacion,
    *,
    at: float,
    deficits_pendientes: int = 0,
) -> tuple[Mapping[str, Hypothesis], CompactionReport]:
    """
    Compacta la lista y devuelve las que CAMBIARON (id → hipótesis nueva) más el
    reporte. Las que no cambiaron no aparecen: así el llamador escribe solo lo
    que se movió.
    """
    identificadas = planificar(tuple(hipotesis), presupuesto)
    por_id = {h.id: h for h in hipotesis}
    resultado: dict[str, Hypothesis] = {}
    ahorrados = 0
    entradas_quitadas = 0

    for hid in identificadas:
        original = por_id[hid]
        compactada = compactar_hipotesis(original, presupuesto, at=at)
        if compactada == original:
            continue
        resultado[hid] = compactada
        ahorrados += len(original.claim) - len(compactada.claim)
        entradas_quitadas += len(original.history) - len(compactada.history) + 1

    return resultado, CompactionReport(
        hipotesis_compactadas=tuple(sorted(resultado)),
        caracteres_ahorrados=max(0, ahorrados),
        entradas_de_historial_quitadas=max(0, entradas_quitadas),
        deficits_pendientes_conservados=deficits_pendientes,
    )


def conserva_lo_irrenunciable(original: Hypothesis, compactada: Hypothesis) -> bool:
    """
    Verificación explícita de lo que la compactación NO puede tocar. El ledger
    la corre después de compactar: es barato y convierte una promesa del
    docstring en una condición ejecutable.

    No se verifica "no promueve" porque es estructural: no hay ninguna rama acá
    que escriba `status`.
    """
    return (
        original.id == compactada.id
        and original.status == compactada.status
        and original.question_id == compactada.question_id
        and original.evidence_links == compactada.evidence_links
        and original.depends_on == compactada.depends_on
        and original.reopen_conditions == compactada.reopen_conditions
        and original.validity_conditions == compactada.validity_conditions
        and original.created_at == compactada.created_at
    )


def flags_no_cambian(flags_antes: Iterable[Flag], flags_despues: Iterable[Flag]) -> bool:
    """Las marcas derivadas se calculan sobre el grafo; si la compactación las
    cambiara, estaría tocando algo estructural que dice no tocar."""
    return frozenset(flags_antes) == frozenset(flags_despues)

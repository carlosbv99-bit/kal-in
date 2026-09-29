"""
El seam del ledger durable: dónde se guarda el estado, y qué significa "el
mismo registro".

Este módulo es deliberadamente chico y NO elige backend. Define dos cosas que
son contrato y que la implementación de almacenamiento no puede decidir sola:

1. **Qué operaciones necesita el ledger de un almacén** (`AlmacenDurable`:
   cargar, guardar, descartar, ámbitos). Cualquier backend —un archivo JSONL,
   SQLite, el backend de memoria del host— implementa estas cuatro y nada más.
   El ledger no sabe ni le importa cuál es.
2. **Qué significa "la misma pregunta"** cuando la pregunta se hace en OTRA
   sesión: el criterio de aceptación de F4 ("la misma pregunta en otra sesión no
   crea un registro nuevo") se cumple porque las sesiones del mismo ÁMBITO
   comparten el estado guardado, y `canonical_key` es la clave. Sin ámbito, la
   dedup entre sesiones es imposible; con un ámbito por sesión, no existe.

**El ámbito es un parámetro, no una política** (decisión D5, todavía abierta):

- un ámbito por instalación → todas las sesiones comparten (lo más simple, y lo
  correcto mientras haya una sola persona usando la instalación);
- un ámbito por usuario → cada usuario tiene su registro, y dos usuarios que
  preguntan lo mismo no se ven entre sí;
- sin ámbito (global) → como el primero, pero sin la clave que permite separar
  después.

La pieza de abajo sirve para los tres: no decide cuál, lo recibe. Lo que sí
decide —y por eso está acá y no en el host— es que **el ámbito sea explícito**:
la alternativa (que el aislamiento surja del objeto que uno tenga a mano, como
pasaba con el ledger por sesión de F2) es exactamente cómo se termina compartiendo
estado entre usuarios sin haberlo decidido. Hay un test que exige que dos ámbitos
distintos no se vean.

**Un solo escritor por ámbito, y hay que decirlo.** `guardar()` escribe el
snapshot completo: si dos procesos (o dos objetos) cargan el mismo ámbito y
guardan los dos, el último pisa al primero. En un agente de un solo proceso —que
es lo que es kal-in— eso no ocurre porque hay un único ledger vivo por ámbito. Un
backend con concurrencia real (SQLite en modo WAL, o un lock de archivo) tiene que
resolverlo él; el port no inventa un protocolo de concurrencia antes de saber
sobre qué backend se apoya (decisión D4).

**El almacén en memoria pasa por JSON a propósito.** Es un doble fiel de un
backend de verdad: si el estado tiene algo que no se puede serializar, o el
round-trip pierde un dato, esto falla en los tests y no en producción.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from kal_epistemics.compaction import CompactionReport, PresupuestoDeCompactacion
from kal_epistemics.diagnosis import Diagnostico, diagnosticar
from kal_epistemics.ledger import HypothesisLedger

AMBITO_POR_DEFECTO = "instalacion"
"""Ámbito único: todas las sesiones comparten el mismo registro. Es el default
porque kal-in es de una sola persona, y es un default EXPLÍCITO — el día que
haya multiusuario, la decisión D5 cambia este valor por el id del usuario, sin
migrar la forma de nada."""


@runtime_checkable
class AlmacenDurable(Protocol):
    """
    Lo que el ledger necesita de donde sea que guarde. Cuatro operaciones, y
    ninguna conoce el formato interno del ledger: todo es el snapshot.

    `cargar` devuelve `None` cuando el ámbito no existe (no levanta): un ámbito
    nuevo es un caso normal, no un error.
    """

    def cargar(self, ambito: str) -> Mapping[str, Any] | None:
        """Estado guardado del ámbito, o `None` si no hay nada."""
        ...

    def guardar(self, ambito: str, snapshot: Mapping[str, Any]) -> None:
        """Reemplaza el estado del ámbito por este snapshot."""
        ...

    def descartar(self, ambito: str) -> None:
        """Borra el ámbito. Es explícito a propósito: no hay borrado implícito."""
        ...

    def ambitos(self) -> tuple[str, ...]:
        """Qué ámbitos hay guardados, en orden estable."""
        ...


class AlmacenEnMemoria:
    """
    Backend de proceso: no sobrevive a un reinicio, y no pretende hacerlo.

    Existe para dos cosas: correr sin persistencia (tests, CI, y un agente que se
    niega a escribir en disco) y servir de doble fiel de un backend real — pasa
    por JSON en las dos direcciones, así que el estado guardado son datos, no
    referencias a objetos vivos del ledger.
    """

    def __init__(self) -> None:
        self._datos: dict[str, str] = {}

    def cargar(self, ambito: str) -> Mapping[str, Any] | None:
        crudo = self._datos.get(ambito)
        return None if crudo is None else json.loads(crudo)

    def guardar(self, ambito: str, snapshot: Mapping[str, Any]) -> None:
        self._datos[ambito] = json.dumps(snapshot, sort_keys=True)

    def descartar(self, ambito: str) -> None:
        self._datos.pop(ambito, None)

    def ambitos(self) -> tuple[str, ...]:
        return tuple(sorted(self._datos))


@dataclass
class LedgerDurable:
    """
    Un `HypothesisLedger` atado a un almacén y a un ámbito: **se abre desde el
    estado guardado y se guarda cuando el host lo pide**.

    Guardar es explícito y no automático. Automatizarlo en cada mutación del
    ledger obligaría a enganchar cada camino que cambia estado (y a olvidarse de
    uno), y además haría que el costo de escribir lo decidiera la librería en vez
    del host: en un turno con cinco observaciones, ¿cinco escrituras? El host
    sabe cuándo termina algo; la librería no.

    `recargar()` existe para el caso opuesto: volver a leer el estado del almacén
    descartando lo que haya en memoria (por ejemplo, si otro componente guardó).
    Está explícito para que nadie lo confunda con una operación inocua.
    """

    almacen: AlmacenDurable
    ambito: str = AMBITO_POR_DEFECTO
    clock: Callable[[], float] = time.time
    presupuesto: PresupuestoDeCompactacion = field(default_factory=PresupuestoDeCompactacion)

    def __post_init__(self) -> None:
        self._diagnostico: Diagnostico = Diagnostico()
        self._ledger: HypothesisLedger = self._abrir()
        self._huella_guardada: str = self._huella()

    def _abrir(self) -> HypothesisLedger:
        snapshot = self.almacen.cargar(self.ambito)
        if snapshot is None:
            self._diagnostico = diagnosticar(HypothesisLedger(clock=self.clock))
            return HypothesisLedger(clock=self.clock)
        # `from_snapshot` falla cerrado: un estado inconsistente o de otra
        # versión no se reinterpreta, se levanta.
        ledger = HypothesisLedger.from_snapshot(snapshot, clock=self.clock)
        # Un estado que PASÓ la carga todavía puede no tener sentido: la carga
        # valida estructura (referencias, esquema, claves canónicas), no que la
        # historia sea coherente. Se diagnostica acá, se guarda el resultado y el
        # HOST decide qué hacer con él (la librería no tiene política de log): lo
        # que no se hace es gobernar en silencio sobre un estado absurdo.
        self._diagnostico = diagnosticar(ledger)
        return ledger

    @property
    def diagnostico_en_la_carga(self) -> "Diagnostico":
        """
        Diagnóstico del estado tal como se cargó (o del estado actual, tras
        `recargar()`). Es una foto, no un monitor en vivo: para el estado actual,
        `kal_epistemics.diagnosticar(registro.ledger)`.
        """
        return self._diagnostico

    @property
    def ledger(self) -> HypothesisLedger:
        return self._ledger

    def _huella(self) -> str:
        """Hash del estado actual. Es el mismo cálculo que haría un backend para
        deduplicar escrituras, así que sirve para no reescribir de gusto."""
        return hashlib.sha256(
            json.dumps(self._ledger.snapshot(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def guardar(self) -> Mapping[str, Any]:
        """Guarda el estado actual y devuelve el snapshot que se guardó (para
        poder loguear su hash sin volver a calcularlo)."""
        snapshot = self._ledger.snapshot()
        self.almacen.guardar(self.ambito, snapshot)
        self._huella_guardada = self._huella()
        return snapshot

    def guardar_si_cambio(self) -> bool:
        """
        Guarda solo si el estado cambió desde el último guardado (o desde la
        carga inicial). Devuelve si escribió.

        Existe por el costo real: la mayoría de los turnos no tocan el ledger, y
        un backend de archivo no debería reescribir todo su contenido en cada
        uno. La decisión de *cuándo* llamarlo sigue siendo del host.
        """
        if self._huella() == self._huella_guardada:
            return False
        self.guardar()
        return True

    def recargar(self) -> HypothesisLedger:
        """Descarta lo que haya en memoria y vuelve a leer el almacén."""
        self._ledger = self._abrir()
        self._huella_guardada = self._huella()
        return self._ledger

    def compactar(self) -> CompactionReport:
        """Compacta y, si hubo cambios, guarda. Compactar sin guardar dejaría el
        almacén con el detalle que se acaba de tirar de memoria."""
        reporte = self._ledger.compact(self.presupuesto)
        if reporte.hubo_cambios:
            self.guardar()
        return reporte

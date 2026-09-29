"""
El backend SQLite del ledger durable (decisión D4, resuelta el 2026-09-29).

Por qué SQLite y no JSONL, que era la otra opción:

- El port tiene una operación que **reemplaza el estado completo de un ámbito**
  (`guardar(ambito, snapshot)`), no escritura incremental por evento. Con JSONL
  eso termina siendo un key-value casero sobre líneas: reescribir el archivo
  entero en cada guardado, con `tempfile` + `os.replace` a mano y un lock propio.
  Es viable, pero es inventar el protocolo de concurrencia que el port dice
  explícitamente que no quiere inventar.
- SQLite ya trae ese protocolo probado (WAL, locking, transacciones). El precio
  es cero: `sqlite3` es stdlib, así que este archivo vive en `kal_epistemics/`
  junto al resto del paquete portable.
- El precedente es del propio repo: `agent_core/memory/mid_term.py` usa
  `sqlite3` crudo, sin ORM, con archivo propio. Mismo patrón, mismo nivel de
  simplicidad, cero dependencia nueva.
- La auditabilidad no se pierde: `sqlite3 data/epistemics/ledger.db "select
  snapshot from ledger where ambito='instalacion'" | jq` es igual de inspeccionable
  que un `grep` sobre líneas — y acá cada fila es el snapshot completo de un
  ámbito, que es un blob grande, no una secuencia de eventos discretos como
  `audit_log.py`.

**Qué resuelve y qué NO resuelve de la concurrencia, porque la diferencia
importa.** WAL + transacciones garantizan que dos escritores simultáneos no
corrompan la base, no se pisen a mitad de escritura y no se queden trabados con
un "database is locked" a la primera. Lo que NO cambia es la semántica del port:
`guardar()` reemplaza el snapshot entero, así que **dos escritores vivos del
mismo ámbito siguen siendo last-write-wins** (gana el último que guarda) — eso
es del contrato, no del backend, y está declarado con un test propio en
`test_kal_epistemics_durable.py`. Para que un escritor no pierda cambios tiene
que recargar antes de guardar, o el día que haga falta habrá que agregarle al
snapshot una revisión optimista. Hoy no ocurre: hay un único ledger vivo por
ámbito.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from kal_epistemics.errors import EstadoIlegible

RUTA_POR_DEFECTO = Path("data/epistemics/ledger.db")
"""Mismo criterio que `agent_core/memory/mid_term.py`: archivo propio, en
`data/` (ignorado por git), y el host decide la ruta si quiere otra."""


class AlmacenSQLite:
    """
    Implementa `AlmacenDurable` sobre una tabla de una fila por ámbito.

    El schema es deliberadamente mínimo —ámbito, snapshot, cuándo— porque todo
    lo que hay que saber está adentro del snapshot, versionado por
    `schema_version`. Agregar columnas por campo sería duplicar el contrato del
    ledger en el schema de la base, y ahí es donde las dos versiones empiezan a
    discrepar.
    """

    def __init__(self, ruta: Path | str = RUTA_POR_DEFECTO, *, timeout: float = 10.0) -> None:
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` + lock propio: el endpoint /chat corre en el
        # threadpool de FastAPI, así que el store puede ser tocado desde otro
        # hilo que el que lo creó. Mismo criterio que mid_term.py.
        self._conn = sqlite3.connect(self.ruta, check_same_thread=False, timeout=timeout)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger (
                    ambito TEXT PRIMARY KEY,
                    snapshot TEXT NOT NULL,
                    actualizado_en REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    # -- AlmacenDurable ----------------------------------------------------

    def cargar(self, ambito: str) -> Mapping[str, Any] | None:
        with self._lock:
            fila = self._conn.execute(
                "SELECT snapshot FROM ledger WHERE ambito = ?", (ambito,)
            ).fetchone()
        if fila is None:
            return None
        try:
            datos = json.loads(fila[0])
        except (TypeError, json.JSONDecodeError) as e:
            raise EstadoIlegible(
                f"El estado guardado del ámbito '{ambito}' no es JSON legible ({e}). "
                "No se reinterpreta: hay que recuperarlo o descartarlo explícitamente."
            ) from None
        if not isinstance(datos, dict):
            raise EstadoIlegible(
                f"El estado guardado del ámbito '{ambito}' no es un objeto."
            )
        return datos

    def guardar(self, ambito: str, snapshot: Mapping[str, Any]) -> None:
        # `sort_keys` no es cosmético: hace que el mismo estado produzca el mismo
        # texto, que es lo que permite comparar/deduplicar escrituras por hash.
        texto = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        with self._lock:
            with self._conn:  # transacción: o entra entero, o no entra
                self._conn.execute(
                    """
                    INSERT INTO ledger (ambito, snapshot, actualizado_en)
                    VALUES (?, ?, ?)
                    ON CONFLICT(ambito) DO UPDATE SET
                        snapshot = excluded.snapshot,
                        actualizado_en = excluded.actualizado_en
                    """,
                    (ambito, texto, time.time()),
                )

    def descartar(self, ambito: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM ledger WHERE ambito = ?", (ambito,))

    def ambitos(self) -> tuple[str, ...]:
        with self._lock:
            filas = self._conn.execute(
                "SELECT ambito FROM ledger ORDER BY ambito"
            ).fetchall()
        return tuple(f[0] for f in filas)

    # -- Ciclo de vida -----------------------------------------------------

    def modo_journal(self) -> str:
        """Modo de journal en uso (`wal` esperado). Es diagnóstico del backend,
        no parte del port: sirve para verificar que la garantía de concurrencia
        que se declara en el docstring del módulo es la que está activa."""
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def actualizado_en(self, ambito: str) -> float | None:
        """Cuándo se guardó por última vez este ámbito. Es metadato de la base
        (no del ledger) y sirve para diagnóstico: `None` si no existe."""
        with self._lock:
            fila = self._conn.execute(
                "SELECT actualizado_en FROM ledger WHERE ambito = ?", (ambito,)
            ).fetchone()
        return None if fila is None else float(fila[0])

    def cerrar(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "AlmacenSQLite":
        return self

    def __exit__(self, *_excepcion: object) -> None:
        self.cerrar()

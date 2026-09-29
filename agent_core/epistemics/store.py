"""
`EpistemicStore`: ledger epistémico del agente, durable, e ingestión de
observaciones.

Este archivo contiene la decisión MÁS importante de todo el sistema, y es una
decisión que la librería portable no puede tomar sola:

    ¿qué cuenta como "observación del mundo" y qué cuenta como "texto que
    escribió el modelo"?

La librería confía en la etiqueta `Evidence.origin` que le pasa su llamador.
Si este archivo se equivoca, el edificio entero se cae: alcanza con marcar
`KERNEL_OBSERVED` sobre texto que salió del propio modelo para que una
alucinación quede con procedencia verificable — y encima el sistema la cite.
Por eso acá:

1. **El origen lo fija el camino de código que ejecutó la herramienta**, nunca
   el modelo. `ingest_tool_result()` se llama desde `_dispatch_tool`, que es el
   punto donde la herramienta realmente corrió.
2. **La lista de herramientas ingeribles es una ALLOWLIST, y falla cerrado.**
   Ver `OBSERVATION_TOOLS`. Una herramienta desconocida no ingiere nada.
3. **Las herramientas de eco están excluidas.** `recall`, `remember`,
   `open_question` y `record_hypothesis` devuelven texto que el propio agente
   escribió antes. Ingerirlas sería lavado de origen: el modelo guarda una
   conjetura, la recupera con `recall`, y esa salida entra al ledger como
   observación del kernel. Es el agujero más grave posible en esta capa, y por
   eso hay un test dedicado.

Sobre la allowlist y su costo: un allowlist se pudre — una herramienta nueva no
se ingiere hasta que alguien la agregue. Eso es deliberado: la pudrición es
hacia el lado SEGURO (menos respaldo → el gate afirma menos), no hacia el lado
del lavado. Lo correcto a futuro es declarar "esta herramienta observa el
mundo" en `ToolManifest`, pero `sdk/` es un directorio compartido con `kal`
(byte-a-byte, verificado por `scripts/check_kernel_drift.py`), así que ese
cambio es cross-repo y va aparte.

**Qué cambió en F4 (decisiones D4 y D5, 2026-09-29), porque altera el aislamiento
que F2 había elegido:**

- El ledger ya no es por sesión: es **por ámbito**, y el ámbito por defecto es la
  instalación (`AMBITO_POR_DEFECTO`). Es lo que hace que la misma pregunta en otra
  sesión caiga en el MISMO registro (el criterio de aceptación de F4, que F6 medía
  como 0.00 de dedup) y que las hipótesis sobrevivan al proceso.
- El aislamiento que F2 protegió sigue existiendo, pero **es por ámbito, no por
  sesión**: dos ámbitos no se ven entre sí (hay test). La lección de A-4 sigue en
  pie — el bug era que el aislamiento dependiera del objeto que uno tuviera a mano
  en vez de ser explícito. Acá es explícito: `ambito=`.
- La consecuencia hay que decirla: dentro de una instalación, una sesión ve las
  hipótesis y los déficits de las otras. Con una sola persona usando la
  instalación es la continuidad que se busca; el día que haya multiusuario, D5 se
  revisa cambiando el ámbito por el id del usuario.
- `ingested_count()` sigue siendo POR SESIÓN (un contador en memoria), porque R2
  pregunta "¿este turno consultó alguna fuente?", no "¿el ámbito tiene evidencia
  acumulada?". Contar el ámbito haría que una sesión silenciara el aviso de otra.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from audit.audit_log import AuditEvent, audit_log
from agent_core.epistemics.sanitize import (
    active_provider,
    sanitize_for_context,
    sanitize_text,
)
from kal_epistemics import (
    Diagnostico,
    diagnosticar,
    AMBITO_POR_DEFECTO,
    AlmacenDurable,
    AlmacenEnMemoria,
    AlmacenSQLite,
    Evidence,
    EvidenceOrigin,
    Hypothesis,
    HypothesisLedger,
    HypothesisStatus,
    LedgerDurable,
    LedgerLimitExceeded,
    LinkPolarity,
    RUTA_POR_DEFECTO,
)

logger = logging.getLogger(__name__)


def almacen_persistente(ruta: Path | str | None = None) -> AlmacenSQLite:
    """
    El almacén REAL del agente: SQLite en `data/epistemics/ledger.db` (decisión
    D4), con la ruta configurable.

    Existe como función y no como default de `EpistemicStore` por un motivo
    concreto: si el default tocara disco, cada test —y cada import del
    orquestador— escribiría en `data/`, y el estado de un test se filtraría al
    siguiente. El default de la clase sigue siendo en memoria; el runtime usa
    esto, explícitamente.
    """
    return AlmacenSQLite(ruta or RUTA_POR_DEFECTO)


OBSERVATION_TOOLS: frozenset[str] = frozenset(
    {
        # Lectura del mundo: estas traen el contenido en ESTA misma llamada.
        "browser",
        # F5: trae evidencia externa para un claim (fuentes candidatas que se le
        # pasan). Es una observación más, con una diferencia que importa: su origen
        # es TOOL_EXTERNAL, no KERNEL_OBSERVED (ver EXTERNAL_TEXT_TOOLS).
        "claim_verifier",
        "analyze_image",
        "extract_text_from_image",
        "speech_to_text",
        # Ejecución real (el stdout es observación).
        "run_code",
        # Generación/composición: el artefacto existe. Es creación propia, pero
        # el HECHO de que el artefacto existe es verificable — lo que no se
        # puede sostener con esto es ninguna afirmación sobre su contenido.
        "image_generation",
        "image_editing",
        "image_composition",
        "audio_generation",
        "video_composition",
    }
)
"""
Herramientas cuyo resultado es una observación del mundo y por lo tanto entra
al ledger. FAIL-CLOSED: lo que no está acá no se ingiere.

Excluidas a propósito, y el motivo importa más que la lista:

- `recall`, `remember` — devuelven contenido que el propio agente guardó antes.
- `open_question`, `record_hypothesis` — devuelven el eco de lo que el modelo
  acaba de escribir. Ingerirlas permitiría: registrar una hipótesis → su propia
  respuesta entra como "observación del kernel" → la hipótesis queda sostenida
  por sí misma. Es un lazo de auto-confirmación de dos pasos.
- `propose_skill`, `propose_project_files` — son propuestas: devuelven el estado
  de una propuesta, no una observación.
- `create_text_file` — es una acción, no una observación. Lo que se escribió se
  puede observar después, con `read_workspace_file`.
- **Las asíncronas** (`_VSCODE_ONLY_TOOL_NAMES`: `read_workspace_file`,
  `import_resource`, `android_build_and_screenshot`, `propose_project_files`)
  — NO son observaciones en el momento en que se llaman: devuelven un Artifact
  "pending" y el contenido real llega después, por otro camino (la extensión
  lee el archivo real y vuelve a llamar a /chat). Ingerir ese "pending" como
  observación del kernel sería fabricar un dato a partir de un marcador de
  estado, y encima un dato que podría matchear condiciones y reabrir
  hipótesis sin ninguna observación detrás. Hay un test que exige que esta
  allowlist sea DISJUNTA de ese conjunto, para que agregar una herramienta
  asíncrona nueva obligue a revisar esta decisión en vez de pasar inadvertida.
"""

EXTERNAL_TEXT_TOOLS: frozenset[str] = frozenset(
    {
        # Traen texto de internet: es exógeno, y NO es verdad — puede mentir y
        # puede intentar inyectar. Se distingue de `KERNEL_OBSERVED`, que es una
        # observación del sistema bajo mediación del kernel (el stdout de un
        # comando, el resultado de un análisis local): ahí el kernel responde por
        # el hecho de que la herramienta corrió y devolvió eso; acá responde solo
        # por la procedencia ("esto es lo que dijo esta fuente, con este hash").
        #
        # La distinción no cambia si la evidencia es exógena (las dos lo son) —
        # cambia con qué fuerza se puede tratar: el marcador que viaja al contexto
        # es distinto (ver `sanitize.py`), y `TOOL_EXTERNAL` es el que dice
        # "contenido externo no confiable" en vez de "observado por el kernel".
        "claim_verifier",
        "browser",
    }
)
"""Herramientas cuyo resultado es TEXTO EXTERNO. `claim_verifier` (F5) trae las
fuentes candidatas que se le pasan; `browser` navega páginas reales dentro de su
allowlist de dominios. Las dos son observaciones ingeribles, con origen distinto."""

# NOTA DE F7: acá vivía `TOOLS_DONDE_EL_FALLO_NO_ES_EVIDENCIA`, una lista de
# nombres de herramientas cuyo mensaje de error no debía ingerirse. Se fue: la
# decisión se mudó al camino que SÍ sabe qué pasó (`_dispatch_tool`, con el tier
# y el `status` del artefacto), y una lista de nombres se pudre en silencio — la
# herramienta nueva no está y nadie se entera.

_TOOLS_QUE_MUTAN_EL_LEDGER: frozenset[str] = frozenset(
    {"open_question", "record_hypothesis"}
)
"""Las dos tools epistémicas: mutan el ledger sin ingerir nada (su salida es el
eco de lo que escribió el modelo, y por eso NO están en `OBSERVATION_TOOLS`).
Están acá porque son un punto de guardado, no porque sean observaciones.""" 

_MAX_SESSIONS = 200
"""Tope de sesiones con ledger propio. Mismo criterio que agent_core/sessions.py:
desalojo LRU, nunca un error visible al usuario por alcanzarlo."""

_MAX_OBSERVATION_CHARS = 4000
"""Tope de la observación que se guarda como evidencia. Se recorta ANTES de
calcular el hash, para que el hash siga correspondiendo al contenido guardado
(que es lo que hace que sirva como ancla). El recorte se declara en `method`."""

_MAX_OPEN_ITEMS = 3
_MAX_BLOCK_CHARS = 1200


@dataclass(frozen=True)
class Ingestion:
    """
    Resultado de ingerir una observación: la evidencia (si entró) y la nota
    para el modelo (si la observación cambió el estado de alguna hipótesis).

    La nota existe porque el valor de la reapertura automática se pierde si el
    modelo no se entera: la hipótesis se reabre sola en el ledger, pero el
    modelo sigue razonando como si nada hubiera pasado. Va pegada a la
    observación, que es donde está mirando en ese momento.
    """

    evidence: Evidence | None = None
    note: str = ""


class EpistemicStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        max_sessions: int = _MAX_SESSIONS,
        almacen: AlmacenDurable | None = None,
        ambito: str = AMBITO_POR_DEFECTO,
    ) -> None:
        self._clock = clock
        self._max_sessions = max_sessions
        # Sin backend explícito, el estado vive en memoria: no sobrevive al
        # proceso, pero el comportamiento (dedup por ámbito, aislamiento por
        # ámbito) es exactamente el mismo. Es lo que usan los tests y lo que
        # corre un agente configurado sin persistencia.
        self._almacen: AlmacenDurable = almacen or AlmacenEnMemoria()
        self._ambito = ambito
        self._registro = LedgerDurable(almacen=self._almacen, ambito=ambito, clock=clock)
        # Mejora A (monitores introspectivos): un estado guardado puede pasar la
        # carga y no tener sentido (contadores de reapertura que no corresponden
        # a su historial, claves de dedup sin evidencia). Eso no se rechaza —un
        # ledger 99% sano sirve— pero TAMPOCO se gobierna en silencio: se reporta
        # una vez, al abrir el ámbito, y queda consultable con `diagnosticar()`.
        self._diagnostico_de_carga = self._registro.diagnostico_en_la_carga
        if not self._diagnostico_de_carga.sano:
            logger.warning(
                "El ledger epistémico del ámbito '%s' se cargó con inconsistencias: %s",
                ambito, self._diagnostico_de_carga.resumen(),
            )
        # Contadores POR SESIÓN, en memoria: el ledger es del ámbito, pero "qué
        # se observó en este turno" es de la sesión (ver el docstring del módulo).
        self._ingestas: dict[str, int] = {}
        self._sesiones: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------------
    # Ledger del ámbito
    # ------------------------------------------------------------------

    def ledger(self, session_id: str | None = None) -> HypothesisLedger:
        """
        Ledger del ÁMBITO (no de la sesión): `session_id` se acepta porque lo
        pasan todos los llamadores, se registra para poder ver qué sesiones
        tocaron el ledger, y no cambia qué registro se devuelve.

        Por qué esto es explícito y no un accidente (decisión D5): que las
        sesiones de una instalación compartan el registro es lo que hace que la
        misma pregunta no cree un registro nuevo y que el "no lo sé" sea el
        mismo entre sesiones. Lo que NO se puede es que el aislamiento dependa
        del objeto que uno tenga a mano — eso fue el bug A-4 (un buffer único
        compartido entre sesiones del proceso). Acá el aislamiento es por ámbito
        declarado, y dos ámbitos no se ven entre sí.
        """
        clave = session_id or "__sin_sesion__"
        self._sesiones[clave] = None
        self._sesiones.move_to_end(clave)
        while len(self._sesiones) > self._max_sessions:
            self._sesiones.popitem(last=False)
        return self._registro.ledger

    def diagnosticar(self, session_id: str | None = None) -> Diagnostico:
        """
        Corre los monitores sobre el estado ACTUAL (no el de la carga). Es una
        lectura sin efectos: sirve para auditar antes de confiar en el registro y
        para que la instrumentación detecte un estado absurdo sin esperar a que
        alguien lo note.
        """
        self.ledger(session_id)
        return diagnosticar(self._registro.ledger)

    @property
    def diagnostico_de_carga(self) -> Diagnostico:
        """El de la carga inicial del ámbito, para no repetir el cómputo."""
        return self._diagnostico_de_carga

    def session_ids(self) -> tuple[str, ...]:
        """Sesiones que tocaron el ledger, en orden de uso (LRU). Es
        observabilidad: el registro es del ámbito y no se desaloja."""
        return tuple(self._sesiones)

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------

    def guardar(self) -> bool:
        """
        Flush explícito del ámbito. Devuelve si escribió (False = no había nada
        nuevo). El host puede llamarlo al cerrar un turno o una sesión; la
        ingestión ya guarda sola después de cada mutación.
        """
        self._registro.compactar()
        return self._registro.guardar_si_cambio()

    def _persistir_si_mutó(self, tool_name: str) -> None:
        """
        Guarda SOLO si la herramienta pudo haber cambiado el ledger.

        Se llama desde `ingest_tool_result` con cada despacho. Los dos caminos que
        mutan el ledger son las observaciones ingeridas y las tools epistémicas
        (que registran preguntas e hipótesis); todo lo demás (un `recall`, un
        `browser` fuera de la allowlist) no toca nada y no tiene por qué pagar una
        escritura. El costo del chequeo es un hash del estado, y
        `guardar_si_cambio()` no escribe si no cambió.
        """
        if tool_name not in OBSERVATION_TOOLS and tool_name not in _TOOLS_QUE_MUTAN_EL_LEDGER:
            return
        try:
            self.guardar()
        except Exception:  # noqa: BLE001 — deliberado: no perder el turno por el disco
            logger.exception(
                "No se pudo guardar el ledger epistémico del ámbito '%s'; el turno "
                "sigue, pero el estado del proceso no quedó persistido.", self._ambito,
            )

    # ------------------------------------------------------------------
    # Ingestión
    # ------------------------------------------------------------------

    def ingest_tool_result(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        observation: str,
        dispatch: str = "in_process_tool",
    ) -> Ingestion:
        """
        Convierte el resultado REAL de una herramienta en evidencia, y evalúa
        reaperturas. Devuelve la evidencia, o `None` si no correspondía
        ingerirla.

        Quien llama es `_dispatch_tool` — código que ejecutó la herramienta. El
        modelo no participa de esta decisión en ningún punto.
        """
        if tool_name not in OBSERVATION_TOOLS:
            # El eco del modelo no se ingiere, pero la tool epistémica pudo haber
            # registrado una pregunta o una hipótesis: eso se persiste igual.
            self._persistir_si_mutó(tool_name)
            return Ingestion()
        if not observation or not observation.strip():
            return Ingestion()
        ledger = self.ledger(session_id)
        recortada = len(observation) > _MAX_OBSERVATION_CHARS
        contenido = observation[:_MAX_OBSERVATION_CHARS]
        metodo = f"{tool_name} vía {dispatch}"
        if recortada:
            metodo += f" (observación recortada a {_MAX_OBSERVATION_CHARS} caracteres)"

        evidencia = Evidence.from_content(
            contenido,
            # El origen lo fija ESTE camino de código (nunca el modelo): observación
            # del sistema salvo que la herramienta traiga texto de internet, que es
            # exógeno pero no es verdad.
            origin=(
                EvidenceOrigin.TOOL_EXTERNAL
                if tool_name in EXTERNAL_TEXT_TOOLS
                else EvidenceOrigin.KERNEL_OBSERVED
            ),
            source=tool_name,
            method=metodo,
            acquired_at=self._clock(),
            recheckable=True,
        )
        # ANCLA REAL: un evento del audit log encadenado por HMAC. `AuditEvent`
        # no tiene id — su identidad ES su `event_hash`, que depende del evento
        # anterior y de la clave HMAC. Eso es lo que hace que la procedencia no
        # se pueda fabricar: el modelo no puede escribir en el audit log.
        #
        # Fail-closed VISIBLE: si no se puede auditar (disco de solo lectura,
        # clave inaccesible), la evidencia NO se descarta —perder una
        # observación real sería peor— pero queda SIN ancla, y el ledger la
        # marca con `Flag.UNVERIFIABLE_PROVENANCE`. Degradación visible, no
        # silenciosa.
        try:
            evento = audit_log.record(
                AuditEvent(
                    event_type="epistemic_evidence_ingested",
                    summary=f"Evidencia epistémica ingerida desde '{tool_name}'",
                    context={
                        "tool_name": tool_name,
                        "session_id": session_id or "",
                        "content_hash": evidencia.content_hash,
                        "dispatch": dispatch,
                    },
                    outcome="success",
                )
            )
            evidencia = replace(
                evidencia, anchor=evento.event_hash, anchor_kind="audit_event_hash"
            )
        except Exception:  # noqa: BLE001 — deliberado: degradar visible, no romper el turno
            logger.exception(
                "No se pudo anclar la evidencia de '%s' al audit log: queda sin "
                "procedencia verificable", tool_name,
            )
            evidencia = replace(evidencia, anchor="", anchor_kind="")

        try:
            ledger.add_evidence(evidencia)
        except LedgerLimitExceeded as e:
            # Fail-closed y ruidoso en el log, pero sin romper el turno: no
            # ingerir significa MENOS respaldo, no más.
            logger.warning("Ingesta epistémica rechazada para '%s': %s", tool_name, e)
            self._persistir_si_mutó(tool_name)
            return Ingestion()
        self._contar_ingesta(session_id)

        # Quién cambió de estado se deduce comparando el contador de
        # reaperturas antes y después: así no hace falta tocar la API del
        # ledger (`consider()` devuelve las decisiones, no a quién afectaron).
        antes = {h.id: ledger.reopen_count(h.id) for h in ledger.hypotheses()}
        try:
            decisiones = ledger.consider(evidencia)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Fallo evaluando reaperturas para '%s'; la evidencia queda ingestada", tool_name
            )
            self._persistir_si_mutó(tool_name)
            return Ingestion(evidence=evidencia)

        for decision in decisiones:
            logger.info(
                "Reapertura automática por '%s': condición %s (%s)",
                tool_name, decision.condition_id, decision.reason,
            )

        cambiadas = [
            hid for hid, contador in antes.items() if ledger.reopen_count(hid) > contador
        ]
        self._persistir_si_mutó(tool_name)
        return Ingestion(evidence=evidencia, note=self._nota_de_reapertura(ledger, cambiadas))

    def _contar_ingesta(self, session_id: str | None) -> None:
        clave = session_id or "__sin_sesion__"
        self._ingestas[clave] = self._ingestas.get(clave, 0) + 1

    def _nota_de_reapertura(self, ledger: HypothesisLedger, hids: list[str]) -> str:
        if not hids:
            return ""
        detalles = []
        for hid in sorted(hids):
            h = ledger.hypothesis(hid)
            detalles.append(
                f'"{h.claim[:200]}" (id={h.id}, estado={h.status.value}, '
                f"nivel={ledger.level(h.id)})"
            )
        return (
            "[NOTA DEL SISTEMA — no es parte de la observación: lo anterior cambió el "
            "estado de una hipótesis tuya: " + "; ".join(detalles) + ". "
            "Ahora tiene respaldo observado, así que podés apoyarte en ella citándola; "
            "no la presentes como verificación propia más allá de esto.]"
        )

    def ingested_count(self, session_id: str | None = None) -> int:
        """
        Cuánta evidencia exógena entró en ESTA SESIÓN. El gate lo usa para saber
        si el turno tuvo ALGUNA base observada: es el único dato mecánicamente
        confiable sobre la procedencia de una respuesta.

        Es un contador por sesión y no el total del ámbito a propósito: la
        pregunta del gate es sobre el turno, y con un ledger compartido contar el
        ámbito haría que una sesión silenciara el aviso de "sin verificación" de
        otra. Se pierde al reiniciar el proceso, y está bien: un turno nuevo
        después de un reinicio no consultó ninguna fuente todavía.
        """
        return self._ingestas.get(session_id or "__sin_sesion__", 0)

    def unsatisfied_deficits(self, session_id: str | None = None) -> tuple[str, ...]:
        """
        Descripciones de lo que el agente declaró que le falta y sigue
        faltando. Un déficit abierto es una obligación: si el agente lo declaró
        y después respondió como si lo supiera, eso es exactamente lo que el
        gate tiene que frenar.

        Los déficits son del ÁMBITO, no de la sesión (decisión D5): un "me falta
        el log del kernel" declarado en una sesión sigue siendo una deuda en la
        siguiente, que es justamente lo que hace que el "no lo sé" sea el mismo.
        Desde el arreglo de D9 esa deuda se puede saldar cuando llega la
        evidencia, así que no queda enclavada.
        """
        ledger = self.ledger(session_id)
        pendientes: list[str] = []
        for q in ledger.all_questions():
            pendientes.extend(ledger.missing_descriptions(q.id))
        return tuple(pendientes)

    # ------------------------------------------------------------------
    # Contexto
    # ------------------------------------------------------------------

    def open_hypotheses(
        self, session_id: str | None = None, *, limit: int = _MAX_OPEN_ITEMS
    ) -> tuple[Hypothesis, ...]:
        """Hipótesis sin cerrar, las más recientes primero."""
        ledger = self.ledger(session_id)
        abiertas = [
            h
            for h in ledger.hypotheses()
            if h.status
            in {
                HypothesisStatus.OPEN,
                HypothesisStatus.WEAKENED,
                HypothesisStatus.SUSPENDED,
                HypothesisStatus.REACTIVATED,
            }
        ]
        abiertas.sort(key=lambda h: (h.updated_at, h.created_at, h.id), reverse=True)
        return tuple(abiertas[:limit])

    def _cita_sanitizada(self, ledger: HypothesisLedger, hipotesis: Hypothesis, *, provider: str) -> str:
        """Cita de la evidencia exógena más reciente que sostiene esta
        hipótesis, sanitizada. Sin esto, el bloque reinyecta la conclusión pero
        no de dónde salió — que es la mitad del punto de todo esto."""
        for link in reversed(hipotesis.evidence_links):
            if link.polarity is not LinkPolarity.SUPPORTS:
                continue
            try:
                ev = ledger.evidence(link.evidence_id)
            except Exception:  # noqa: BLE001
                continue
            if not ev.is_exogenous:
                continue
            saneada = sanitize_for_context(ev, provider=provider)
            if saneada is not None:
                return saneada.text
        return ""

    def context_block(
        self, session_id: str | None = None, *, provider: str | None = None
    ) -> str:
        """
        Bloque de contexto con lo que el agente dejó sin resolver.

        Dos cosas que este bloque NO hace, y son deliberadas:

        1. No presenta las hipótesis como datos: el texto lo escribió el modelo,
           así que reinyectarlo con formato de hechos sería un canal para que
           confunda su propia conjetura con conocimiento — el mismo lavado de
           origen que la allowlist evita del otro lado.
        2. No reinyecta nada sin pasar por el sanitizador: el claim, la pregunta
           y el déficit también son texto que puede contener una credencial
           pegada por el usuario (el sumidero no es solo `Evidence.content`).
        """
        ledger = self.ledger(session_id)
        abiertas = self.open_hypotheses(session_id)
        if not abiertas:
            return ""

        proveedor = provider if provider is not None else active_provider()
        lineas = [
            "EXPEDIENTE ABIERTO — hipótesis TUYAS sin resolver. NO son conocimiento:",
            "las escribiste vos. El nivel indica a cuántos saltos está cada una de una",
            "observación real (nivel=0 = sostenida por una observación; nivel=None = sin",
            "respaldo). No afirmes ninguna como un hecho.",
            "",
        ]
        omitidas = 0
        for h in abiertas:
            claim = sanitize_text(h.claim, provider=proveedor, max_chars=300)
            if claim is None:
                omitidas += 1
                continue
            nivel = ledger.level(h.id)
            lineas.append(f"- [estado={h.status.value} | nivel={nivel}] {claim}")

            cita = self._cita_sanitizada(ledger, h, provider=proveedor)
            if cita:
                lineas.append(f"  evidencia: {cita}")

            if h.question_id:
                try:
                    pregunta = ledger.question(h.question_id)
                except Exception:  # noqa: BLE001
                    pregunta = None
                if pregunta is not None:
                    texto = sanitize_text(pregunta.text, provider=proveedor, max_chars=200)
                    if texto:
                        detalle = f"  pregunta: {texto}"
                        faltan = [
                            f
                            for f in (
                                sanitize_text(d, provider=proveedor, max_chars=120)
                                for d in ledger.missing_descriptions(pregunta.id)
                            )
                            if f
                        ]
                        if faltan:
                            detalle += f" | falta: {'; '.join(faltan)}"
                        lineas.append(detalle)

        if omitidas:
            lineas.append(
                f"({omitidas} hipótesis omitida(s) por política de exposición de datos.)"
            )
        return "\n".join(lineas)[:_MAX_BLOCK_CHARS]

"""
MemoryManager: fachada única sobre los tres niveles de memoria.

El resto del sistema no debería instanciar ShortTermMemory/MidTermMemory/
LongTermMemory directamente — usa esta clase, que además implementa el
flujo de consolidación y promoción entre niveles.
"""
from __future__ import annotations

from agent_core.memory.base import MemoryConfidence, MemoryItem
from agent_core.memory.events import MemoryEvent, MemoryObserver
from agent_core.memory.long_term import LongTermMemory
from agent_core.memory.mid_term import MidTermMemory
from agent_core.memory.security_policy import MemoryClassification, classify, redact
from agent_core.memory.short_term import ShortTermMemory
from utils.logger import get_logger

logger = get_logger(__name__)

# Confianzas que un humano ya fijó explícitamente — la promoción
# automática (patrón auto-inferido) nunca las degrada de vuelta a
# APRENDIDA, solo las sube nunca las baja.
_HUMAN_CONFIRMED = frozenset({MemoryConfidence.VERIFICADA, MemoryConfidence.PERMANENTE})

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), A-4: antes había UN solo ShortTermMemory para todo el
# proceso — cualquier sesión/usuario podía recall() lo que otra sesión
# distinta acababa de remember() (fuga de datos entre conversaciones no
# relacionadas). `_DEFAULT_SESSION` preserva el comportamiento anterior
# para cualquier llamador que no pase session_id (tests, o algún
# caller fuera del flujo de /chat que no tenga noción de sesión).
_DEFAULT_SESSION = "_no_session"


class MemoryManager:
    def __init__(
        self,
        short_term: ShortTermMemory | None = None,
        mid_term: MidTermMemory | None = None,
        long_term: LongTermMemory | None = None,
        observers: list[MemoryObserver] | None = None,
    ):
        """
        Por defecto construye los tres backends reales (rutas de
        config.yaml). Permite inyectar instancias propias — usado en
        tests para evitar escribir en data/mid_term o
        data/long_term reales del proyecto durante la suite.

        `short_term` (si se inyecta) se usa como la instancia para
        `_DEFAULT_SESSION` únicamente — cada session_id real sigue
        recibiendo su propio ShortTermMemory nuevo (ver
        _short_term_for()), mismo criterio de aislamiento que en
        producción.

        `observers`: infraestructura preparada para un futuro Knowledge
        Miner (ver agent_core/memory/events.py y
        agent_core/knowledge/) — vacío por defecto, cero cambio de
        comportamiento si nadie se registra.
        """
        self._short_term_by_session: dict[str, ShortTermMemory] = {
            _DEFAULT_SESSION: short_term or ShortTermMemory(),
        }
        self.mid_term = mid_term or MidTermMemory()
        self.long_term = long_term or LongTermMemory()
        self._observers: list[MemoryObserver] = list(observers or [])

    @property
    def short_term(self) -> ShortTermMemory:
        """Compatibilidad hacia atrás: código/tests que leen `.short_term`
        directamente (sin noción de sesión) siguen viendo la instancia
        de _DEFAULT_SESSION, igual que antes de este fix."""
        return self._short_term_by_session[_DEFAULT_SESSION]

    def _short_term_for(self, session_id: str | None) -> ShortTermMemory:
        key = session_id or _DEFAULT_SESSION
        if key not in self._short_term_by_session:
            self._short_term_by_session[key] = ShortTermMemory()
        return self._short_term_by_session[key]

    def remember(
        self,
        content: str,
        metadata: dict | None = None,
        confidence: MemoryConfidence = MemoryConfidence.TEMPORAL,
        sharing: str = "local_only",
        session_id: str | None = None,
    ) -> MemoryItem:
        """
        Punto de entrada por defecto: todo lo nuevo entra por corto plazo.

        `sharing`: ver agent_core/memory/security_policy.py::MemorySharing
        — default fail-closed ("local_only"). Sin ningún mecanismo hoy
        para marcar algo "cloud_ok" explícitamente, así que en la
        práctica todo lo guardado queda local_only por ahora — recall()
        (ver tool_integration/adapters/core_tools.py::MemoryRecallTool)
        filtra esto si el proveedor de LLM activo es en la nube.

        `session_id`: aísla el buffer de corto plazo por conversación
        (ver A-4 arriba) — None cae en `_DEFAULT_SESSION`, mismo
        comportamiento que antes de este fix.
        """
        item = MemoryItem(content=content, metadata={**(metadata or {}), "sharing": sharing}, confidence=confidence)
        self._short_term_for(session_id).store(item)
        return item

    def recall(self, query: str, top_k: int = 5, session_id: str | None = None) -> dict[str, list[MemoryItem]]:
        """
        Busca en los tres niveles. El llamador decide cómo priorizar
        (p.ej., corto plazo primero por ser más específico a la tarea actual).
        `session_id`: ver remember() — corto plazo solo busca DENTRO de
        la sesión actual; mediano/largo plazo siguen siendo
        compartidos entre sesiones a propósito (es conocimiento
        consolidado del agente, no contexto de una tarea puntual).

        M-7 (auditoría externa Likay-OS, 2026-09-26): top_k llega hasta
        un `LIMIT ?` de SQLite (mid_term.py) — un valor <= 0 hace que
        SQLite lo interprete como "sin límite" y devuelva la tabla
        ENTERA. GET /memory/search ya lo acota en la capa HTTP (ver
        agent_core/routers/memory.py), pero recall() también lo llama
        el propio agente como Tool (MemoryRecallTool, top_k elegido por
        el LLM) — clamp acá, en el único punto que ambos caminos
        comparten, para no depender de que cada llamador lo recuerde.
        """
        top_k = max(1, min(top_k, 100))
        return {
            "short_term": self._short_term_for(session_id).retrieve(query, top_k),
            "mid_term": self.mid_term.retrieve(query, top_k),
            "long_term": self.long_term.retrieve(query, top_k),
        }

    def register_observer(self, observer: MemoryObserver) -> None:
        """
        Infraestructura preparada para un futuro Knowledge Miner (ver
        agent_core/memory/events.py, agent_core/knowledge/) — sin
        ningún observer registrado (default), el comportamiento de
        consolidate_short_to_mid()/promote_mid_to_long() es idéntico a
        antes de que esto existiera.
        """
        self._observers.append(observer)

    def _notify(self, event: MemoryEvent) -> None:
        # Fail-safe a propósito (mismo criterio que
        # ConversationEngine.classify()): un observer experimental
        # (p.ej. un Knowledge Miner todavía sin madurar) nunca debe
        # poder romper el ciclo real de consolidación/promoción de
        # memoria.
        for observer in self._observers:
            try:
                observer.on_memory_event(event)
            except Exception as e:  # noqa: BLE001 — un observer nunca debe romper el ciclo real (ver arriba)
                logger.warning(f"Observer de memoria falló, se ignora: {e}")

    def consolidate_short_to_mid(self, summarizer=None) -> int:
        """
        Traslada el contenido de corto plazo a mediano plazo, resumiendo
        si se provee un `summarizer` (callable str -> str, típicamente
        una llamada a un modelo). Sin summarizer, se guarda el contenido tal cual.

        Recorre TODAS las sesiones (ver A-4 arriba: cada una tiene su
        propio ShortTermMemory desde este fix) — antes de eso había un
        único buffer global, así que "consolidar" ya cubría todo con
        una sola llamada; ahora hay que barrer cada uno. Las sesiones
        que quedan vacías tras consolidar se eliminan del diccionario
        — evita crecer sin límite con una entrada por sesión vieja para
        siempre.
        """
        total = 0
        emptied_sessions: list[str] = []
        for key, buffer in self._short_term_by_session.items():
            items = buffer.consolidate()
            for item in items:
                if summarizer is not None:
                    item.content = summarizer(item.content)
                self.mid_term.store(item)
                self._notify(MemoryEvent(kind="consolidated", item=item))
            total += len(items)
            if key != _DEFAULT_SESSION:
                emptied_sessions.append(key)
        for key in emptied_sessions:
            del self._short_term_by_session[key]
        logger.info(f"Consolidados {total} items de corto a mediano plazo")
        return total

    def promote_mid_to_long(self) -> int:
        """
        Evalúa candidatos de mediano plazo según la política de promoción
        (repeticiones + relevancia, ver config.yaml) y los traslada a
        largo plazo. No borra automáticamente de mediano plazo: eso lo
        maneja purge_expired() por TTL de forma independiente.

        Un patrón que cruza el umbral de repeticiones/relevancia es una
        inferencia del propio agente, no algo que un humano confirmó —
        se etiqueta APRENDIDA, salvo que ya sea VERIFICADA/PERMANENTE
        (un humano ya se pronunció sobre ese item, no se le baja el nivel).
        """
        candidates = self.mid_term.candidates_for_promotion()
        for item in candidates:
            if item.confidence not in _HUMAN_CONFIRMED:
                item.confidence = MemoryConfidence.APRENDIDA
            # Memory Security Policy Engine (Fase 1, ver
            # agent_core/memory/security_policy.py): lo PERMANENTE se
            # clasifica y, si contiene una credencial de formato
            # conocido, se redacta ANTES de persistir para siempre —
            # remember()/consolidate_short_to_mid() nunca se tocan (el
            # agente necesita poder usar una credencial pegada en la
            # tarea inmediata, solo lo que se vuelve permanente se filtra).
            classification = classify(item.content)
            item.metadata["classification"] = classification.value
            if classification == MemoryClassification.SECRET:
                item.content = redact(item.content)
            self.long_term.store(item)
            self._notify(MemoryEvent(kind="promoted", item=item))
        logger.info(f"Promovidos {len(candidates)} items de mediano a largo plazo")
        return len(candidates)

    def verify(self, item_id: str, tier: str, verified_by: str) -> MemoryItem:
        """
        Un humano confirma explícitamente que un recuerdo es correcto:
        sube su confianza a VERIFICADA, sin importar cómo haya entrado
        (temporal, aprendida, externa). Trabaja sobre mid_term o
        long_term (donde existe get_by_id por clave exacta) — short_term
        no aplica: vive solo en RAM de la tarea activa.
        """
        backend = self._backend_for_tier(tier)
        item = backend.get_by_id(item_id)
        if item is None:
            raise ValueError(f"No existe el item '{item_id}' en la memoria de nivel '{tier}'")
        item.confidence = MemoryConfidence.VERIFICADA
        item.metadata = {**item.metadata, "verified_by": verified_by}
        backend.store(item)
        logger.info(f"Item {item_id} ({tier}) marcado VERIFICADA por {verified_by}")
        return item

    def pin(self, item_id: str, tier: str) -> MemoryItem:
        """
        Fija un recuerdo como PERMANENTE: nunca se purga por TTL (ver
        MidTermMemory.purge_expired) y se trata como hecho base, no
        como una inferencia sujeta a revisión.
        """
        backend = self._backend_for_tier(tier)
        item = backend.get_by_id(item_id)
        if item is None:
            raise ValueError(f"No existe el item '{item_id}' en la memoria de nivel '{tier}'")
        item.confidence = MemoryConfidence.PERMANENTE
        backend.store(item)
        logger.info(f"Item {item_id} ({tier}) fijado como PERMANENTE")
        return item

    def forget(self, item_id: str, tier: str) -> None:
        """
        Derecho al olvido — conecta el `forget()` que cada backend YA
        implementaba (parte del contrato MemoryBackend) pero que hasta
        ahora ningún endpoint exponía. Igual que verify()/pin(), aplica
        a mid_term/long_term (corto plazo vive solo en RAM de la tarea
        activa, sin identidad estable fuera de ese turno).
        """
        self._backend_for_tier(tier).forget(item_id)

    def forget_matching(
        self,
        keyword: str | None = None,
        tier: str | None = None,
        classification: str | None = None,
        before: float | None = None,
        after: float | None = None,
    ) -> int:
        """
        Borrado masivo del derecho al olvido. Exige AL MENOS un filtro —
        seguro por sí solo (mismo criterio que
        agent_core/orchestrator.py::_artifact_url(): la función nunca
        confía en que el llamador ya validó, se protege ella misma),
        nunca un "borrar todo" accidental de un único llamado sin
        parámetros. `tier` restringe a un nivel puntual; sin él, se
        aplica a los tres (a diferencia de forget()/verify()/pin(), acá
        SÍ cubre corto plazo — no tiene la limitación de identidad
        estable de esos otros, list_all() alcanza).
        """
        if keyword is None and classification is None and before is None and after is None:
            raise ValueError(
                "forget_matching() requiere al menos un filtro (keyword/classification/before/after)"
            )

        # A-4: 'short_term' ahora es UNA lista de backends (uno por
        # sesión activa), no uno solo — sin esto, un borrado masivo
        # solo limpiaba la sesión _DEFAULT_SESSION, dejando intacto
        # cualquier otro short_term de sesión real.
        short_term_backends = list(self._short_term_by_session.values())
        backends: dict[str, list] = {
            "short_term": short_term_backends, "mid_term": [self.mid_term], "long_term": [self.long_term],
        }
        if tier is not None:
            if tier not in backends:
                raise ValueError(f"Nivel de memoria inválido: '{tier}' (usar 'short_term', 'mid_term' o 'long_term')")
            backends = {tier: backends[tier]}

        deleted = 0
        for backend in (b for backend_list in backends.values() for b in backend_list):
            for item in backend.list_all():
                if keyword is not None and keyword.lower() not in item.content.lower():
                    continue
                if classification is not None and item.metadata.get("classification") != classification:
                    continue
                if before is not None and item.created_at >= before:
                    continue
                if after is not None and item.created_at <= after:
                    continue
                backend.forget(item.id)
                deleted += 1
        logger.info(f"forget_matching(): {deleted} item(s) borrados")
        return deleted

    def _backend_for_tier(self, tier: str):
        if tier == "mid_term":
            return self.mid_term
        if tier == "long_term":
            return self.long_term
        raise ValueError(f"Nivel de memoria inválido: '{tier}' (usar 'mid_term' o 'long_term')")

"""
Tests del adaptador `agent_core/epistemics/`.

Acá se prueba la decisión que la librería portable NO puede tomar sola:
qué cuenta como observación del mundo y qué cuenta como texto del modelo. Si
esto se equivoca, una alucinación queda con procedencia verificable y el sistema
la cita — así que los tests son sobre eso, no sobre "anda".
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent_core.epistemics import OBSERVATION_TOOLS, EpistemicStore
from kal_epistemics import (
    EvidenceOrigin,
    Flag,
    HypothesisStatus,
    ReopenAction,
    ReopenCondition,
)

RAIZ = Path(__file__).resolve().parents[1]


def _store(**kwargs) -> EpistemicStore:
    return EpistemicStore(clock=lambda: 1000.0, **kwargs)



# --- aislamiento por ÁMBITO (misma clase de bug que A-4) -------------------
#
# Cambio de F4, decisión D5: el ledger pasó de ser por sesión a ser por ámbito
# (por defecto, la instalación). Las sesiones de una instalación COMPARTEN el
# registro —es lo que hace que la misma pregunta no cree un registro nuevo y que
# el "no lo sé" sea el mismo entre sesiones—, y el aislamiento pasa a ser entre
# ámbitos. La lección de A-4 sigue en pie: el bug era que el aislamiento
# dependiera del objeto que uno tuviera a mano, no que existiera.


def test_las_sesiones_del_mismo_ambito_comparten_el_registro():
    store = _store()
    a = store.ledger("sesion_a")
    b = store.ledger("sesion_b")

    a.record_hypothesis("una conjetura de A")

    assert a is b
    assert len(b.hypotheses()) == 1
    assert b.hypotheses()[0].claim == "una conjetura de A"
    # La sesión se registra igual (observabilidad), en orden LRU.
    assert store.session_ids() == ("sesion_a", "sesion_b")


def test_dos_ambitos_no_se_ven_entre_si():
    """El aislamiento no se perdió: cambió de eje. Dos ámbitos (dos usuarios, dos
    instalaciones) no comparten nada."""
    from kal_epistemics import AlmacenEnMemoria

    # Mismo almacén, dos ámbitos: es el caso de dos usuarios o dos instalaciones.
    almacen = AlmacenEnMemoria()
    ana = EpistemicStore(clock=lambda: 1000.0, almacen=almacen, ambito="ana")
    beto = EpistemicStore(clock=lambda: 1000.0, almacen=almacen, ambito="beto")

    ana.ledger("s1").record_hypothesis("una conjetura de Ana")

    assert beto.ledger("s1").hypotheses() == ()
    assert ana.ledger("s1").hypotheses() != ()


def test_el_tope_de_sesiones_acota_la_observabilidad_no_el_registro():
    """
    El tope ya no desaloja ledgers (el registro es del ámbito y es durable): acota
    la lista de sesiones vistas. Antes desalojar significaba perder hipótesis sin
    resolver; ahora no se pierde nada.
    """
    store = _store(max_sessions=2)

    store.ledger("a").record_hypothesis("algo de a")
    store.ledger("b")
    store.ledger("c")

    assert store.session_ids() == ("b", "c")
    assert len(store.ledger("c").hypotheses()) == 1  # el ledger sigue siendo el mismo


# --- ingestión: quién decide el origen ------------------------------------


def test_la_ingestion_marca_la_observacion_como_observada_por_el_kernel():
    store = _store()

    ingesta = store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="exit 1: connection refused"
    )
    ev = ingesta.evidence

    assert ev is not None
    assert ev.origin is EvidenceOrigin.KERNEL_OBSERVED
    assert ev.is_exogenous
    assert ev.source == "run_code"
    # Ancla FUERTE: el hash del evento del audit log encadenado por HMAC, no
    # el hash del contenido (que sigue existiendo para dedup).
    assert ev.anchor_kind == "audit_event_hash"
    assert ev.anchor
    assert ev.anchor != ev.content_hash


def test_una_herramienta_fuera_de_la_allowlist_no_ingiere_nada():
    """Fail-closed: lo que no está declarado como observación, no entra."""
    store = _store()

    resultado = store.ingest_tool_result(
        session_id="s", tool_name="herramienta_del_futuro", observation="algo que parece un dato"
    )

    assert resultado.evidence is None
    assert store.ledger("s").exogenous_evidence_ids() == frozenset()


def test_las_herramientas_de_eco_estan_excluidas():
    """
    Si esto falla hay LAVADO DE ORIGEN, que es el agujero más grave de esta
    capa: el modelo guarda una conjetura, la recupera con `recall`, y su propio
    texto entra al ledger como observación del kernel. Con `open_question` y
    `record_hypothesis` el lazo es de dos pasos: registrar una hipótesis y su
    propia respuesta vuelve como evidencia.
    """
    for nombre in ("recall", "remember", "open_question", "record_hypothesis"):
        assert nombre not in OBSERVATION_TOOLS, f"'{nombre}' no puede ser ingerible"


def test_la_allowlist_no_nombra_herramientas_inexistentes():
    """
    Un typo en la allowlist falla en SILENCIO: esa herramienta nunca ingeriría
    nada, y el agente simplemente afirmaría menos sin que nadie se entere.
    """
    nombres: set[str] = set()
    for archivo in (RAIZ / "tool_integration" / "adapters").glob("*.py"):
        nombres |= set(re.findall(r'name="([a-z_]+)"', archivo.read_text(encoding="utf-8")))

    faltantes = OBSERVATION_TOOLS - nombres
    assert faltantes == set(), (
        f"La allowlist nombra herramientas que no existen: {sorted(faltantes)}. "
        "Nunca se ingeriría nada de ellas, en silencio."
    )


def test_una_observacion_vacia_no_se_ingiere():
    assert (
        _store().ingest_tool_result(session_id="s", tool_name="run_code", observation="  ").evidence
        is None
    )


def test_la_observacion_larga_se_recorta_y_el_hash_corresponde_a_lo_guardado():
    store = _store()

    ev = store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="x" * 10_000
    ).evidence

    assert ev is not None
    assert len(ev.content) == 4000
    # El hash tiene que corresponder al contenido GUARDADO: es lo que hace que
    # sirva como ancla.
    assert ev.content_hash == hashlib.sha256(ev.content.encode("utf-8")).hexdigest()
    assert "recortada" in ev.method


# --- el ciclo completo, sin intervención del modelo ------------------------


def test_la_ingestion_reabre_una_hipotesis_guardada_sin_intervencion_del_modelo():
    store = _store()
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    hipotesis = ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])

    store.ingest_tool_result(
        session_id="s",
        tool_name="run_code",
        observation="kernel: Out of memory: Killed process 4821",
    )

    final = ledger.hypothesis(hipotesis.id)
    assert final.status is HypothesisStatus.REACTIVATED
    assert ledger.level(hipotesis.id) == 0
    assert Flag.GROUNDED_IN_EXOGENOUS in ledger.flags(hipotesis.id)
    assert ledger.reopen_count(hipotesis.id) == 1


def test_una_condicion_de_refutacion_refuta_al_ingerir():
    store = _store()
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(
        content_keywords=("rollback",), action=ReopenAction.REFUTE
    )
    hipotesis = ledger.record_hypothesis("El deploy salió bien", reopen_conditions=[condicion])

    store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="deploy rollback ejecutado"
    )

    assert ledger.hypothesis(hipotesis.id).status is HypothesisStatus.REFUTED


# --- bloque de contexto ---------------------------------------------------


def test_el_bloque_de_expediente_avisa_que_no_es_conocimiento_y_esta_acotado():
    store = _store()
    store.ledger("s").record_hypothesis("A podría causar B")

    bloque = store.context_block("s")

    assert "NO son conocimiento" in bloque
    assert "A podría causar B" in bloque
    assert "nivel=None" in bloque
    assert len(bloque) <= 1200


def test_sin_hipotesis_abiertas_el_bloque_es_vacio():
    assert _store().context_block("s") == ""


def test_una_hipotesis_refutada_no_aparece_en_el_expediente_abierto():
    store = _store()
    ledger = store.ledger("s")
    h = ledger.record_hypothesis("Algo que ya se cayó")
    ledger.invalidate(h.id, reason="la premisa no estaba respaldada")

    assert h.id not in {x.id for x in store.open_hypotheses("s")}


def test_la_allowlist_es_disjunta_de_las_herramientas_asincronas():
    """
    Las herramientas de `_VSCODE_ONLY_TOOL_NAMES` devuelven un "pending": el
    contenido real llega después, por otro camino. Ingerirlas sería fabricar
    una observación a partir de un marcador de estado. Este test es el
    mecanismo que obliga a revisar la allowlist cuando aparece una asíncrona
    nueva.
    """
    from agent_core.client_provider import _VSCODE_ONLY_TOOL_NAMES

    interseccion = OBSERVATION_TOOLS & _VSCODE_ONLY_TOOL_NAMES

    assert interseccion == frozenset(), (
        f"{sorted(interseccion)} son asíncronas (devuelven 'pending') y no pueden "
        "ingerirse como observación del mundo."
    )


def test_el_cableado_real_ingiere_una_observacion_y_no_un_eco():
    """
    El test que cierra el lazo, POR EL CAMINO REAL (`_dispatch_tool`), no por
    las piezas sueltas:

    - una herramienta de eco devuelve EXACTAMENTE el texto que reabriría la
      hipótesis → no ingiere nada y la hipótesis sigue abierta (lavado de
      origen cerrado);
    - registrar una hipótesis por el camino real → su propia respuesta NO vuelve
      como evidencia (el lazo de auto-confirmación de dos pasos);
    - una herramienta declarada como observación → ingiere, y la hipótesis se
      reabre sola.
    """
    from agent_core.llm.agent_loop import AgentLoop, AgentTool, _agent_tool_from_tool
    from tool_integration.adapters.epistemic_tools import RecordHypothesisTool

    store = _store()
    loop = AgentLoop(
        llm_client=object(), task_executor=object(), memory=object(), epistemics=store
    )
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    hipotesis = ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])

    def _tool(nombre: str, handler) -> AgentTool:
        return AgentTool(name=nombre, description="", parameters_schema={}, handler=handler)

    eco = _tool("recall", lambda **kw: "de mi memoria: Out of memory, seguro")
    loop._dispatch_tool("recall", {}, {"recall": eco}, session_id="s")
    assert ledger.exogenous_evidence_ids() == frozenset()
    assert ledger.hypothesis(hipotesis.id).status is HypothesisStatus.OPEN

    registrar = _agent_tool_from_tool(
        "record_hypothesis", RecordHypothesisTool(store, session_id="s")
    )
    loop._dispatch_tool(
        "record_hypothesis", {"claim": "otra cosa"}, {"record_hypothesis": registrar}, session_id="s"
    )
    assert len(ledger.hypotheses()) == 2  # se registró…
    assert ledger.exogenous_evidence_ids() == frozenset()  # …pero no se ingirió

    observacion = _tool(
        "run_code", lambda **kw: "kernel: Out of memory: Killed process 4821"
    )
    loop._dispatch_tool("run_code", {}, {"run_code": observacion}, session_id="s")

    assert ledger.exogenous_evidence_ids() != frozenset()
    assert ledger.hypothesis(hipotesis.id).status is HypothesisStatus.REACTIVATED


# --- el lazo se cierra: el modelo se entera de la reapertura --------------


def test_la_ingestion_avisa_cuando_reabre_una_hipotesis():
    """
    La reapertura automática no sirve de nada si el modelo no se entera: la
    hipótesis se reabre sola en el ledger y el modelo sigue razonando como si
    nada hubiera pasado.
    """
    store = _store()
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])

    ingesta = store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="kernel: Out of memory: Killed 4821"
    )

    assert ingesta.evidence is not None
    assert "NOTA DEL SISTEMA" in ingesta.note
    assert "Se cayó por OOM" in ingesta.note
    assert "nivel=0" in ingesta.note


def test_sin_reapertura_no_hay_nota():
    store = _store()

    ingesta = store.ingest_tool_result(session_id="s", tool_name="run_code", observation="exit 0")

    assert ingesta.evidence is not None
    assert ingesta.note == ""


def test_el_aviso_de_reapertura_llega_pegado_a_la_observacion():
    """Por el camino real: el modelo ve la observación Y el aviso, juntos."""
    from agent_core.llm.agent_loop import AgentLoop, AgentTool

    store = _store()
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])
    loop = AgentLoop(
        llm_client=object(), task_executor=object(), memory=object(), epistemics=store
    )
    herramienta = AgentTool(
        name="run_code",
        description="",
        parameters_schema={},
        handler=lambda **kw: "kernel: Out of memory: Killed process 4821",
    )

    observacion = loop._dispatch_tool("run_code", {}, {"run_code": herramienta}, session_id="s")

    assert observacion.startswith("kernel: Out of memory")
    assert "NOTA DEL SISTEMA" in observacion


# --- el expediente abierto en el prompt ----------------------------------


def _loop(store) -> object:
    from agent_core.llm.agent_loop import AgentLoop

    return AgentLoop(llm_client=object(), task_executor=object(), memory=object(), epistemics=store)


def test_el_expediente_se_funde_en_el_unico_mensaje_system():
    """
    BUG REAL YA DOCUMENTADO en agent_loop.py: con DOS mensajes `system`,
    qwen3-coder:30b ignoraba el contexto de sesión por completo (confirmado con
    una prueba directa contra Ollama). El bloque epistémico se funde en el
    único system en vez de ir aparte; esto lo fija.
    """
    store = _store()
    store.ledger("s").record_hypothesis("A podría causar B")

    contenido = _loop(store)._system_content({"content": "contexto de sesión"}, "s")

    assert isinstance(contenido, str)
    assert "Eres kal-in" in contenido  # SYSTEM_PROMPT
    assert "contexto de sesión" in contenido
    assert "EXPEDIENTE ABIERTO" in contenido
    assert "A podría causar B" in contenido


def test_sin_hipotesis_abiertas_el_system_no_lleva_expediente():
    store = _store()

    contenido = _loop(store)._system_content({"content": "ctx"}, "s")

    assert "EXPEDIENTE ABIERTO" not in contenido


def test_no_se_agrega_un_segundo_mensaje_system():
    """Test estructural: el contexto tiene que ir FUNDIDO, no en un system
    nuevo (ver el bug real documentado arriba)."""
    fuente = (RAIZ / "agent_core" / "llm" / "agent_loop.py").read_text(encoding="utf-8")

    assert fuente.count('"role": "system"') == 2, (
        "apareció un mensaje system nuevo: el contexto epistémico tiene que ir "
        "fundido en el único system, o el modelo lo ignora"
    )
    assert "_system_content(" in fuente


# --- el ancla real: un eslabón de la cadena de auditoría ------------------


def test_el_ancla_es_un_eslabon_real_de_la_cadena_de_auditoria():
    """
    Lo que hace que la procedencia no se pueda fabricar: el ancla es el hash de
    un evento del audit log encadenado por HMAC, y el modelo no puede escribir
    ahí. Esto verifica que el ancla apunte a una entrada REAL y que el
    encadenamiento sea consistente con la anterior.
    """
    from audit.audit_log import audit_log

    store = _store()
    ingesta = store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="exit 0"
    )
    ancla = ingesta.evidence.anchor

    entradas = audit_log.tail(1000)
    indices = [i for i, e in enumerate(entradas) if e.get("event_hash") == ancla]
    assert indices, "el ancla no corresponde a ninguna entrada del audit log"

    i = indices[-1]
    assert entradas[i]["event_type"] == "epistemic_evidence_ingested"
    assert entradas[i]["context"]["tool_name"] == "run_code"
    # El eslabón: su prev_hash es el hash de la entrada anterior.
    if i > 0:
        assert entradas[i]["prev_hash"] == entradas[i - 1]["event_hash"]


def test_si_no_se_puede_auditar_la_evidencia_no_se_pierde_pero_queda_sin_ancla(monkeypatch):
    """
    Fail-closed VISIBLE: sin audit log no hay procedencia verificable, pero
    descartar una observación real sería peor. La evidencia entra, queda sin
    ancla, y el ledger la marca — degradación visible, no silenciosa.
    """
    import agent_core.epistemics.store as store_mod

    def explota(*args, **kwargs):
        raise OSError("disco de solo lectura")

    monkeypatch.setattr(store_mod.audit_log, "record", explota)

    store = _store()
    ingesta = store.ingest_tool_result(
        session_id="s", tool_name="run_code", observation="exit 0"
    )

    assert ingesta.evidence is not None  # la observación NO se pierde
    assert ingesta.evidence.anchor == ""  # pero no tiene procedencia verificable

    ledger = store.ledger("s")
    hipotesis = ledger.record_hypothesis("A", evidence_links=[ingesta.evidence.id])
    assert Flag.UNVERIFIABLE_PROVENANCE in ledger.flags(hipotesis.id)

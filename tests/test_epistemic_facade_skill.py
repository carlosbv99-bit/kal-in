"""
Tests de F2b: la fachada `epistemic_ledger` para terceros.

Cinco cosas, en orden de importancia:

1. **La copia vendorizada no puede separarse de la fuente.** La skill lleva
   `kal_epistemics/` adentro (es la única forma de que el contenedor la vea, y la
   vía que el roadmap recomienda a terceros). Eso crea el riesgo obvio: dos
   copias que se desincronizan. Hay un test que compara archivo por archivo.
2. **La fachada no duplica lógica.** Se verifica comparando el resultado de la
   skill contra el de llamar la librería directo: si alguien reimplementara una
   regla adentro del `tool.py`, los dos caminos podrían empezar a discrepar y este
   test lo tiene que ver.
3. **Es advisory y lo dice.** El bloque `advisory` viaja en cada respuesta: sin
   gate, con el origen declarado por el llamador, sin persistencia y sin la
   política de exposición del host.
4. **El contrato JSON falla cerrado**: operación desconocida, claves
   desconocidas, origen inválido, estado ilegible o de otra versión, sin estado.
5. **No es ingerible.** Su salida es un eco del estado que el llamador ya tiene:
   si el ledger la ingiriera como observación, sería lavado de origen —
   exactamente el lazo que F2 cerró por allowlist.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_core.epistemics import OBSERVATION_TOOLS, EpistemicStore
from agent_core.llm.agent_loop import AgentLoop, _agent_tool_from_tool
from kal_epistemics import HypothesisLedger, ReopenAction, ReopenCondition
from kernel.registry.skills import parse_manifest
from tests.conftest import requires_docker

RAIZ = Path(__file__).resolve().parents[1]
SKILL_DIR = RAIZ / "skills" / "epistemic_ledger"
FUENTE = RAIZ / "kal_epistemics"


def _cargar_tool():
    """
    Importa el `tool.py` de la skill. Nota honesta sobre qué prueba esto: como el
    repo root ya tiene `kal_epistemics` importado, el `from kal_epistemics import
    ...` de adentro resuelve a la copia del REPO (Python cachea por nombre de
    módulo). O sea: estos tests ejercitan la lógica de la fachada, y el que prueba
    que la copia vendorizada se basta a sí misma es el subprocess de abajo.
    """
    sys.path.insert(0, str(SKILL_DIR))
    try:
        spec = importlib.util.spec_from_file_location("epistemic_ledger_tool", SKILL_DIR / "tool.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        return modulo
    finally:
        sys.path.remove(str(SKILL_DIR))


@pytest.fixture(scope="module")
def facade():
    return _cargar_tool()


def _tool_con_manifiesto(facade):
    """
    Instancia de la Tool con el manifiesto que le inyecta el loader al cargar la
    skill (la clase no lo declara: `skill.yaml` es la única fuente de verdad, ver
    `kernel/registry/skills.py`). Los tests que no pasan por el loader lo arman
    con la misma forma.
    """
    from sdk.permissions import Permission
    from sdk.skill import ToolManifest

    manifest = parse_manifest(SKILL_DIR / "skill.yaml")
    tool = facade.EpistemicLedgerTool()
    tool.manifest = ToolManifest(
        name=manifest.name,
        description=manifest.description,
        permissions=frozenset(Permission(p) for p in manifest.permissions),
        created_by="system",
        parameters_schema=manifest.parameters_schema,
    )
    return tool


def _correr(facade, operacion: str, **entrada):
    artefacto = facade.EpistemicLedgerTool().execute(operacion=operacion, entrada=entrada or None)
    return artefacto.metadata["respuesta"]


# --------------------------------------------------------------------------
# 1. La copia vendorizada
# --------------------------------------------------------------------------


def test_la_copia_vendorizada_es_identica_a_la_fuente():
    """
    La skill lleva su propia copia de la librería portable. Este test es la
    guarda: si alguien arregla la librería y no la copia (o al revés), las dos
    versiones empiezan a comportarse distinto y nadie se entera hasta que un
    tercero reporta algo raro.
    """
    fuente = {p.name: p.read_bytes() for p in FUENTE.glob("*.py")}
    copia = {p.name: p.read_bytes() for p in (SKILL_DIR / "kal_epistemics").glob("*.py")}

    assert set(copia) == set(fuente), (
        "el conjunto de archivos vendorizados no coincide con kal_epistemics/: "
        f"faltan {sorted(set(fuente) - set(copia))}, sobran {sorted(set(copia) - set(fuente))}"
    )
    distintos = sorted(n for n in fuente if fuente[n] != copia[n])
    assert distintos == [], (
        f"la copia vendorizada difiere de la fuente en {distintos}: si el cambio es "
        "intencional, copiá los archivos y volvé a firmar la skill "
        "(scripts/sign_skill.py skills/epistemic_ledger/)"
    )


def test_la_copia_vendorizada_funciona_sola_en_un_proceso_limpio():
    """
    La prueba de que la skill arranca en un contenedor, donde lo único que existe
    es su propia carpeta — con una fidelidad que la primera versión de este test NO
    tenía y que costó un bug real: el runner carga la skill por RUTA DE ARCHIVO
    (`spec_from_file_location("/workspace/skill/tool.py")`) y **no** agrega la
    carpeta a `sys.path`. La versión anterior hacía `sys.path.insert(0, skill_dir)`
    + `from tool import ...`, o sea que le regalaba a la skill la condición que en
    el contenedor no tiene: el test pasaba y la skill no arrancaba en el sandbox
    (`ModuleNotFoundError: kal_epistemics`, encontrado recién al correr un
    contenedor de verdad). Acá se imita al runner.
    """
    guion = textwrap.dedent(
        """
        import importlib.util, json, sys
        # Igual que kernel/lifecycle/skill_runner.py: por ruta de archivo y sin
        # agregar la carpeta de la skill a sys.path. Lo único que se agrega es la
        # raíz del repo, que en el contenedor equivale a que el runner copia `sdk/`.
        sys.path.append(sys.argv[2])
        spec = importlib.util.spec_from_file_location("kal_sandboxed_skill", sys.argv[1])
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        herramienta = modulo.EpistemicLedgerTool()
        r = herramienta.execute(operacion="estado_vacio").metadata["respuesta"]
        import kal_epistemics
        print(json.dumps({
            "ok": r["ok"],
            "claves": sorted(r["estado"]),
            "libreria_usada": kal_epistemics.__file__,
        }))
        """
    )
    resultado = subprocess.run(
        [sys.executable, "-c", guion, str(SKILL_DIR / "tool.py"), str(RAIZ)],
        capture_output=True, text=True, cwd=str(SKILL_DIR), timeout=120,
        env={"PATH": "/usr/bin:/bin"},  # sin PYTHONPATH
    )

    assert resultado.returncode == 0, resultado.stderr
    salida = json.loads(resultado.stdout.strip().splitlines()[-1])
    assert salida["ok"] is True
    assert "schema_version" in salida["claves"]
    assert Path(salida["libreria_usada"]).resolve().is_relative_to(SKILL_DIR.resolve()), (
        "el proceso importó la librería del repo en vez de la copia vendorizada: la "
        "skill no se basta a sí misma"
    )


# --------------------------------------------------------------------------
# 2. La fachada no duplica lógica
# --------------------------------------------------------------------------


def test_el_resultado_es_el_mismo_que_llamar_la_libreria_directo(facade):
    """
    Si el `tool.py` reimplementara una regla, este test podría pasar hoy y fallar
    mañana (o al revés). Se compara la cadena completa: abrir pregunta, registrar
    hipótesis con dos condiciones, ingerir una observación que dispara las dos.
    """
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(
        facade, "abrir_pregunta", estado=estado,
        pregunta="¿Por qué se cayó el servicio?", faltantes=["el log"], reabrir_con=["Out of memory"],
    )
    registrado = _correr(
        facade, "registrar_hipotesis", estado=abierto["estado"],
        pregunta_id=abierto["resultado"]["pregunta_id"], claim="Se cayó por OOM",
        reabrir_con=["Out of memory"], refutar_con=["deploy"],
    )
    ingerido = _correr(
        facade, "ingerir_evidencia", estado=registrado["estado"],
        contenido="kernel: Out of memory Killed 4821, deploy 03:12",
        origen="kernel_observed", fuente="run_code", ancla="evt_1", ancla_tipo="audit_event_hash",
    )

    # El mismo camino, con la librería directo.
    led = HypothesisLedger(clock=lambda: 1000.0)
    pregunta, _ = led.open_question(
        "¿Por qué se cayó el servicio?", missing=["el log"],
        reopen_conditions=[ReopenCondition.from_untrusted(content_keywords=("Out of memory",))],
    )
    hipotesis = led.record_hypothesis(
        "Se cayó por OOM", question_id=pregunta.id,
        reopen_conditions=[
            ReopenCondition.from_untrusted(content_keywords=("Out of memory",)),
            ReopenCondition.from_untrusted(content_keywords=("deploy",), action=ReopenAction.REFUTE),
        ],
    )
    from kal_epistemics import Evidence, EvidenceOrigin

    led.consider(
        Evidence.from_content(
            "kernel: Out of memory Killed 4821, deploy 03:12",
            origin=EvidenceOrigin.KERNEL_OBSERVED, source="run_code",
            anchor="evt_1", anchor_kind="audit_event_hash",
        )
    )

    final = led.hypothesis(hipotesis.id)
    assert ingerido["resultado"]["reaperturas"][0]["estado"] == final.status.value
    assert ingerido["resultado"]["reaperturas"][0]["nivel"] == led.level(hipotesis.id)
    assert ingerido["resultado"]["reaperturas"][0]["flags"] == sorted(f.value for f in led.flags(hipotesis.id))


def test_recargar_reanuda_el_mismo_estado(facade):
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(facade, "abrir_pregunta", estado=estado, pregunta="¿cuánto gana Marta?")

    reanudado = _correr(facade, "reanudar", estado=abierto["estado"])

    assert reanudado["ok"] is True
    assert reanudado["resultado"]["resumen"]["preguntas"][0]["texto"] == "¿cuánto gana Marta?"
    assert reanudado["estado"] == abierto["estado"]


# --------------------------------------------------------------------------
# 3. Advisory: lo que la fachada NO da, dicho en cada respuesta
# --------------------------------------------------------------------------


def test_cada_respuesta_declara_que_es_advisory(facade):
    """
    El valor de F2b depende de que un tercero no confunda "capacidad" con
    "garantía". El bloque viaja en TODAS las respuestas (también en las que
    fallan) para que no se pueda leer el resultado sin verlo.
    """
    for respuesta in (
        _correr(facade, "estado_vacio"),
        _correr(facade, "consultar"),
        _correr(facade, "operacion_que_no_existe"),
    ):
        advisory = respuesta["advisory"]
        assert advisory["gate_aplicado_por_esta_skill"] is False
        assert "ruta de salida" in advisory["por_que_no"]
        assert "origen" in advisory["origen_confiado_del_llamador"]
        assert "ninguna" in advisory["persistencia"]
        assert "saneamiento" in advisory


def test_la_abstencion_es_estable_entre_parafrasis(facade):
    """
    La capacidad que más importa para un tercero: "no lo sé" repetible. Dos
    paráfrasis de la misma pregunta rinden el MISMO texto, sin modelo de por medio.
    """
    estado = _correr(facade, "estado_vacio")["estado"]
    primera = _correr(
        facade, "abrir_pregunta", estado=estado,
        pregunta="¿Por qué se cayó el servicio a las 3:14?", faltantes=["el log del kernel"],
    )
    segunda = _correr(
        facade, "abrir_pregunta", estado=primera["estado"],
        pregunta="¿por que se cayo el servicio a las 3:14?", faltantes=["el log del kernel"],
    )

    a1 = _correr(facade, "abstencion", estado=segunda["estado"],
                 pregunta_id=primera["resultado"]["pregunta_id"])
    a2 = _correr(facade, "abstencion", estado=segunda["estado"],
                 pregunta="¿POR QUÉ SE CAYÓ EL SERVICIO A LAS 3:14?")

    assert segunda["resultado"]["creada"] is False
    assert a1["resultado"]["texto"] == a2["resultado"]["texto"]
    assert a1["resultado"]["tipo"] == "ignorance"
    assert "el log del kernel" in a1["resultado"]["texto"]


def test_la_evidencia_del_modelo_se_puede_registrar_pero_nunca_reabre(facade):
    """
    Regla estructural de la librería, que la fachada hereda sin reimplementar:
    `model_generated` se registra (queda trazable y pesa 0) y no reabre nada.
    """
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(facade, "abrir_pregunta", estado=estado, pregunta="¿por qué?")
    registrado = _correr(
        facade, "registrar_hipotesis", estado=abierto["estado"],
        pregunta_id=abierto["resultado"]["pregunta_id"], claim="fue el deploy",
        reabrir_con=["deploy"],
    )
    ingerido = _correr(
        facade, "ingerir_evidencia", estado=registrado["estado"],
        contenido="deploy aplicado a las 03:12", origen="model_generated",
    )

    assert ingerido["ok"] is True
    assert ingerido["resultado"]["reaperturas"] == []
    assert ingerido["resultado"]["advertencia_origen"]


# --------------------------------------------------------------------------
# 4. El contrato falla cerrado
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operacion, entrada",
    [
        ("operacion_que_no_existe", None),
        ("consultar", {"estado": {"schema_version": 99}}),
        ("consultar", {"estado": "esto no es json"}),
        ("ingerir_evidencia", {"contenido": "x", "origen": "de_la_nada"}),
        ("registrar_hipotesis", {"claim": "algo", "reabrir_con": ["no"]}),  # marcador muy corto
    ],
)
def test_el_contrato_falla_cerrado(facade, operacion, entrada):
    respuesta = facade.EpistemicLedgerTool().execute(operacion=operacion, entrada=entrada).metadata["respuesta"]

    assert respuesta["ok"] is False
    assert respuesta["error"]
    assert respuesta["estado"] is None, "un error no devuelve un estado que parezca válido"


def test_las_claves_desconocidas_se_rechazan(facade):
    """Un contrato que acepta cualquier cosa no es un contrato: si te equivocás
    en el nombre de una clave, te enterás."""
    respuesta = _correr(facade, "consultar", estado={"schema_version": 1}, limitee=5)

    assert respuesta["ok"] is False
    assert "claves desconocidas" in respuesta["error"]


def test_las_operaciones_que_continuan_un_estado_lo_exigen(facade):
    respuesta = _correr(facade, "consultar")

    assert respuesta["ok"] is False
    assert "necesita 'estado'" in respuesta["error"]


def test_las_condiciones_van_por_el_camino_no_confiable(facade):
    """
    El llamador de una Skill puede estar reflejando texto no confiable, así que
    las condiciones se construyen con `from_untrusted`: marcadores, nunca
    patrones. Un valor con pinta de regex catastrófica entra como substring
    literal y no puede colgar al matcher.
    """
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(facade, "abrir_pregunta", estado=estado, pregunta="¿por qué?")
    registrado = _correr(
        facade, "registrar_hipotesis", estado=abierto["estado"],
        pregunta_id=abierto["resultado"]["pregunta_id"], claim="algo",
        reabrir_con=["(a+)+$"],
    )

    assert registrado["ok"] is True
    hipotesis = registrado["estado"]["hypotheses"][0]
    condicion = hipotesis["reopen_conditions"][0]
    assert condicion["content_keywords"] == ["(a+)+$"]
    assert condicion["content_pattern"] == ""


# --------------------------------------------------------------------------
# 5. No es ingerible (su salida es eco)
# --------------------------------------------------------------------------


def test_la_fachada_no_esta_en_la_allowlist_de_ingestion():
    """
    La fachada devuelve el estado epistémico del llamador: hipótesis, preguntas y
    déficits que el propio agente escribió. Si fuera ingerible, el ledger se
    alimentaría de su propio eco — el lazo de lavado de origen que F2 cerró por
    allowlist, con una vuelta más.
    """
    assert "epistemic_ledger" not in OBSERVATION_TOOLS


def test_por_el_camino_real_no_ingiere_nada(facade):
    """Lo mismo, pero por el camino real del loop: la observación vuelve al
    modelo y NO entra al ledger."""
    store = EpistemicStore(clock=lambda: 1000.0)
    loop = AgentLoop(llm_client=object(), task_executor=object(), memory=object(), epistemics=store)
    herramienta = _agent_tool_from_tool("epistemic_ledger", _tool_con_manifiesto(facade))

    salida = loop._dispatch_tool(
        "epistemic_ledger", {"operacion": "estado_vacio"}, {"epistemic_ledger": herramienta},
        session_id="s",
    )

    assert '"ok": true' in salida
    assert store.ledger("s").exogenous_evidence_ids() == frozenset()


def test_el_loader_registra_la_fachada_como_herramienta():
    from agent_core.default_tools import register_default_static_tools
    from kernel.registry.registry import tool_registry
    from sdk.permissions import Permission

    register_default_static_tools()
    registrada = tool_registry.get("epistemic_ledger")

    assert registrada is not None
    assert registrada.manifest.name == "epistemic_ledger"
    assert getattr(registrada, "entry_point", None) == "tool:EpistemicLedgerTool"
    # El manifiesto agrega `filesystem_read` implícito a TODA tool (ver
    # ToolManifest.__post_init__), así que lo que se verifica es que no pida red.
    assert Permission.NETWORK not in registrada.manifest.permissions


def test_la_fachada_no_pide_red_ni_escritura():
    """
    Es una skill sin estado y sin red. Importa por dos motivos: si pidiera red, la
    cascada del kernel la rechazaría por defecto (y la fachada no serviría para
    nada sin tocar config); y si escribiera en disco, tendría una forma de
    persistir que no puede sostener entre invocaciones (el contenedor es efímero).
    """
    from sdk.permissions import Permission

    manifest = parse_manifest(SKILL_DIR / "skill.yaml")

    assert manifest.permissions == []
    assert Permission.NETWORK not in {Permission(p) for p in manifest.permissions}


def test_la_skill_esta_firmada_y_su_manifiesto_es_valido():
    """
    `scripts/validate_skills.py` exige firma verificada para TODAS las skills del
    repo, y acá pesa más que en otras: la firma cubre también la librería
    vendorizada (12 archivos), así que es lo que ata la copia a la identidad del
    autor. Editar cualquier archivo de adentro —incluida la librería— invalida la
    firma hasta que se vuelva a firmar.
    """
    from kernel.registry.skill_signing import verify_skill_signature

    manifest = parse_manifest(SKILL_DIR / "skill.yaml")

    assert manifest.name == "epistemic_ledger"
    assert manifest.enabled is True
    assert manifest.entry_point == "tool:EpistemicLedgerTool"
    assert verify_skill_signature(SKILL_DIR) == "verified"


# --------------------------------------------------------------------------
# 6. End-to-end en el sandbox real (el criterio de F2b: como el resto de las
#    skills). Es además la prueba de que la librería vendorizada se copia y se
#    importa bien DENTRO del contenedor, que es donde tiene que servir.
# --------------------------------------------------------------------------


@requires_docker
def test_end_to_end_en_el_sandbox_real(tmp_path):
    """
    A diferencia de `claim_verifier`, la fachada no pide permisos: la cascada la
    deja pasar por defecto, así que el test no necesita otorgar nada. Corre una
    operación completa (`estado_vacio` + `abrir_pregunta`) dentro del contenedor y
    verifica que la respuesta sea el mismo sobre JSON.
    """
    from kernel.lifecycle.docker_runner import DockerSandboxRunner
    from kernel.lifecycle.executor import SandboxExecutor
    from kernel.registry.sandboxed_skill import SandboxedSkillTool
    from sdk.permissions import Permission
    from sdk.skill import ToolManifest

    manifest = parse_manifest(SKILL_DIR / "skill.yaml")
    tool = SandboxedSkillTool(
        manifest=ToolManifest(
            name=manifest.name,
            description=manifest.description,
            permissions=frozenset(Permission(p) for p in manifest.permissions),
            created_by="system",
            parameters_schema=manifest.parameters_schema,
        ),
        skill_dir=SKILL_DIR,
        entry_point=manifest.entry_point,
        image="python:3.11-slim",
        sandbox=SandboxExecutor(runner=DockerSandboxRunner()),
        artifacts_root=tmp_path / "artifacts",
    )

    inicial = json.loads(tool.execute(operacion="estado_vacio").metadata["summary"])
    assert inicial["ok"] is True
    assert inicial["advisory"]["gate_aplicado_por_esta_skill"] is False

    abierto = json.loads(
        tool.execute(
            operacion="abrir_pregunta",
            entrada={"estado": inicial["estado"], "pregunta": "¿por qué se cayó el servicio?"},
        ).metadata["summary"]
    )
    assert abierto["ok"] is True, abierto.get("error")
    assert abierto["resultado"]["creada"] is True


# --------------------------------------------------------------------------
# 7. Introspección por la fachada: trayectoria y monitores
# --------------------------------------------------------------------------


def _hasta_una_reapertura(facade):
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(
        facade, "abrir_pregunta", estado=estado,
        pregunta="¿por qué se cayó el servicio?", reabrir_con=["deploy"],
    )
    registrado = _correr(
        facade, "registrar_hipotesis", estado=abierto["estado"],
        pregunta_id=abierto["resultado"]["pregunta_id"], claim="fue el deploy",
        reabrir_con=["deploy"],
    )
    ingerido = _correr(
        facade, "ingerir_evidencia", estado=registrado["estado"],
        contenido="el deploy de las 03:12 rompió el esquema", origen="kernel_observed",
        fuente="run_code", ancla="f" * 64, ancla_tipo="audit_event_hash",
    )
    return ingerido["estado"], registrado["resultado"]["hipotesis_id"]


def test_la_fachada_expone_la_trayectoria(facade):
    """
    Un tercero que guarda el snapshot puede reconstruir la biografía de una
    hipótesis, no solo su estado actual: es lo que hace visible que una
    `reactivated` pudo haber estado refutada antes.
    """
    estado, hid = _hasta_una_reapertura(facade)

    r = _correr(facade, "trayectoria", estado=estado, hipotesis_id=hid)

    assert r["ok"] is True
    cambios = [t for t in r["resultado"]["transiciones"] if t["cambia_estado"]]
    assert [t["hacia"] for t in cambios] == ["open", "reactivated"]
    assert r["resultado"]["estado"] == "reactivated"
    assert r["resultado"]["historial_podado"] is False


def test_la_trayectoria_se_puede_acotar_y_exige_hipotesis(facade):
    estado, hid = _hasta_una_reapertura(facade)

    acotada = _correr(facade, "trayectoria", estado=estado, hipotesis_id=hid, limite=1)
    sin_id = _correr(facade, "trayectoria", estado=estado)

    assert len(acotada["resultado"]["transiciones"]) == 1
    assert sin_id["ok"] is False
    assert "'hipotesis_id' es obligatorio" in sin_id["error"]


def test_la_fachada_expone_los_monitores(facade):
    """
    El caso de uso central de la mejora A del lado de un tercero: auditar el
    estado que le llegó ANTES de confiar en él. La carga valida estructura; los
    monitores miran si la historia tiene sentido.
    """
    estado, hid = _hasta_una_reapertura(facade)

    sano = _correr(facade, "diagnosticar", estado=estado)

    assert sano["ok"] is True
    assert sano["resultado"]["sano"] is True
    assert len(sano["resultado"]["verificados"]) == 6

    # Un estado estructuralmente válido y absurdamente falso: 5 reaperturas
    # contadas que su historial no registra.
    roto = dict(estado)
    roto["reopen_count"] = [[hid, 5]]
    d = _correr(facade, "diagnosticar", estado=roto)

    assert d["resultado"]["sano"] is False
    assert "reaperturas_vs_historial" in {
        h["invariante"] for h in d["resultado"]["hallazgos"]
    }


def test_reanudar_un_estado_guardado_viene_con_su_diagnostico(facade):
    """
    Reanudar es exactamente el momento de mirar: si el diagnóstico no viajara con
    la respuesta, un snapshot absurdo se descubriría tres días después.
    """
    estado, hid = _hasta_una_reapertura(facade)

    limpio = _correr(facade, "reanudar", estado=estado)
    roto = dict(estado)
    roto["reopen_count"] = [[hid, 5]]
    sucio = _correr(facade, "reanudar", estado=roto)

    assert limpio["resultado"]["diagnostico"]["sano"] is True
    assert sucio["resultado"]["diagnostico"]["sano"] is False
    assert sucio["ok"] is True, "un estado absurdo se reporta, no se rechaza"


def test_la_fachada_deja_declarar_alternativas_incompatibles(facade):
    """
    C por la fachada: el tercero declara que dos de sus hipótesis son alternativas
    incompatibles, y el conflicto queda NOMBRABLE en su estado — no como una
    decisión de redacción suya en cada turno.
    """
    estado = _correr(facade, "estado_vacio")["estado"]
    abierto = _correr(facade, "abrir_pregunta", estado=estado, pregunta="¿por qué se cayó?")
    qid = abierto["resultado"]["pregunta_id"]
    h1 = _correr(
        facade, "registrar_hipotesis", estado=abierto["estado"], pregunta_id=qid,
        claim="fue el OOM", reabrir_con=["OutOfMemory"],
    )
    h2 = _correr(
        facade, "registrar_hipotesis", estado=h1["estado"], pregunta_id=qid,
        claim="fue el deploy", reabrir_con=["deploy"],
        excluye_a=[h1["resultado"]["hipotesis_id"]],
    )

    assert h2["resultado"]["excluye_a"] == [h1["resultado"]["hipotesis_id"]]
    guardadas = {h["id"]: h["excludes"] for h in h2["estado"]["hypotheses"]}
    assert guardadas[h1["resultado"]["hipotesis_id"]] == [h2["resultado"]["hipotesis_id"]]
    assert guardadas[h2["resultado"]["hipotesis_id"]] == [h1["resultado"]["hipotesis_id"]]

    # Las dos respaldadas por evidencia distinta → conflicto marcado en las dos.
    e1 = _correr(facade, "ingerir_evidencia", estado=h2["estado"],
                 contenido="kernel: OutOfMemory killed 4821", origen="kernel_observed")
    e2 = _correr(facade, "ingerir_evidencia", estado=e1["estado"],
                 contenido="coincide con el deploy 2026.09.29-3", origen="tool_external")
    d = _correr(facade, "diagnosticar", estado=e2["estado"])

    assert d["resultado"]["sano"] is True
    for hid in (h1["resultado"]["hipotesis_id"], h2["resultado"]["hipotesis_id"]):
        hipotesis = [h for h in e2["estado"]["hypotheses"] if h["id"] == hid][0]
        assert hipotesis["status"] == "reactivated"


def test_la_fachada_deja_declarar_hasta_cuando_vale_una_evidencia(facade):
    """D1 por la fachada: la ventana la declara quien trae el dato y vencer no
    cambia el estado, solo marca."""
    estado = _correr(facade, "estado_vacio")["estado"]

    r = _correr(
        facade, "ingerir_evidencia", estado=estado, contenido="la API respondió 200",
        origen="kernel_observed", valido_desde=10.0, valido_hasta=20.0,
    )

    assert r["ok"] is True
    evidencia = r["estado"]["evidence"][0]
    assert evidencia["valid_from"] == 10.0 and evidencia["valid_until"] == 20.0

    malo = _correr(
        facade, "ingerir_evidencia", estado=estado, contenido="otra", origen="kernel_observed",
        valido_hasta="mañana",
    )
    assert malo["ok"] is False
    assert "tiene que ser un número" in malo["error"]

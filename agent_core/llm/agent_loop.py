"""
Loop de razonamiento con herramientas (estilo ReAct) que convierte a
kal de "infraestructura" a "agente utilizable": toma un objetivo en
lenguaje natural, decide qué hacer usando el modelo de Ollama
configurado, y ejecuta acciones reales (código en sandbox, memoria,
herramientas multimodales) hasta llegar a una respuesta final o agotar
el presupuesto de pasos.

Diseño ReAct simplificado:
  1. Se arma el mensaje de sistema con la descripción de kal y el
     catálogo de herramientas disponibles (JSON schema por herramienta).
  2. Se llama a Ollama con la conversación + las herramientas.
  3. Si el modelo pide llamar una herramienta: se ejecuta, se agrega el
     resultado a la conversación como mensaje role="tool", y se repite
     desde (2).
  4. Si el modelo responde con contenido final (sin tool_calls): esa es
     la respuesta, se corta el loop.
  5. Si se agota max_steps sin una respuesta final: se corta igual,
     devolviendo lo último que se tenga, marcado como incompleto — un
     agente que nunca se detiene es tan peligroso como uno que nunca
     actúa (mismo espíritu que el circuit breaker de auto-reparación).

Todo lo que este loop ejecuta como "código" pasa por
task_execution/executor.py::run_sandboxed (nunca in-process) — el LLM
decide QUÉ hacer, pero el sandbox sigue siendo quien decide qué tan
peligroso se le permite ser.

Catálogo de herramientas (arquitectura de plataforma): cuando no se
inyecta `tools=` explícito (el override que usan los tests para
inyectar dobles, que queda fijo tal cual se pasó), el catálogo se
recalcula en CADA run() combinando kernel.registry.registry
(imagen/audio/video hoy; browser/skills/herramientas dinámicas del
agente en fases futuras, sin tocar este archivo) con tres `Tool` de
instancia atados a este loop en particular (run_code/remember/recall,
ver tool_integration/adapters/core_tools.py) — recalcular en cada
run() importa porque una herramienta dinámica creada a mitad de
conversación debe quedar disponible en el siguiente turno.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from agent_core.capability_broker import capability_broker
from agent_core.client_provider import _MULTIMEDIA_TOOL_NAMES, _VSCODE_ONLY_TOOL_NAMES, get_client_provider
from agent_core.llm.json_extraction import extract_json_array, extract_json_object
from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.provider import LLMProvider, ProviderError, ToolCall
from agent_core.llm.self_check_tracker import SelfCheckTracker
from agent_core.llm.tool_repeat_limiter import ToolRepeatLimiter
from agent_core.memory.manager import MemoryManager
from audit.audit_log import AuditEvent, audit_log
from task_execution.executor import TaskExecutor
from tool_integration.adapters.core_tools import CodeExecutionTool, MemoryRecallTool, MemoryRememberTool
from sdk.skill import Tool
from sdk.artifacts import Artifact
from kernel.permissions.permission_cascade import permission_cascade, trust_tier_for
from sdk.permissions import Permission
from kernel.registry.registry import ToolRegistry
from kernel.registry.registry import tool_registry as default_tool_registry
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AgentTool:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[..., str]
    # Poblado por el handler de _agent_tool_from_tool() con el Artifact
    # crudo que devolvió la Tool real (antes de aplanarlo a texto) — así
    # run() puede trackear qué artefacto generó cada paso sin cambiar la
    # firma de handler (sigue devolviendo str, no rompe los AgentTool de
    # test que ya inyectan handlers propios).
    last_artifact: Artifact | None = None
    # Para la cascada de permisos (sdk/permissions.py::
    # PermissionCascade) — poblados por _agent_tool_from_tool() a partir
    # del ToolManifest real. Los AgentTool de test que se construyen a
    # mano (tools=[AgentTool(...)] inyectado) quedan con los defaults de
    # abajo: sin permisos declarados y tier "system", que la cascada
    # nunca restringe por defecto — no rompe ningún test existente.
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    trust_tier: str = "system"

    def to_ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters_schema},
        }


@dataclass
class AgentStep:
    tool_name: str
    arguments: dict[str, Any]
    observation: str
    artifact: Artifact | None = None


@dataclass
class AgentRunResult:
    goal: str
    final_answer: str
    steps: list[AgentStep] = field(default_factory=list)
    status: str = "success"  # success | max_steps_exceeded | llm_error
    # Nombres de herramientas que se autochequearon con analyze_image en
    # este turno (ver el comentario de self_checked_tools más abajo en
    # run()) — agent_core/routers/chat.py lo usa para saber qué llamadas
    # repetidas a la MISMA herramienta son en realidad una regeneración
    # que reemplaza al intento anterior (no dos resultados distintos),
    # incluso si el prompt de la regeneración quedó reformulado distinto
    # al original.
    self_checked_tools: frozenset[str] = frozenset()


# HISTORIA COMPLETA DE LOS BUGS REALES QUE MOTIVARON CADA REGLA DE
# SYSTEM_PROMPT (2026-07-30: separado del texto que se le manda al
# modelo — ver docs/HISTORY.md, "reducir el overhead de tokens fijo
# por mensaje"). El modelo solo necesita la REGLA final, no la
# narrativa completa de cada incidente; esa narrativa sigue viva acá,
# como referencia para quien lea el código, sin gastar ~2300 tokens
# de contexto en CADA mensaje (incluido un simple "hola").
#
# - "generá EXACTAMENTE lo que se pidió": pedido "generame un
#   sombrero" terminó generando CUATRO imágenes de sombreros,
#   agregándole título a dos, y combinando dos en una composición —
#   nada de eso se pidió.
# - Autochequeo de cantidad exacta: "crea una naranja (solo una)"
#   generó bien, pero el autochequeo sin límite llevó a regenerar una
#   y otra vez hasta agotar todos los pasos SIN darle ninguna
#   respuesta al usuario. Después, "crea una orca" (SIN la palabra
#   "solo") generó DOS orcas completas sin que el autochequeo (que
#   antes solo se disparaba con "solo una/un X") lo detectara nunca —
#   de ahí que "una/un X" ya alcance, sin necesitar "solo".
# - Inpaint sobre imagen existente: "hay dos orcas en la imagen, borra
#   una de ellas" fue directo a inpaint con un 'box' adivinado sin
#   haber llamado nunca a analyze_image — el resultado terminó siendo
#   una composición completamente distinta a la original.
# - run_code vs. propose_project_files: "creá la página web para una
#   panadería" generó código con `open('index.html', 'w')` y
#   `import os`, rechazado por el validador del sandbox, después de
#   gastar un paso entero en el intento fallido.
# - Herramientas irrelevantes en pedidos conversacionales: el modelo
#   llamó a la skill system_info (contenedor Docker efímero) antes de
#   responder "¿quién sos?", mezclando SO/Python/disco libre en su
#   autopresentación para una pregunta puramente conversacional.
# - Negar una capacidad sin comprobarla: pedido "lee y entregame el
#   audio de todo tu ultimo comentario con voz femenina" (texto ya
#   presente en el HISTORIAL) respondió "no tengo la capacidad de leer
#   textos o generar audio" — FALSO, audio_generation sí estaba
#   disponible y funcionaba para exactamente ese caso.
SYSTEM_PROMPT = """Eres kal, un agente de IA que ejecuta tareas usando herramientas reales, \
no solo texto. Todo el código que ejecutas corre en un sandbox aislado (sin red por defecto, \
filesystem read-only salvo tu área de trabajo) — esto es una garantía de seguridad real, no una \
sugerencia, y no puedes ni debes intentar evadirla.

Reglas:
- Usa una herramienta SOLO si la tarea realmente la necesita (cálculos, generar contenido,
  buscar en memoria). Preguntas conversacionales o sobre vos mismo se responden directo, sin
  llamar a ninguna herramienta.
- No inventes resultados de una herramienta que no llamaste.
- Si una herramienta falla, decide si tiene sentido reintentar con otro enfoque o informar el fallo.
- La memoria que trae recall() puede estar desactualizada. Cada resultado indica su nivel de
  confianza entre corchetes ([temporal], [aprendida], [verificada], [permanente], [externa]). Si
  algo que ya generaste u observaste EN ESTA MISMA conversación contradice lo que trajo recall(),
  confiá en tu observación directa y reciente, no en la memoria recuperada — especialmente si está
  marcada [temporal] o [aprendida].
- No inventes ni guardes con remember() datos que no confirmaste realmente. Si no estás seguro de
  algo, decilo en vez de inventar algo plausible.
- Cuando tengas la respuesta final, respóndela directamente sin llamar a más herramientas.
- Sé directo y conciso en la respuesta final.
- Generá EXACTAMENTE lo que se pidió, ni más ni menos: si piden "una imagen de X", generá UNA
  sola, no varias variantes. No encadenes herramientas extra (agregar texto/título, componer o
  combinar imágenes, o analizarla con analyze_image) a menos que el pedido lo mencione
  explícitamente.
- Si el pedido especifica una CANTIDAD exacta de un objeto contable ("una/un X" alcanza,
  NO hace falta que diga "SOLO una/un X": pedir "una orca" ya implica una sola), podés
  llamar UNA vez a analyze_image sobre tu propio resultado recién generado para confirmarlo,
  y si no coincide,
  regenerar COMO MUCHO una vez más — nunca más de eso (el sistema lo bloquea estructuralmente de
  todos modos). Los modelos de generación de imágenes (SDXL-Turbo local) NO respetan de forma
  confiable cantidades exactas de objetos. Si tras ese único reintento el resultado TODAVÍA no
  coincide, entregalo igual y decilo honestamente en tu respuesta final — nunca sigas intentando,
  nunca afirmes que coincide si no coincide. Para cualquier otro caso, NO llames a analyze_image
  sobre tu propia generación.
- Antes de usar image_editing con operation="inpaint" para modificar un objeto ESPECÍFICO en una
  imagen YA EXISTENTE (no una que generaste vos mismo en este mismo turno), llamá primero a analyze_image
  preguntando específicamente por la UBICACIÓN aproximada del objeto y usá esa descripción para
  elegir un 'box' más informado que una adivinanza completamente a ciegas. Igual
  así, seguí aclarando en tu respuesta final que la posición sigue siendo una estimación — nunca
  afirmes que el resultado es exacto.
- run_code NUNCA puede crear archivos que el usuario se lleve (una página web, una app, un
  proyecto con varios archivos, un documento de texto): `import os` y `open()` están prohibidos
  a propósito en ese sandbox. Para un documento de texto simple (poema, notas, lista) usá
  create_text_file si la tenés disponible. Para un proyecto con varios archivos de código usá
  propose_project_files si la tenés disponible (solo VS Code). Si NO tenés ninguna de las dos
  disponible, no intentes escribirlo con run_code de todos modos — respondé con el contenido
  completo en la respuesta final en cambio.

Ejemplos de cuándo NO llamar a ninguna herramienta:
- "hola" / "¿quién sos?" / "quien eres" -> responder directo, sin llamar a NINGUNA herramienta (ni
  audio, ni system_info, ni ninguna otra). Una pregunta conversacional no autoriza llamar
  CUALQUIER herramienta.
- "¿qué hace este código? explicame" + código ya pegado en el mensaje -> leer el código dado y
  explicarlo con texto, sin ejecutar nada ni pedir información del sistema.
- Un pedido ambiguo en español con una palabra que también podría significar un dispositivo o
  concepto distinto (p.ej. "el ratón") -> interpretar por el CONTEXTO de la conversación, no el
  significado menos relacionado con lo que se venía haciendo.
- Si la pregunta ya se responde con algo que está en el HISTORIAL de la conversación o en el
  "Contexto de esta sesión" (p.ej. el artefacto activo) -> respondé con esa información tal cual,
  sin llamar a NINGUNA herramienta. NUNCA vuelvas a generar/ejecutar algo que ya existe solo para
  "confirmar" un dato que ya tenés.

Nunca neges una capacidad sin comprobarla primero (p.ej. generar audio con audio_generation, o
cualquier otra herramienta): antes de decir que NO PODÉS hacer algo, FIJATE primero en tu lista real de herramientas disponibles
AHORA MISMO — casi siempre kal SÍ tiene la capacidad (imagen,
audio, video, código, búsqueda web) y la herramienta correspondiente está ahí. Si el pedido es
ambiguo, pedí una aclaración concreta en vez de inventar una incapacidad — nunca al revés. Solo
mencioná una limitación real DESPUÉS de haber intentado de verdad la herramienta correspondiente y
haber recibido un rechazo concreto, nunca antes de intentarlo.
"""


def _artifact_to_observation(artifact: Artifact) -> str:
    """
    Convierte el resultado tipado de una Tool (Artifact) en el texto
    que se agrega a la conversación como mensaje role="tool". Dos
    convenciones cubren todas las Tool actuales:
      - modality="text" con "status" en metadata (convención de
        DynamicSandboxedTool/CodeExecutionTool): éxito -> stdout,
        fallo -> "ERROR (status): stderr".
      - modality="text" con "summary" en metadata (remember/recall):
        se devuelve tal cual.
      - cualquier otra modalidad (image/audio/video): referencia al
        artefacto generado.
    """
    if artifact.modality == "text":
        if "status" in artifact.metadata:
            if artifact.metadata["status"] == "success":
                return artifact.metadata.get("stdout") or "(sin salida)"
            return f"ERROR ({artifact.metadata['status']}): {artifact.metadata.get('stderr', '')}"
        if "summary" in artifact.metadata:
            return artifact.metadata["summary"]
        return str(artifact.metadata)
    if artifact.modality == "project_files":
        # Nunca el contenido completo de los archivos acá — infla el
        # historial de la conversación sin necesidad, el modelo no
        # necesita releerlo (la vista previa real la ve el USUARIO, del
        # lado de la extensión de VS Code, no el modelo).
        if artifact.metadata.get("status") == "requires_approval":
            return (
                "ERROR: esta acción requiere aprobación humana explícita antes de poder "
                "proponerse (política de filesystem_access en config.yaml) — avisale al "
                "usuario, no se creó ni propuso ningún archivo."
            )
        files = artifact.metadata.get("files", [])
        names = ", ".join(f["path"] for f in files)
        return f"Se prepararon {len(files)} archivo(s) para el proyecto ({names}) — el usuario decidirá si los aplica."
    if artifact.modality == "android_build_request":
        # AndroidBuildScreenshotTool (tool_integration/adapters/vscode_android.py):
        # a diferencia de workspace_file_request, esto NO se encadena de
        # vuelta a un paso siguiente del agente — el resultado (captura
        # real o el error de Gradle) se le muestra al usuario del lado
        # de la extensión directamente, el modelo nunca lo ve. El
        # mensaje deja explícito que sigue en curso, para que la
        # respuesta final del modelo no dé la tarea por terminada.
        return (
            "Pedido de compilar/instalar/capturar enviado a la extensión de VS Code — sigue en "
            "curso, todavía no hay resultado. El usuario va a ver el resultado real (captura de "
            "pantalla, o el error de compilación) directamente en el chat en un momento. No "
            "inventes ni asumas cómo se ve la app ni si terminó bien."
        )
    if artifact.modality == "workspace_file_request":
        # ReadWorkspaceFileTool (tool_integration/adapters/vscode_files.py):
        # acá tampoco hay contenido real todavía — lo pide la extensión de
        # VS Code recién en el próximo /chat encadenado (ver
        # vscode-extension/src/readWorkspaceFile.ts). El mensaje deja
        # explícito que hay que esperar, para que el modelo no invente el
        # contenido ni vuelva a pedir el mismo archivo en este mismo turno.
        path = artifact.metadata.get("path", "")
        return (
            f"Pedido de lectura de '{path}' enviado — su contenido real todavía no está disponible en "
            "este turno, va a llegar automáticamente como un mensaje nuevo en un paso siguiente de esta "
            "misma conversación. No inventes ni asumas su contenido."
        )
    return f"{artifact.modality}: archivo generado en {artifact.uri}"


def _agent_tool_from_tool(name: str, tool: Tool) -> AgentTool:
    # Construcción en dos pasos: el handler necesita una referencia al
    # AgentTool ya creado para poder guardarle last_artifact.
    agent_tool = AgentTool(
        name=name,
        description=tool.manifest.description,
        parameters_schema=tool.manifest.parameters_schema,
        handler=None,  # se completa abajo
        permissions=tool.manifest.permissions,
        trust_tier=trust_tier_for(tool),
    )

    def handler(**kwargs) -> str:
        artifact = tool.execute(**kwargs)
        agent_tool.last_artifact = artifact
        return _artifact_to_observation(artifact)

    agent_tool.handler = handler
    return agent_tool


# _MULTIMEDIA_TOOL_NAMES/_VSCODE_ONLY_TOOL_NAMES viven en
# agent_core/client_provider.py (el único lugar que sabe qué significa
# cada `client`, ver ClientProvider). Acá solo se importa
# _VSCODE_ONLY_TOOL_NAMES directamente porque además se usa más abajo
# para el tope de repeticiones por turno de esas 3 herramientas
# específicamente — un uso que NO depende de cuál sea el `client`
# activo (propiedad intrínseca de las herramientas: piden aprobación
# async, una segunda llamada en el mismo turno nunca tiene información
# nueva), así que no pasa por ClientProvider.excluded_tool_names().


class AgentLoop:
    def __init__(
        self,
        llm_client: LLMProvider | None = None,
        task_executor: TaskExecutor | None = None,
        memory: MemoryManager | None = None,
        tools: list[AgentTool] | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.llm = llm_client or OllamaClient()
        self.task_executor = task_executor or TaskExecutor()
        self.memory = memory or MemoryManager()
        self.tool_registry = tool_registry or default_tool_registry
        # Si se pasa `tools=` explícito, queda fijo tal cual (usado por
        # tests para inyectar dobles) — nunca se mezcla con el registry.
        self._explicit_tools: dict[str, AgentTool] | None = (
            {tool.name: tool for tool in tools} if tools is not None else None
        )

    def _current_tools(
        self, client: str | None = None, required_capabilities: list[str] | None = None
    ) -> dict[str, AgentTool]:
        if self._explicit_tools is not None:
            return self._explicit_tools
        return self._build_tools_from_registry(client, required_capabilities)

    def _build_tools_from_registry(
        self, client: str | None = None, required_capabilities: list[str] | None = None
    ) -> dict[str, AgentTool]:
        instance_tools: dict[str, Tool] = {
            "run_code": CodeExecutionTool(self.task_executor),
            "remember": MemoryRememberTool(self.memory),
            "recall": MemoryRecallTool(self.memory),
        }
        merged: dict[str, Tool] = {**self.tool_registry.active_tools(), **instance_tools}
        excluded = get_client_provider(client).excluded_tool_names()
        if required_capabilities:
            # Ver agent_core/capability_broker.py: SOLO desbloquea
            # herramientas normalmente excluidas por _MULTIMEDIA_TOOL_NAMES
            # (client="vscode"). La intersección es la barrera de
            # seguridad clave — para client="web", `excluded` es
            # _VSCODE_ONLY_TOOL_NAMES (disjunto de _MULTIMEDIA_TOOL_NAMES
            # por construcción), así que esto nunca desbloquea
            # propose_project_files/read_workspace_file ahí, sin
            # necesidad de comparar `client` de nuevo acá.
            unlocked = capability_broker.tool_names_for(required_capabilities) & _MULTIMEDIA_TOOL_NAMES
            excluded = excluded - unlocked
        merged = {name: tool for name, tool in merged.items() if name not in excluded}
        return {name: _agent_tool_from_tool(name, tool) for name, tool in merged.items()}

    def _extract_fallback_tool_call(self, content: str, tools: dict[str, AgentTool]) -> ToolCall | None:
        """
        Detecta un tool call imitado como texto plano/JSON cuando el
        modelo no completó message.tool_calls nativo.

        BUG REAL ENCONTRADO EN PRUEBAS: no todos los modelos servidos
        por Ollama completan message.tool_calls (el campo
        estructurado), aunque se les pase el parámetro `tools` —
        depende de si la plantilla de chat de ESE modelo en Ollama
        soporta tool-calling nativo. Confirmado con qwen2.5-coder:14b:
        en vez de tool_calls estructurado, el modelo imita el formato
        como texto plano en `content` (a veces envuelto en
        ```json ... ```). Sin este fallback, ese texto se mostraba tal
        cual al usuario como si fuera la respuesta final, sin ejecutar
        nada.

        Solo se acepta si el JSON tiene "name" con un nombre de
        herramienta que existe — así un JSON cualquiera que el modelo
        mencione al pasar no se confunde con un tool call.
        """
        data = extract_json_object(content)
        if data is not None and data.get("name") in tools:
            arguments = data.get("arguments", {})
            if isinstance(arguments, dict):
                return ToolCall(name=data["name"], arguments=arguments)

        # BUG REAL ENCONTRADO EN USO: para propose_project_files en
        # particular, el modelo a veces "imita" la llamada como un
        # array JSON crudo de archivos — ni siquiera envuelto en
        # {"name", "arguments"} como el resto de los tool calls
        # imitados que sí detecta el chequeo de arriba. Sin esto, ese
        # texto se mostraba tal cual al usuario (con los saltos de
        # línea de cada archivo escapados como "\n" literal, pareciendo
        # "todo el código en una sola línea") en vez de crear la
        # propuesta de verdad.
        if "propose_project_files" in tools:
            files = extract_json_array(content)
            if files and all(
                isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("content"), str)
                for f in files
            ):
                return ToolCall(name="propose_project_files", arguments={"files": files})

        return None

    @staticmethod
    def _looks_like_a_failed_tool_call_attempt(content: str, tools: dict[str, AgentTool] | None = None) -> bool:
        """
        BUG REAL ENCONTRADO EN USO (2026-07-21): un saludo simple ("Hola,
        ¿cómo estás?") hizo que el modelo devolviera el texto literal
        '{"name": null, "arguments": {}}' como respuesta final —
        _extract_fallback_tool_call ya rechaza esto correctamente (no es
        ninguna herramienta real), pero antes de este fix el texto
        rechazado se mostraba tal cual al usuario en vez de pedirle al
        modelo una respuesta de verdad.

        Deliberadamente ANGOSTO: solo dispara con "name" vacío/None
        (un intento de tool call a medio completar, sin haber elegido
        ninguna herramienta todavía) — NO con cualquier nombre presente,
        aunque no exista (ver test_json_with_unknown_tool_name_is_not_
        treated_as_tool_call: un JSON que MENCIONA un nombre de
        herramienta que no existe se sigue tratando como respuesta final
        real, no como un intento fallido — mismo criterio ya establecido
        para evitar falsos positivos).

        BUG REAL ENCONTRADO EN USO (2026-07-25, VS Code): "el tiburón en
        esta imagen tiene dos colas y no tiene cabeza, corregilo" hizo
        que el modelo devolviera prosa + un bloque ```json``` con
        "name": "image_editing" real, pero con JSON INVÁLIDO de verdad
        (un comentario "// estimado a ciegas" y un placeholder
        `[x1, y1, x2, y2]` sin comillas en vez de números) — ni
        siquiera parsea como dict, así que el chequeo de arriba
        (`data.get("name")` falsy) nunca lo detectaba, y ese texto
        crudo se mostraba tal cual como si fuera la respuesta final
        real, sin ejecutar ninguna herramienta. Detecta la FORMA de un
        intento de tool call (nombre de una herramienta REAL junto a
        "arguments") por regex, sin depender de que el JSON en sí
        parsee — mismo criterio de "nombre real" que arriba para no
        confundir esto con prosa legítima que solo MENCIONA JSON.
        """
        data = extract_json_object(content)
        if isinstance(data, dict) and "name" in data and not data.get("name"):
            return True
        if tools is not None and '"arguments"' in content:
            match = re.search(r'"name"\s*:\s*"(\w+)"', content)
            if match is not None and match.group(1) in tools:
                return True
        return False

    # --- Loop principal ---

    def run(
        self,
        goal: str,
        model: str | None = None,
        max_steps: int | None = None,
        max_tool_repeats: int | None = None,
        history: list[dict] | None = None,
        session_context: dict | None = None,
        denied_permissions: frozenset[Permission] = frozenset(),
        client: str | None = None,
        required_capabilities: list[str] | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> AgentRunResult:
        """
        `history` (turnos previos de la misma sesión, ver
        agent_core/sessions.py) y `session_context` (p.ej. el artefacto
        activo) son opcionales — sin ellos, el comportamiento es
        exactamente el de antes (conversación nueva de cero).
        `denied_permissions`: override de la cascada de permisos para
        ESTA sesión (ver sdk/permissions.py::PermissionCascade
        y agent_core/sessions.py::Session.denied_permissions) — vacío por
        defecto, no restringe nada más de lo que ya restringen el techo
        global y el nivel de confianza de cada herramienta.
        `max_tool_repeats`: tope estructural a cuántas veces se puede
        llamar a la MISMA herramienta dentro de este run() — ver
        settings.llm.max_tool_repeats. BUG REAL ENCONTRADO EN USO:
        "genera una raqueta de tenis" generó la imagen correcta una vez
        y después, en el mismo turno, 3 imágenes más de paisajes sin
        relación, sin llegar nunca a una respuesta final. El modelo
        nunca ve el resultado visual de una generación (la observación
        es solo la ruta del archivo, ver _artifact_to_observation) — no
        estaba "reintentando por mala calidad", perdió el hilo de la
        tarea. La regla de SYSTEM_PROMPT sola no alcanzó (ya estaba
        activa cuando pasó esto) — este tope es la barrera estructural
        que no depende de que el modelo la respete.
        `client`: "vscode" excluye del toolset las herramientas de
        generación/edición multimedia (ver _MULTIMEDIA_TOOL_NAMES más
        arriba) — mismo criterio que max_tool_repeats: una restricción
        ESTRUCTURAL (el modelo ni siquiera ve estas herramientas en la
        lista disponible), no una instrucción de prompt. Ya se probó en
        vivo que pedirle por prompt que no las llame no alcanza.
        `required_capabilities`: lista calculada por el Conversation
        Engine para ESTE turno puntual (ver agent_core/
        conversation_engine.py::ConversationEngineResult) — desbloquea,
        vía agent_core/capability_broker.py, SOLO las herramientas
        multimedia que este pedido puntual señaló como necesarias
        (p.ej. una página web en VS Code que además necesita generar
        una imagen). None/vacío (Conversation Engine deshabilitado o
        sin señal) preserva el comportamiento actual sin cambios — la
        restricción por `client` sigue siendo la misma de siempre.
        `on_step`: callback opcional, invocado con CADA `AgentStep` a
        medida que ocurre (exitoso, con error real, o rechazado por el
        tope de repeticiones) — pensado para que agent_core/routers/
        chat.py arme un recorrido en vivo consultable vía polling
        (GET /chat/progress/{id}) mientras este run() todavía está en
        curso. None (default) preserva el comportamiento actual sin
        cambios.
        """
        max_steps = max_steps or settings.llm.max_agent_steps
        max_tool_repeats = max_tool_repeats or settings.llm.max_tool_repeats
        tools = self._current_tools(client, required_capabilities)
        # BUG REAL ENCONTRADO EN USO: session_context como un SEGUNDO
        # mensaje role="system" separado (en vez de fundido en el
        # primero) hacía que qwen3-coder:30b lo ignorara por completo —
        # confirmado con una prueba directa contra Ollama: con dos
        # mensajes system, el modelo negaba tener cualquier artefacto
        # activo aunque la info estuviera ahí; fundiendo el contexto en
        # el ÚNICO mensaje system, lo usó correctamente. El historial
        # (roles user/assistant) sí funciona bien como mensajes propios
        # — el problema es específico de un segundo system.
        system_content = SYSTEM_PROMPT
        if session_context:
            system_content = f"{SYSTEM_PROMPT}\n\n{session_context['content']}"
        messages: list[dict] = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": goal})
        steps: list[AgentStep] = []
        # Ver agent_core/llm/tool_repeat_limiter.py y
        # agent_core/llm/self_check_tracker.py para la historia completa
        # (con los bugs reales en uso que motivaron cada mecanismo) de
        # estos dos rastreadores por turno.
        limiter = ToolRepeatLimiter(max_tool_repeats)
        self_check = SelfCheckTracker()

        for _ in range(max_steps):
            # GAP ESTRUCTURAL IDENTIFICADO (revisión de diseño,
            # 2026-07-30): antes, `tool_schemas` se calculaba UNA sola
            # vez antes de este loop y se mandaba sin cambios en cada
            # paso — una herramienta que ya superó su tope de
            # repeticiones (o, para las VS-Code-only, que ya tuvo éxito
            # una vez) seguía apareciendo como opción, y el único freno
            # era el mensaje de ERROR después de que el modelo insistía
            # (barrera de honor, no física). Recalcularlo acá, en cada
            # paso, según el estado real de `limiter`/`self_check`,
            # saca la herramienta de lo que el modelo puede volver a
            # elegir — el ERROR post-rechazo sigue existiendo como red
            # de seguridad para el fallback de texto plano (que no
            # depende del schema nativo) y para varias llamadas a la
            # misma herramienta dentro de UN mismo paso.
            tool_schemas = [
                t.to_ollama_schema()
                for name, t in tools.items()
                if not limiter.is_blocked(name, self_check.is_checked(name))
            ]
            try:
                response = self.llm.chat(messages, model=model, tools=tool_schemas, temperature=settings.llm.temperature)
            except ProviderError as e:
                logger.error(f"Error llamando al proveedor de LLM: {e}")
                return AgentRunResult(
                    goal=goal, final_answer=str(e), steps=steps, status="llm_error",
                    self_checked_tools=self_check.as_frozenset(),
                )

            effective_tool_calls = list(response.tool_calls)
            if not effective_tool_calls:
                fallback = self._extract_fallback_tool_call(response.content, tools)
                if fallback is not None:
                    logger.info(f"Tool call detectado como texto plano (fallback, modelo sin tool-calling nativo): {fallback.name}")
                    effective_tool_calls = [fallback]

            if not effective_tool_calls:
                if self._looks_like_a_failed_tool_call_attempt(response.content, tools):
                    # Ver _looks_like_a_failed_tool_call_attempt: nunca
                    # mostrarle al usuario un intento de tool call
                    # rechazado como si fuera la respuesta final — se lo
                    # devolvemos al modelo como un error, mismo patrón
                    # que el resto de los rechazos de este loop.
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "ERROR: eso no es ninguna herramienta válida ni una respuesta real. Si querés "
                                "usar una herramienta, elegí una de las disponibles con su nombre exacto. Si "
                                "no, respondé directamente en texto natural — nunca un JSON ni un bloque "
                                "imitando una llamada a herramienta."
                            ),
                        }
                    )
                    continue
                if not response.content.strip():
                    # BUG REAL ENCONTRADO EN USO (2026-07-28, probando qwen3.5):
                    # algunos modelos separan su razonamiento en un campo
                    # "thinking" (fuera de ChatResponse, ver ollama_client.py)
                    # y a veces dejan `content` vacío Y sin tool_calls tras un
                    # paso exitoso (confirmado con qwen3.5:9b-q4_K_M tras un
                    # run_code exitoso: content='', tool_calls=[]) — sin este
                    # chequeo, esa respuesta vacía se aceptaba tal cual como
                    # "respuesta final", dejando al usuario sin absolutamente
                    # nada pese a que la tarea real ya se había completado.
                    # Mismo patrón de retry-con-corrección que el resto de
                    # este loop, nunca aceptar en silencio una respuesta vacía.
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "ERROR: tu respuesta llegó vacía, sin texto ni ninguna llamada a herramienta. "
                                "Da tu respuesta final ahora, con texto real explicando lo que ya se hizo o "
                                "respondiendo el pedido."
                            ),
                        }
                    )
                    continue
                return AgentRunResult(
                    goal=goal, final_answer=response.content, steps=steps, status="success",
                    self_checked_tools=self_check.as_frozenset(),
                )

            # BUG REAL ENCONTRADO EN USO: el formato OpenAI (que Groq valida
            # ESTRICTO, a diferencia de Ollama que es tolerante) exige un
            # 'id' único por tool_call — para correlacionar la respuesta de
            # la herramienta (mensaje role="tool", tool_call_id) con la
            # llamada que la originó. Ollama no siempre lo devuelve, y el
            # fallback de texto plano tampoco tiene uno — se genera acá si
            # falta, nunca se manda un tool_call sin id hacia un proveedor
            # que sí lo exige.
            for tc in effective_tool_calls:
                if tc.id is None:
                    tc.id = f"call_{uuid4().hex[:24]}"

            # `arguments` queda como el objeto ya parseado (dict) — formato
            # CANÓNICO interno, el mismo que espera el /api/chat nativo de
            # Ollama. BUG REAL ENCONTRADO EN USO (2026-07-19): esto antes
            # serializaba `arguments` a un string JSON acá mismo (para
            # satisfacer a Groq, ver más abajo) — pero Ollama nativo rechaza
            # ESE formato con 400 ("Value looks like object, but can't find
            # closing '}' symbol") en cualquier turno posterior a una llamada
            # a herramienta, rompiendo TODA conversación de más de un paso
            # contra Ollama (confirmado en vivo, incluso con "hola" simple:
            # un solo tool call ya alcanza para el segundo turno). La
            # necesidad real de un string (Groq/OpenAI-strict) es específica
            # de ESE proveedor — se serializa dentro de
            # OpenAICompatibleClient.chat(), no acá (el núcleo del loop no
            # debe conocer el wire format de un proveedor concreto, ver
            # agent_core/llm/provider.py).
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in effective_tool_calls
                    ],
                }
            )

            for tool_call in effective_tool_calls:
                artifact = None
                is_self_checked = self_check.is_checked(tool_call.name)
                over_limit, effective_limit = limiter.evaluate(tool_call.name, is_self_checked)
                if over_limit:
                    # Rechazado ANTES de ejecutar — cada llamada real a una
                    # herramienta de generación cuesta minutos de cómputo acá,
                    # no tiene sentido gastarlos en una repetición que ya
                    # sabemos que vamos a cortar.
                    if is_self_checked:
                        self_check.record_rejection(tool_call.name)
                        observation = (
                            f"ERROR: ya generaste con '{tool_call.name}' {effective_limit} veces en este turno "
                            "(incluido un reintento después de revisarlo con analyze_image) — no lo intentes de "
                            "nuevo. Da tu respuesta final AHORA con la última versión que generaste. Si todavía "
                            "no coincide exactamente con lo pedido (p.ej. una cantidad exacta de algo), decilo "
                            "honestamente en tu respuesta — los modelos de generación de imágenes no siempre "
                            "respetan cantidades exactas, reintentar más no lo garantiza."
                        )
                    elif tool_call.name in _VSCODE_ONLY_TOOL_NAMES:
                        if limiter.already_succeeded(tool_call.name):
                            # BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): el
                            # modelo llamó a propose_project_files 3 veces en el
                            # mismo turno (revisando su propio intento anterior,
                            # sin ninguna señal real de que hiciera falta) — la
                            # extensión mostraba solo UNA propuesta al usuario
                            # (antes de otro fix, ni siquiera la última), así que
                            # las llamadas de más eran cómputo puro perdido.
                            observation = (
                                f"ERROR: ya llamaste a '{tool_call.name}' en este turno — no la llames de nuevo. "
                                "El usuario todavía no vio ni decidió sobre esa propuesta (su revisión ocurre "
                                "DESPUÉS de tu respuesta, nunca en este mismo turno), así que no tenés ninguna "
                                "información nueva que justifique proponer de nuevo. Da tu respuesta final ahora, "
                                "describiendo lo que ya propusiste."
                            )
                        else:
                            observation = (
                                f"ERROR: ya intentaste '{tool_call.name}' {effective_limit} veces en este turno sin "
                                "éxito — no lo intentes de nuevo. Si hay otra herramienta más apropiada para lo que "
                                "el usuario pidió, usá esa en su lugar; si no, respondé explicando la limitación."
                            )
                    else:
                        observation = (
                            f"ERROR: ya llamaste a '{tool_call.name}' {effective_limit} veces en este turno — "
                            "no la llames de nuevo. Da tu respuesta final ahora con lo que ya generaste/obtuviste."
                        )
                    logger.warning(f"Tope de repeticiones excedido para '{tool_call.name}' (límite={effective_limit}), rechazado sin ejecutar")
                else:
                    observation = self._dispatch_tool(tool_call.name, tool_call.arguments, tools, denied_permissions)
                    dispatched_tool = tools.get(tool_call.name)
                    artifact = dispatched_tool.last_artifact if dispatched_tool is not None else None
                    self_check.record_artifact(artifact, tool_call.name)
                    limiter.record_outcome(tool_call.name, observation)
                    self_check.note_check_if_applicable(tool_call.name, tool_call.arguments)
                new_step = AgentStep(
                    tool_name=tool_call.name, arguments=tool_call.arguments,
                    observation=observation, artifact=artifact,
                )
                steps.append(new_step)
                if on_step is not None:
                    on_step(new_step)
                messages.append({"role": "tool", "content": observation, "tool_call_id": tool_call.id})

                if self_check.should_cut_turn(tool_call.name):
                    logger.warning(
                        f"'{tool_call.name}' fue rechazada 2 veces por el tope de autochequeo y el modelo "
                        "insistió igual — se corta el turno con una respuesta sintetizada en vez de seguir "
                        "gastando pasos reales."
                    )
                    return AgentRunResult(
                        goal=goal, final_answer=self_check.build_cut_short_final_answer(tool_call.name),
                        steps=steps, status="success", self_checked_tools=self_check.as_frozenset(),
                    )

        logger.warning(f"Agente agotó max_steps={max_steps} sin respuesta final para: {goal!r}")
        return AgentRunResult(
            goal=goal,
            final_answer="No llegué a una respuesta final dentro del límite de pasos permitido.",
            steps=steps,
            status="max_steps_exceeded",
            self_checked_tools=self_check.as_frozenset(),
        )

    def answer_directly(
        self,
        goal: str,
        llm_client: LLMProvider | None = None,
        model: str | None = None,
        history: list[dict] | None = None,
        session_context: dict | None = None,
    ) -> str:
        """
        Una única llamada al LLM SIN pasar `tools` — estructuralmente
        imposible que el modelo llame cualquier herramienta (no hay
        ninguna declarada), a diferencia de confiar en que respete la
        instrucción de SYSTEM_PROMPT ("no llames ninguna herramienta
        para un saludo"), que ya se probó NO confiable dos veces (ver
        agent_core/tool_need_classifier.py, kal-in issue #4).

        Usada por agent_core/routers/chat.py cuando
        tool_need_classifier.predict_needs_tool() predice, con alta
        confianza, que el mensaje no necesita ninguna herramienta —
        pero NO es un mensaje exacto de get_trivial_reply(), así que no
        hay una respuesta enlatada posible. No pasa por el loop de
        pasos/self-check/tool-repeat-limiter de run() (irrelevantes
        acá: no hay herramientas que llamar, así que no hay nada que
        limitar ni auto-chequear).

        `llm_client`/`model`: chat.py pasa explícitamente
        orchestrator.conversation_engine.llm_client (el cliente PROPIO
        del Conversation Engine, SIEMPRE local por diseño — ver
        agent_core/conversation_engine.py::_build_default_client) +
        settings.tool_need_classifier.answer_model — un modelo chico
        PROPIO para este rol, distinto de conversation_engine.model
        (evaluado empíricamente 2026-09-22 específicamente para
        conversación sin herramientas, no para el JSON estructurado de
        classify()). Nunca self.llm con el nombre de modelo chico
        pasado por encima: self.llm apunta a donde diga
        settings.llm.base_url/provider, que podría ser un proveedor en
        la nube sin ese modelo. Default a self.llm si no se pasa nada,
        preservando el comportamiento previo para cualquier otro
        llamador.

        La única debilidad conocida de ese modelo chico (llamar una
        herramienta de más — ver
        technical_model_calls_unnecessary_tool_for_simple_messages)
        queda estructuralmente imposible acá, porque nunca se le ofrece
        ninguna herramienta para llamar.
        """
        system_content = SYSTEM_PROMPT
        if session_context:
            system_content = f"{SYSTEM_PROMPT}\n\n{session_context['content']}"
        messages: list[dict] = [{"role": "system", "content": system_content}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": goal})
        client = llm_client or self.llm
        return client.chat(messages, model=model).content

    def _dispatch_tool(
        self, name: str, arguments: dict[str, Any], tools: dict[str, AgentTool],
        denied_permissions: frozenset[Permission] = frozenset(),
    ) -> str:
        tool = tools.get(name)
        if tool is None:
            return f"ERROR: herramienta '{name}' no existe"

        # Cascada de permisos (ver sdk/permissions.py::
        # PermissionCascade): se resetea last_artifact ANTES del chequeo
        # para que un rechazo nunca deje pasar un artefacto viejo de una
        # llamada anterior a esta misma herramienta en el mismo run().
        tool.last_artifact = None
        missing = permission_cascade.missing_permissions(tool.permissions, tool.trust_tier, denied_permissions)
        if missing:
            reason = (
                f"ERROR: '{name}' requiere permiso(s) no autorizados en este contexto "
                f"(nivel de confianza '{tool.trust_tier}'): {', '.join(sorted(p.value for p in missing))}"
            )
            logger.warning(reason)
            audit_log.record(
                AuditEvent(
                    event_type="permission_denied",
                    summary=f"Herramienta '{name}' rechazada por la cascada de permisos: {reason}",
                    context={
                        "tool_name": name, "trust_tier": tool.trust_tier,
                        "missing_permissions": sorted(p.value for p in missing),
                    },
                    outcome="failure",
                )
            )
            return reason

        try:
            return tool.handler(**arguments)
        except TypeError as e:
            return f"ERROR: argumentos inválidos para '{name}': {e}"
        except Exception as e:
            logger.exception(f"Fallo inesperado ejecutando herramienta '{name}'")
            return f"ERROR inesperado ejecutando '{name}': {e}"

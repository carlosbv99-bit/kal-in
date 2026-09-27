"""
Context Service: decide qué entra al próximo mensaje al LLM.

Antes, esta lógica vivía repartida en agent_core/sessions.py::Session
(history_messages()/context_message()) — devolvía TODO el historial de
la sesión sin ningún límite, y el "contexto de sesión" era solo el
artefacto activo. Los frontends (la extensión de VS Code) armaban su
propio texto de contexto del editor y lo mandaban ya concatenado
dentro de `goal` — el frontend decidía qué entraba al prompt, no el
kernel.

Diseño acordado: los frontends mandan SEÑALES CRUDAS (texto del
editor, si es selección o archivo completo, etc.) — nunca texto ya
formateado — y este servicio decide el mensaje final. Vive in-process
(mismo patrón que agent_core/memory/manager.py::MemoryManager), NO se
expone por el Kernel Bus — las skills nunca necesitan construir un
prompt de chat, eso no es algo que una skill sandboxeada haga.

Alcance de esta iteración, deliberadamente mecánico (sin ninguna
llamada a LLM todavía): ventana de "últimos N turnos" en vez de
historial completo, y fusión de artefacto activo + contexto del
editor en UN ÚNICO mensaje de sistema — nunca dos mensajes system
separados (BUG REAL ya documentado en
agent_core/llm/agent_loop.py::run(): un segundo mensaje system hacía
que qwen3-coder:30b lo ignorara por completo). Resumen automático de
sesión, memoria de proyecto persistente, navegación de símbolos y
tracking de intención quedan fuera — necesitan una llamada real a un
LLM (resumen) o análisis de código por lenguaje (símbolos), y merecen
su propia validación antes de confiarlos.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent_core.client_provider import get_client_provider
from sdk.artifacts import Artifact
from utils.config import settings


@dataclass
class EditorContextSignals:
    """Señal cruda del editor — el frontend NUNCA la formatea, solo la captura."""
    relative_path: str
    language_id: str
    text: str
    is_selection: bool
    # Pieza mínima de "Editor Context Provider" (2026-07-20, pedido
    # explícito del usuario tras un bug real: kal creó un archivo nuevo
    # en el lugar equivocado por no saber que un proyecto ya existía
    # como subcarpeta). Rutas relativas nomás — nunca contenido, eso
    # sigue siendo carísimo en tokens para mandar de más de un archivo
    # (ver read_workspace_file para leer uno puntual bajo demanda).
    # `workspace_tree`: listado (acotado) de archivos visibles en el
    # Explorer. `open_editors`: pestañas actualmente abiertas (subconjunto
    # del árbol, pero más probable que sea relevante AHORA MISMO).
    workspace_tree: list[str] = field(default_factory=list)
    open_editors: list[str] = field(default_factory=list)


@dataclass
class ContextBundle:
    """Mismo shape que ya espera agent_core/llm/agent_loop.py::run() — ese módulo no cambia."""
    history: list[dict]
    session_context: dict | None


# Tope de rutas del árbol de archivos mostradas en el prompt (ver
# EditorContextSignals.workspace_tree) — un proyecto real puede tener
# miles de archivos (node_modules, .git, etc., aunque la extensión ya
# los excluye antes de mandarlos); esto es una segunda barrera del
# lado del backend para nunca inflar el prompt sin límite, sea cual
# sea el tamaño real de lo que mande la extensión.
_MAX_WORKSPACE_TREE_PATHS_IN_PROMPT = 200


class ContextService:
    def __init__(self, max_recent_turns: int | None = None):
        self.max_recent_turns = max_recent_turns or settings.context.max_recent_turns

    def build(
        self,
        session,
        editor_context: EditorContextSignals | None = None,
        client: str | None = None,
    ) -> ContextBundle:
        history = self._windowed_history(session.turns)
        session_context = self._build_session_context(session.active_artifact, editor_context, client)
        return ContextBundle(history=history, session_context=session_context)

    def _windowed_history(self, turns: list) -> list[dict]:
        recent = turns[-self.max_recent_turns:] if self.max_recent_turns else turns
        messages: list[dict] = []
        for turn in recent:
            messages.append({"role": "user", "content": turn.goal})
            messages.append({"role": "assistant", "content": turn.final_answer})
        return messages

    def _build_session_context(
        self,
        active_artifact: Artifact | None,
        editor_context: EditorContextSignals | None,
        client: str | None = None,
    ) -> dict | None:
        parts: list[str] = []
        addendum = get_client_provider(client).system_prompt_addendum()
        if addendum:
            parts.append(addendum)
        if active_artifact is not None:
            parts.append(
                f"El último artefacto activo (generado por ti o subido por el usuario) es "
                f"{active_artifact.modality} en '{active_artifact.uri}'. Si el usuario se refiere a él "
                "sin dar más detalle — \"la imagen\"/\"el audio\"/\"el video\", pero también \"el "
                'documento"/"el archivo"/"esto"/"eso"/"lo que subí"/"lo que te pasé" o cualquier otra '
                "referencia genérica sin nombrar explícitamente otra cosa — probablemente hable de "
                "este artefacto, aunque la palabra que use no coincida literalmente con su modalidad "
                '("documento" para una imagen subida es un caso real y válido, no un motivo para '
                "decir que no tenés ningún archivo disponible)."
            )
            if active_artifact.modality == "image":
                # BUG REAL ENCONTRADO EN USO: pedido "describe esta imagen"
                # (sobre una imagen recién generada, ya anunciada como
                # artefacto activo arriba) respondió "no puedo ver o
                # analizar imágenes en este entorno" — el modelo tenía el
                # path del artefacto activo Y la herramienta analyze_image
                # disponible, pero nunca conectó una cosa con la otra,
                # cayendo en su respuesta genérica de "no tengo visión".
                # Mismo patrón que otros hallazgos de esta sesión: tener la
                # herramienta disponible no alcanza sin una instrucción
                # explícita que la conecte con la intención del usuario.
                parts.append(
                    "Si el usuario pide describir, analizar o identificar qué hay en esta imagen (o "
                    "hace una pregunta sobre su contenido visual), NUNCA respondas que no puedes ver "
                    "imágenes, y NUNCA respondas desde el tema de turnos anteriores de la conversación "
                    "como si fuera el contenido de esta imagen: llama a la herramienta analyze_image "
                    f"con image_path='{active_artifact.uri}' y question igual al pedido del usuario.\n"
                    "Si en cambio el pedido es sobre TEXTO ESCRITO que aparece dentro de la imagen — "
                    "transcribirlo/leerlo, o identificar algo (una canción, un libro, una cita) A "
                    "PARTIR de ese texto — usa extract_text_from_image en su lugar, NUNCA "
                    "analyze_image: un modelo de visión-lenguaje como el de analyze_image puede "
                    "ALUCINAR contenido que no está en la imagen (confirmado en uso: inventó una "
                    "sección de RAM completa con números de parte falsos frente a una foto de CPU-Z "
                    "que no la tenía), mientras que extract_text_from_image es un pipeline de OCR "
                    "clasificatorio que no puede inventar texto que no esté ahí.\n"
                    "En ambos casos, tu respuesta final tiene que basarse en lo que la herramienta "
                    "realmente devuelva sobre ESTA imagen, nunca en una suposición ni en lo que se "
                    "venía hablando antes."
                )
            elif active_artifact.modality == "audio":
                # Mismo patrón que el bloque de imagen de arriba, para el
                # gap equivalente con audio: un audio SUBIDO POR EL
                # USUARIO (no generado por kal-in) ahora también se
                # convierte en artefacto activo (ver agent_core/routers/
                # chat.py::upload_image) — sin esta instrucción, nada le
                # avisa al modelo que puede transcribirlo con
                # speech_to_text en vez de decir que no puede escuchar
                # audio.
                parts.append(
                    "Si el usuario pide transcribir, escuchar o saber qué dice este audio (o hace una "
                    "pregunta sobre su contenido), NUNCA respondas que no puedes escuchar audio: llama "
                    f"a la herramienta speech_to_text con audio_path='{active_artifact.uri}'. Tu "
                    "respuesta final tiene que basarse en lo que la herramienta realmente transcriba de "
                    "ESTE audio, nunca en una suposición ni en lo que se venía hablando antes."
                )
        if editor_context is not None:
            if editor_context.text:
                label = "selección" if editor_context.is_selection else "archivo completo"
                parts.append(
                    f"Contexto del editor ({label} de {editor_context.relative_path}, "
                    f"lenguaje {editor_context.language_id}):\n"
                    f"```{editor_context.language_id}\n{editor_context.text}\n```"
                )
            else:
                # BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): pedido
                # de agregar fotos a "la página de menú" con esa página
                # abierta de verdad en el editor — kal no tenía forma de
                # saberlo (la vista de chat de la barra lateral no mandaba
                # NINGÚN contexto del editor) y creó un archivo nuevo en
                # el lugar equivocado, desconectado del proyecto real. Esta
                # rama cubre un contexto LIVIANO (solo ruta, sin contenido
                # — ver vscode-extension/src/editorContext.ts::
                # captureEditorSnapshot(includeContent=false)), mandado
                # automáticamente en cada pedido de esa vista: mandar el
                # archivo COMPLETO en cada mensaje de un chat libre sería
                # carísimo en tokens sin necesidad real la mayoría de las
                # veces — alcanza con que el modelo sepa EN QUÉ ruta está
                # trabajando el usuario.
                parts.append(
                    f"El usuario tiene actualmente abierto '{editor_context.relative_path}' en su "
                    "editor (no se incluyó su contenido aquí). Si el pedido es agregar o modificar "
                    "algo de ESE archivo o del proyecto al que pertenece, usa esa ruta real como "
                    "referencia en vez de adivinar o inventar una ruta nueva."
                )
            if editor_context.workspace_tree:
                tree = editor_context.workspace_tree[:_MAX_WORKSPACE_TREE_PATHS_IN_PROMPT]
                listing = "\n".join(f"- {p}" for p in tree)
                omitted = len(editor_context.workspace_tree) - len(tree)
                if omitted > 0:
                    listing += f"\n... y {omitted} archivo(s) más (no se muestran todos)."
                parts.append(
                    "Árbol de archivos REAL visible en el Explorer del proyecto — usalo para saber "
                    "qué existe ANTES de decidir dónde crear algo nuevo. Si ya existe un proyecto/"
                    "carpeta relacionado con el pedido (p.ej. 'restaurante-web/' para un pedido sobre "
                    "el menú de un restaurante), agregá o modificá archivos AHÍ ADENTRO — nunca crees "
                    "un archivo suelto en la raíz con el mismo nombre que uno que ya existe en otra "
                    "ruta de esta lista, eso deja dos archivos desconectados en vez de uno solo real:\n"
                    f"{listing}"
                )
            if editor_context.open_editors:
                parts.append(
                    "Pestañas actualmente abiertas en el editor (más probable que sean relevantes "
                    "para este pedido puntual que el resto del árbol): "
                    + ", ".join(editor_context.open_editors)
                )

        if not parts:
            return None
        return {"role": "system", "content": "\n\n".join(parts)}

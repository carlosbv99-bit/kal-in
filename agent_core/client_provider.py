"""
CONTRATO PÚBLICO entre el núcleo del agente (context_service.py,
agent_loop.py) y "qué cliente está pidiendo esto" — mismo espíritu que
agent_core/llm/provider.py::LLMProvider, aplicado a un eje distinto: en
vez de "qué motor de lenguaje responde", acá es "qué interfaz hizo el
pedido" (hoy: la web, o la extensión de VS Code).

Antes de este archivo, `client == "vscode"` aparecía repetido en 2
lugares (context_service.py y agent_loop.py), cada uno decidiendo algo
distinto (qué instrucción de prompt agregar, qué herramientas excluir)
a partir del mismo string crudo. Este archivo es el único lugar que
sabe qué significa cada valor de `client` — los llamadores solo piden
`get_client_provider(client).algo()`, nunca comparan el string ellos
mismos.

Primer caso real de "Provider" fuera de LLMProvider (2026-07-21,
pedido explícito del usuario tras el pivote de visión hacia un
Kernel que coordina sin decidir) — deliberadamente el caso más chico
posible: solo 2 puntos de decisión existían antes de este archivo.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

# Solo se agrega cuando el pedido viene del cliente "vscode" (ver
# ChatRequest.client en agent_core/routers/chat.py) — la interfaz web
# sigue generando imagen/audio/video como comportamiento default, ya
# validado. Bug real encontrado en uso: sin esta distinción, "creá la
# página web para una panadería" generó fotos de panadería sin
# relación con el pedido de código en vez de HTML/CSS/JS.
# HISTORIA COMPLETA DE LOS BUGS REALES QUE MOTIVARON CADA REGLA DE
# _VSCODE_CLIENT_INSTRUCTION (2026-08-23: separado del texto que se le
# manda al modelo, mismo criterio que SYSTEM_PROMPT en agent_loop.py —
# ver docs/HISTORY.md, "reducir el overhead de tokens fijo por
# mensaje"). El modelo solo necesita la REGLA final, no la narrativa
# completa; esa narrativa sigue viva acá para quien lea el código.
#
# - propose_project_files vs. mostrar código en texto: sin esta
#   instrucción, el modelo seguía mostrando el código en la respuesta
#   y pidiéndole al usuario que lo copie a mano, aunque la herramienta
#   ya existía y estaba disponible.
# - Subcarpeta por proyecto: pedidos de proyectos distintos en la
#   misma conversación (una barbería, después una panadería)
#   proponían todos sus archivos SUELTOS en la raíz, mezclándose entre
#   sí y pisándose unos a otros.
# - Proyectos grandes en varios pasos: pedido de una app Android
#   completa (manifest/build.gradle/actividades/layouts en varias
#   carpetas) generó una llamada tan larga que se cortó a la mitad,
#   sin llegar a proponer nada.
# - "propuse" vs. "creé" (2026-08-23): tras propose_project_files
#   exitoso, la respuesta final dijo "creé los archivos" — el usuario
#   entendió que ya estaban guardados, cuando la herramienta SOLO
#   propone (la escritura real depende de un diálogo aparte de VS
#   Code, separado del panel de chat, fácil de no notar).
# - import_resource vs. hotlink: pedido de agregar una foto real, el
#   modelo consiguió una URL real con browser pero la puso directo en
#   un <img src="..."> en vez de llamar import_resource — un enlace
#   remoto, no una descarga real.
# - Generalización falsa de "sin acceso a internet": un dominio
#   puntual rechazado (www.google.com) generó "no tengo acceso a
#   Internet ni a servicios externos" — falso, browser sí funciona
#   sobre dominios permitidos (unsplash/pexels/pixabay).
# - Negar la capacidad de generar imágenes: "generá vos mismo las
#   imágenes" respondió "no tengo la capacidad de generar imágenes" y
#   sugirió herramientas externas — engañoso, kal sí genera imágenes,
#   la herramienta solo estaba bloqueada por el modo, no ausente.
# - Incapacidad inventada ante un pedido vago: "necesito que me
#   ayudes con algo" (sin mencionar internet) respondió "no tengo
#   acceso a internet ni a servicios externos" SIN haber intentado
#   nada — una limitación inventada de la nada.
# - android_build_and_screenshot disparándose solo (2026-08-23): "crea
#   un proyecto de agenda para android" (SIN pedir ver el progreso)
#   llamó igual a la herramienta después de propose_project_files,
#   sobre archivos que ni siquiera se habían aplicado todavía.
_VSCODE_CLIENT_INSTRUCTION = (
    "Estás actuando como agente de programación dentro de VS Code (una faceta distinta de la "
    "interfaz web de kal-in, donde SÍ corresponde generar imagen/audio/video). Aquí, si piden crear una "
    "página web, una app, un script o cualquier proyecto de código, nunca generes imagen/audio/video "
    "para ese pedido, aunque el contenido describa algo visual: aquí \"página web\" es un pedido de "
    "código, no de imágenes.\n\n"
    "IMPORTANTE: tienes disponible propose_project_files para crear archivos/carpetas REALES en el "
    "proyecto del usuario (él revisa una vista previa y decide si aplicarla, nunca se escribe nada "
    "sin su aprobación). Si el pedido implica crear uno o más archivos nuevos que el usuario se va a "
    "llevar, usa SIEMPRE propose_project_files — no te limites a mostrar el código en bloques y "
    "sugerir que lo copien. Si el proyecto tiene VARIOS archivos, llama la herramienta UNA sola vez "
    "con TODOS los archivos juntos en la lista 'files' — nunca describas algunos en texto y otros en "
    "la herramienta. Reserva responder solo con código en texto para cuando el pedido es una "
    "explicación o un fragmento de referencia, no un archivo real a crear.\n\n"
    "Si el pedido es un proyecto NUEVO y distinto de lo que ya se venía haciendo en esta "
    "conversación, pon TODOS sus archivos dentro de una subcarpeta con un nombre corto y "
    "descriptivo derivado del pedido (p.ej. 'barberia-web/index.html', nunca 'index.html' suelto en "
    "la raíz). Si en cambio el pedido es agregar o modificar algo del MISMO proyecto que ya se venía "
    "creando, o el usuario pide explícitamente una ruta/carpeta distinta, sigue esa instrucción en "
    "cambio.\n\n"
    "Si el proyecto pedido tiene MUCHOS archivos (más de 4-5, o alguno muy largo), NO intentes "
    "generarlos todos en una sola llamada — propón primero SOLO los archivos esenciales para que el "
    "proyecto compile/funcione de forma mínima, dile al usuario en tu respuesta qué archivos faltan "
    "y espera el siguiente pedido para agregarlos con otra llamada "
    "a propose_project_files.\n\n"
    "En tu respuesta final después de propose_project_files, NUNCA digas \"creé\"/\"guardé\"/"
    "\"generé\" los archivos — di \"propuse\" o \"preparé\", y agrega SIEMPRE una frase explícita "
    "como \"revisa el diálogo que apareció en VS Code para verlos y aprobarlos antes de que se "
    "guarden de verdad\". Nunca des la tarea por terminada hasta que el usuario confirme que aplicó "
    "la propuesta.\n\n"
    "Si el pedido es agregar una foto/imagen REAL (no generada por IA) que el usuario se lleve como "
    "archivo propio del proyecto: (1) usa browser con action='images' sobre una página real del "
    "sitio permitido para conseguir URLs de imagen REALES — nunca inventes una URL a ciegas; "
    "(2) llama import_resource con esa URL confirmada y un destination_path dentro de una carpeta de "
    "assets del proyecto. NUNCA pongas esa URL directamente en el HTML como <img src=\"https://...\"> "
    "ni la menciones como enlace — eso NO descarga ni guarda nada real.\n\n"
    "Si un dominio puntual no está permitido, NO significa que no haya acceso a internet en "
    "absoluto — la herramienta browser sí funciona sobre dominios reales ya permitidos (hoy "
    "incluyen unsplash.com, pexels.com y pixabay.com para fotos). Ante un dominio rechazado, "
    "reintenta con esos en vez de rendirte, y nunca le digas al usuario que no hay acceso a "
    "internet cuando lo que pasó es que ESE dominio puntual no está en la lista.\n\n"
    "kal-in SÍ genera imágenes con IA (SDXL-Turbo local) — normalmente esa herramienta no está "
    "disponible en ESTE modo, PERO si tu pedido actual además necesita una imagen (p.ej. un logo para "
    "el proyecto que estás armando), puede estar desbloqueada para este turno puntual. IMPORTANTE: "
    "antes de decir que no puedes generar una imagen, FÍJATE primero en tu lista real de herramientas "
    "disponibles AHORA MISMO — si image_generation (o image_via_kernel) aparece ahí, ÚSALA de "
    "verdad. Si no aparece, aclara que es una limitación de ESTE modo/turno (no una incapacidad "
    "general de kal-in) y ofrece la alternativa que SÍ funciona aquí: buscar fotos reales con browser e "
    "importarlas con import_resource.\n\n"
    "Si un pedido es vago o le falta información, la respuesta correcta es SIEMPRE pedir una "
    "aclaración concreta (\"¿con qué necesitas ayuda?\", \"¿qué quieres que haga exactamente?\") — "
    "nunca inventar una incapacidad SIN haber intentado ninguna herramienta. Solo menciona una "
    "limitación real DESPUÉS de haber intentado de verdad la herramienta correspondiente y haber "
    "recibido un rechazo concreto.\n\n"
    "IMPORTANTE: tienes disponible android_build_and_screenshot para cuando el usuario pida ver "
    "visualmente el progreso de una app Android real que se está construyendo — compila el "
    "proyecto, lo instala en un dispositivo conectado (USB o WiFi) y muestra una captura de "
    "pantalla real. El resultado NUNCA llega en esta misma respuesta ni puedes verlo tú: la "
    "extensión de VS Code hace el trabajo real y se lo muestra al usuario directamente en el chat en "
    "un momento posterior. Por eso, en tu respuesta final después de llamarla, di algo como "
    "\"estoy compilando el proyecto e instalándolo en tu dispositivo, en un momento vas a ver una "
    "captura real de cómo se ve\" — NUNCA digas que ya se instaló, nunca describas ni inventes cómo "
    "se ve la app, y nunca des la tarea por terminada en esta respuesta. android_build_and_screenshot "
    "NUNCA se llama automáticamente después de crear/proponer un proyecto — SOLO cuando el pedido "
    "del usuario, en ESE mismo mensaje, pide explícitamente ver, monitorear o mostrar el progreso "
    "visual/la app corriendo en un dispositivo. Crear un proyecto es una tarea completa en sí "
    "misma.\n\n"
    "IMPORTANTE: tienes disponible read_workspace_file para pedir el contenido REAL de un archivo del "
    "árbol del proyecto (ver el listado de 'Árbol de archivos' de esta conversación) que no esté ya "
    "incluido aquí — nunca inventes o asumas qué contiene un archivo que no viste. Llámala con la ruta "
    "relativa exacta (tomada del árbol, nunca adivinada) y espera: el contenido real te va a llegar "
    "automáticamente en un paso siguiente de este mismo turno. Úsala quirúrgicamente — para ENTENDER "
    "un archivo puntual antes de modificarlo o antes de responder una pregunta sobre él, no para leer "
    "todo el árbol de una vez."
)

# Herramientas de generación/edición multimedia, excluidas del toolset
# cuando client="vscode" — restricción ESTRUCTURAL, no una instrucción
# de prompt: ya se probó en vivo que una regla de SYSTEM_PROMPT sola no
# evita que el modelo llame a estas herramientas para pedidos de código
# ("creá la página web para una panadería" generó fotos de panadería
# en vez de HTML/CSS/JS, dos veces, con distintas reglas de prompt).
_MULTIMEDIA_TOOL_NAMES = frozenset({
    "image_generation", "audio_generation", "video_composition",
    "image_editing", "image_composition", "speech_to_text",
    "image_via_kernel", "audio_via_kernel",
    "voice_roundtrip_via_kernel", "image_inpaint_via_kernel",
})

# Inverso del conjunto de arriba: herramientas que SOLO tienen sentido
# para client="vscode" — el backend nunca toca el filesystem real del
# usuario él mismo (ver tool_integration/adapters/vscode_files.py):
# propose_project_files/import_resource escriben recién del lado de la
# extensión, tras la aprobación del usuario; read_workspace_file lee
# recién del lado de la extensión, que encadena la respuesta
# automáticamente (ver vscode-extension/src/readWorkspaceFile.ts). El
# cliente web no tiene ningún workspace real que leer ni ningún canal
# para aplicar una propuesta de escritura, así que ofrecerle cualquiera
# de las tres solo generaría una respuesta que nadie puede usar.
#
# También se usa en agent_core/llm/agent_loop.py para el tope de
# repeticiones por turno de estas 4 herramientas específicamente — ESE
# uso es una propiedad intrínseca de las herramientas (piden aprobación
# async, una segunda llamada en el mismo turno nunca tiene información
# nueva), no depende de cuál sea el `client` activo, así que no pasa
# por ClientProvider — solo importa esta misma constante.
# android_build_and_screenshot (ver tool_integration/adapters/
# vscode_android.py) sigue el mismo criterio: compilar/instalar/
# capturar es asincrónico del lado de la extensión, una segunda
# llamada en el mismo turno (antes de que el usuario vea el resultado
# de la primera) nunca tiene información nueva tampoco.
_VSCODE_ONLY_TOOL_NAMES = frozenset(
    {"propose_project_files", "import_resource", "read_workspace_file", "android_build_and_screenshot"}
)


@runtime_checkable
class ClientProvider(Protocol):
    """
    Toda interfaz que consume el agente (hoy: web, VS Code) implementa
    esta forma (conformidad estructural, como LLMProvider). El núcleo
    (context_service.py, agent_loop.py) solo llama estos 2 métodos —
    nunca vuelve a comparar `client == "algo"` él mismo.
    """

    def system_prompt_addendum(self) -> str | None: ...

    def excluded_tool_names(self) -> frozenset[str]: ...


class VSCodeClientProvider:
    def system_prompt_addendum(self) -> str | None:
        return _VSCODE_CLIENT_INSTRUCTION

    def excluded_tool_names(self) -> frozenset[str]:
        return _MULTIMEDIA_TOOL_NAMES


class WebClientProvider:
    def system_prompt_addendum(self) -> str | None:
        return None

    def excluded_tool_names(self) -> frozenset[str]:
        return _VSCODE_ONLY_TOOL_NAMES


def get_client_provider(client: str | None) -> ClientProvider:
    """Único lugar del código que compara `client` contra un string literal."""
    return VSCodeClientProvider() if client == "vscode" else WebClientProvider()

"""
Tests de agent_core/context_service.py — decide qué entra al próximo
mensaje al LLM (ventana de turnos + fusión de artefacto activo y
contexto del editor en un único mensaje de sistema).
"""
from __future__ import annotations

from agent_core.context_service import ContextService, EditorContextSignals
from agent_core.sessions import SessionManager
from sdk.artifacts import Artifact


def _session_with_turns(n: int):
    manager = SessionManager()
    session = manager.get_or_create(None)
    for i in range(n):
        manager.record_turn(session, goal=f"pedido {i}", final_answer=f"respuesta {i}")
    return session


def test_history_includes_all_turns_when_under_the_limit():
    service = ContextService(max_recent_turns=8)
    session = _session_with_turns(3)

    bundle = service.build(session)

    assert len(bundle.history) == 6  # 3 turnos * (user + assistant)
    assert bundle.history[0] == {"role": "user", "content": "pedido 0"}


def test_history_window_keeps_only_the_last_n_turns():
    service = ContextService(max_recent_turns=2)
    session = _session_with_turns(5)

    bundle = service.build(session)

    assert len(bundle.history) == 4  # solo los últimos 2 turnos
    assert bundle.history[0] == {"role": "user", "content": "pedido 3"}
    assert bundle.history[-1] == {"role": "assistant", "content": "respuesta 4"}


def test_default_max_recent_turns_comes_from_settings():
    from utils.config import settings

    service = ContextService()
    assert service.max_recent_turns == settings.context.max_recent_turns


def test_session_context_is_none_without_artifact_or_editor_context():
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session)

    assert bundle.session_context is None


def test_session_context_describes_the_active_artifact():
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="data/artifacts/images/logo.png"))

    bundle = service.build(session)

    assert bundle.session_context["role"] == "system"
    assert "image" in bundle.session_context["content"]
    assert "data/artifacts/images/logo.png" in bundle.session_context["content"]


def test_session_context_covers_generic_references_beyond_the_word_imagen():
    """
    BUG REAL ENCONTRADO EN USO (2026-09-22): "de que se trata este
    documento" (sobre una imagen subida por el usuario, ya artefacto
    activo) hizo que el modelo respondiera "necesito verlo o leerlo
    primero... ¿podrías indicarme cómo puedo acceder a él?" — cero
    llamadas a herramientas, como si no hubiera ningún archivo
    disponible. La instrucción solo cubría "la imagen"/"el audio"/"el
    video" literalmente; "documento" no calzaba con ninguna, así que el
    modelo no conectó la referencia genérica del usuario con el
    artefacto activo real.
    """
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="data/artifacts/images/foto.png"))

    bundle = service.build(session)
    content = bundle.session_context["content"]

    assert "el documento" in content
    assert "el archivo" in content
    assert "lo que subí" in content


def test_session_context_tells_the_model_to_call_analyze_image_for_an_active_image():
    """
    BUG REAL ENCONTRADO EN USO: pedido "describe esta imagen" sobre una
    imagen recién generada (ya anunciada como artefacto activo) devolvió
    "no puedo ver o analizar imágenes en este entorno" — el modelo tenía
    disponibles el path del artefacto activo y la herramienta
    analyze_image, pero nunca conectó una cosa con la otra.
    """
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="data/artifacts/images/leon.png"))

    bundle = service.build(session)

    assert "analyze_image" in bundle.session_context["content"]
    assert "data/artifacts/images/leon.png" in bundle.session_context["content"]
    assert "no puedes ver imágenes" in bundle.session_context["content"]


def test_session_context_tells_the_model_to_use_ocr_instead_of_analyze_image_for_text_requests():
    """
    BUG REAL ENCONTRADO EN USO (2026-09-22): pedirle a analyze_image (un
    modelo de visión-lenguaje) transcribir texto denso de una imagen
    (una foto de CPU-Z) hizo que alucinara una sección de RAM completa
    con números de parte falsos que no existen en la imagen. Para
    pedidos de TEXTO, la instrucción tiene que dirigir a
    extract_text_from_image (un pipeline de OCR clasificatorio, no
    generativo) en vez de analyze_image.
    """
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="data/artifacts/images/letras.png"))

    bundle = service.build(session)
    content = bundle.session_context["content"]

    assert "extract_text_from_image" in content
    assert "NUNCA analyze_image" in content
    assert "ALUCINAR" in content


def test_session_context_does_not_mention_analyze_image_for_non_image_artifacts():
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="audio", uri="data/artifacts/audio/voz.wav"))

    bundle = service.build(session)

    assert "analyze_image" not in bundle.session_context["content"]


def test_session_context_tells_the_model_to_call_speech_to_text_for_an_active_audio():
    """
    BUG REAL ENCONTRADO EN USO (2026-09-22): un audio SUBIDO POR EL
    USUARIO (a diferencia de uno generado por kal-in) ahora también se
    convierte en artefacto activo (ver agent_core/routers/chat.py::
    upload_image, que antes solo aceptaba imágenes) — mismo patrón que
    ya se arregló para analyze_image: la herramienta speech_to_text
    disponible no alcanza sin una instrucción que la conecte con la
    intención del usuario.
    """
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="audio", uri="data/artifacts/uploads/nota.wav"))

    bundle = service.build(session)
    content = bundle.session_context["content"]

    assert "speech_to_text" in content
    assert "data/artifacts/uploads/nota.wav" in content
    assert "no puedes escuchar audio" in content


def test_session_context_describes_editor_context():
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="src/foo.py", language_id="python", text="def foo():\n    pass\n", is_selection=True,
    )

    bundle = service.build(session, editor_context)

    assert bundle.session_context["role"] == "system"
    assert "selección de src/foo.py" in bundle.session_context["content"]
    assert "lenguaje python" in bundle.session_context["content"]
    assert "```python\ndef foo" in bundle.session_context["content"]


def test_editor_context_labels_full_file_when_not_a_selection():
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="src/bar.ts", language_id="typescript", text="export const x = 1;", is_selection=False,
    )

    bundle = service.build(session, editor_context)

    assert "archivo completo de src/bar.ts" in bundle.session_context["content"]


def test_editor_context_code_block_is_closed():
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="a.js", language_id="javascript", text="console.log(1);", is_selection=False,
    )

    bundle = service.build(session, editor_context)

    assert bundle.session_context["content"].rstrip().endswith("```")


def test_editor_context_without_text_mentions_only_the_path_no_empty_code_block():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): la vista de chat
    de la barra lateral manda un contexto LIVIANO (solo ruta, sin
    contenido — ver vscode-extension/src/editorContext.ts::
    captureEditorSnapshot(includeContent=false)) automáticamente en
    cada pedido, para que kal sepa en qué archivo/proyecto está
    trabajando el usuario sin pagar el costo en tokens de mandar el
    archivo completo en cada mensaje de un chat libre.
    """
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="restaurante-web/menu.html", language_id="html", text="", is_selection=False,
    )

    bundle = service.build(session, editor_context)

    content = bundle.session_context["content"]
    assert "restaurante-web/menu.html" in content
    assert "```" not in content


def test_editor_context_includes_the_workspace_tree_when_present():
    """
    "Visible Tree" (2026-07-20, pedido explícito del usuario): kal debe
    saber qué proyectos/carpetas ya existen ANTES de decidir dónde
    crear un archivo nuevo — evita exactamente el bug real de crear
    'menu.html' suelto en la raíz cuando 'restaurante-web/menu.html' ya
    existía.
    """
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="restaurante-web/menu.html", language_id="html", text="", is_selection=False,
        workspace_tree=["restaurante-web/index.html", "restaurante-web/menu.html", "farmacia-web/index.html"],
    )

    bundle = service.build(session, editor_context)
    content = bundle.session_context["content"]

    assert "restaurante-web/index.html" in content
    assert "farmacia-web/index.html" in content


def test_editor_context_workspace_tree_is_capped_in_the_prompt():
    """Un proyecto real puede tener miles de rutas — el prompt nunca
    debe inflarse sin límite, sea cual sea el tamaño de lo que mande
    la extensión."""
    from agent_core.context_service import _MAX_WORKSPACE_TREE_PATHS_IN_PROMPT

    service = ContextService()
    session = _session_with_turns(0)
    many_paths = [f"archivo{i}.txt" for i in range(_MAX_WORKSPACE_TREE_PATHS_IN_PROMPT + 50)]
    editor_context = EditorContextSignals(
        relative_path="a.txt", language_id="plaintext", text="", is_selection=False,
        workspace_tree=many_paths,
    )

    bundle = service.build(session, editor_context)
    content = bundle.session_context["content"]

    assert f"archivo{_MAX_WORKSPACE_TREE_PATHS_IN_PROMPT - 1}.txt" in content
    assert f"archivo{_MAX_WORKSPACE_TREE_PATHS_IN_PROMPT}.txt" not in content
    assert "50 archivo(s) más" in content


def test_editor_context_includes_open_editors_when_present():
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="restaurante-web/menu.html", language_id="html", text="", is_selection=False,
        open_editors=["restaurante-web/menu.html", "restaurante-web/estilos.css"],
    )

    bundle = service.build(session, editor_context)
    content = bundle.session_context["content"]

    assert "restaurante-web/estilos.css" in content


def test_editor_context_without_tree_or_open_editors_does_not_mention_them():
    service = ContextService()
    session = _session_with_turns(0)
    editor_context = EditorContextSignals(
        relative_path="a.txt", language_id="plaintext", text="", is_selection=False,
    )

    bundle = service.build(session, editor_context)
    content = bundle.session_context["content"]

    assert "Árbol de archivos" not in content
    assert "Pestañas actualmente abiertas" not in content


def test_vscode_client_adds_the_code_only_instruction():
    """
    Distinción real entre facetas (2026-07-11): la interfaz web sigue
    generando imagen/audio/video por default; la extensión de VS Code
    (client="vscode") necesita la instrucción explícita de responder
    con código, no con imágenes, para pedidos de "página web"/app/etc.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")

    assert bundle.session_context["role"] == "system"
    assert "agente de programación dentro de VS Code" in bundle.session_context["content"]


def test_vscode_client_instruction_tells_the_model_to_use_propose_project_files():
    """
    BUG REAL ENCONTRADO EN USO: con propose_project_files ya disponible
    en el toolset, el modelo seguía mostrando el código en texto y
    pidiéndole al usuario que lo copie a mano — tener la herramienta
    ofrecida no bastó, hizo falta instruirlo explícitamente a preferirla
    sobre responder solo en texto.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")

    assert "propose_project_files" in bundle.session_context["content"]


def test_vscode_client_instruction_tells_the_model_to_use_a_subfolder_per_distinct_project():
    """
    BUG REAL ENCONTRADO EN USO: dos pedidos de proyectos distintos en la
    misma conversación (una página para una barbería, después otra para
    una panadería) proponían archivos sueltos en la raíz del proyecto
    (index.html/estilos.css/script.js para ambos) — se mezclaban entre
    sí, sin ninguna separación.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")

    assert "subcarpeta" in bundle.session_context["content"]


def test_vscode_client_instruction_tells_the_model_to_browse_before_importing_a_real_photo():
    """
    Artifact Service (import_resource): el modelo no debe inventar una
    URL de Unsplash/Pexels a ciegas — tiene que confirmarla navegando
    primero (browser, action='images').
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")

    assert "import_resource" in bundle.session_context["content"]
    assert "action='images'" in bundle.session_context["content"]


def test_vscode_client_instruction_never_claims_no_internet_access():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): un dominio
    puntual rechazado (www.google.com) hizo que el modelo le dijera al
    usuario "no tengo acceso a Internet ni a servicios externos" — una
    generalización falsa (unsplash.com/pexels.com/pixabay.com sí
    funcionan). La instrucción ahora nombra esos dominios explícitamente
    y aclara que un rechazo puntual no es una incapacidad general.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")
    content = bundle.session_context["content"]

    assert "unsplash.com" in content
    assert "no significa que no haya acceso" in content.lower()


def test_vscode_client_instruction_clarifies_image_generation_is_mode_specific_not_a_general_incapacity():
    """
    BUG REAL ENCONTRADO EN USO: pedido de "generá vos mismo las
    imágenes" respondió "no tengo la capacidad de generar imágenes" y
    mandó al usuario a buscar herramientas externas — engañoso, kal SÍ
    genera imágenes (SDXL-Turbo local), solo que esa herramienta no
    está disponible en este modo de código. La instrucción ahora pide
    aclarar que es una limitación de ESTE modo, no una incapacidad
    general, y ofrecer browser+import_resource como alternativa real.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")
    content = bundle.session_context["content"]

    assert "SÍ genera imágenes" in content
    assert "import_resource" in content


def test_vscode_client_instruction_tells_the_model_to_check_its_real_toolset_before_refusing_image_generation():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-24): con el Capability Broker ya
    integrado (ver agent_core/capability_broker.py), un pedido de VS
    Code que SÍ necesitaba una imagen (código + logo) tenía
    image_generation realmente disponible ese turno, pero el modelo
    respondió igual "no puedo generar imágenes directamente ahora" —
    la instrucción vieja afirmaba como hecho fijo que la herramienta
    "no está disponible en ESTE modo", sin condicionarlo a si el
    Capability Broker la desbloqueó para ESE turno puntual.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")
    content = bundle.session_context["content"]

    assert "fíjate primero en tu lista real de herramientas" in content.lower()


def test_vscode_client_instruction_tells_the_model_to_ask_for_clarification_instead_of_inventing_a_limitation():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-21): pedido vago ("necesito que
    me ayudes con algo", sin mencionar internet ni ninguna herramienta)
    respondió "no tengo acceso a internet ni a servicios externos" SIN
    haber intentado ninguna herramienta — una incapacidad inventada de
    la nada. La instrucción ahora pide aclaración concreta en vez de
    inventar una limitación no probada.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")
    content = bundle.session_context["content"]

    assert "con qué necesitas ayuda" in content.lower()
    assert "sin haber intentado" in content.lower()


def test_vscode_client_instruction_tells_the_model_to_use_read_workspace_file():
    """
    Pieza mínima de "Editor Context Provider" (2026-07-20): kal no debe
    inventar o asumir el contenido de un archivo que no vio — tiene que
    pedirlo con read_workspace_file y esperar la respuesta encadenada.
    """
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client="vscode")
    content = bundle.session_context["content"]

    assert "read_workspace_file" in content
    assert "nunca inventes o asumas qué contiene" in content


def test_web_client_never_gets_the_vscode_instruction():
    service = ContextService()
    session = _session_with_turns(0)

    bundle = service.build(session, client=None)

    assert bundle.session_context is None  # sin artefacto/editor context, nada que agregar


def test_vscode_instruction_is_fused_with_artifact_and_editor_context_in_one_message():
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="logo.png"))
    editor_context = EditorContextSignals(
        relative_path="src/foo.py", language_id="python", text="x = 1", is_selection=False,
    )

    bundle = service.build(session, editor_context, client="vscode")

    assert bundle.session_context["role"] == "system"
    assert "agente de programación dentro de VS Code" in bundle.session_context["content"]
    assert "logo.png" in bundle.session_context["content"]
    assert "src/foo.py" in bundle.session_context["content"]


def test_artifact_and_editor_context_are_fused_into_a_single_system_message():
    """
    BUG REAL ya documentado en agent_core/llm/agent_loop.py::run(): un
    segundo mensaje role=system hacía que qwen3-coder:30b lo ignorara
    por completo. El Context Service debe fusionar todo en UNO solo.
    """
    service = ContextService()
    manager = SessionManager()
    session = manager.get_or_create(None)
    manager.update_active_artifact(session, Artifact(modality="image", uri="logo.png"))
    editor_context = EditorContextSignals(
        relative_path="src/foo.py", language_id="python", text="x = 1", is_selection=False,
    )

    bundle = service.build(session, editor_context)

    assert bundle.session_context["role"] == "system"
    assert "logo.png" in bundle.session_context["content"]
    assert "src/foo.py" in bundle.session_context["content"]

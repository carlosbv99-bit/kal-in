"""
Chat / agente: /chat, /uploads.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from agent_core.context_service import EditorContextSignals
from agent_core.conversation_engine import get_trivial_reply
from agent_core.llm.provider import ProviderError
from agent_core.orchestrator import _artifact_url, orchestrator
from sdk.artifacts import Artifact
from sdk.permissions import Permission
from utils.config import settings
from utils.correlation import new_id, set_correlation_id
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Chat"])


class EditorContextRequest(BaseModel):
    """
    Señal cruda del editor (ver agent_core/context_service.py) — el
    frontend (extensión de VS Code) NUNCA manda texto ya formateado
    acá, solo estos campos. El Context Service decide cómo se ve en
    el mensaje final al LLM.
    """
    relative_path: str
    language_id: str
    text: str
    is_selection: bool
    # Pieza mínima de "Editor Context Provider" (2026-07-20) — ver
    # agent_core/context_service.py::EditorContextSignals. Ambos vacíos
    # por defecto: compatibilidad con clientes viejos que todavía no
    # los mandan.
    workspace_tree: list[str] = Field(default_factory=list)
    open_editors: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    goal: str = Field(description="Mensaje del usuario / objetivo para el agente.")
    model: str | None = Field(default=None, description="Override del modelo LLM a usar. None = el default de config.yaml.")
    use_planner: bool | None = Field(default=None, description="None = usar el default de config.yaml (llm.planning_enabled).")
    session_id: str | None = Field(default=None, description="None = crea una sesión nueva (ver agent_core/sessions.py).")
    # Override de la cascada de permisos para esta sesión (ver
    # sdk/permissions.py::PermissionCascade). None = no tocar
    # lo que ya había (default); [] = limpiar cualquier restricción previa;
    # una lista = REEMPLAZA el override completo (no se acumula turno a
    # turno, para que nunca quede algo bloqueado "para siempre" sin que el
    # usuario lo vea venir).
    deny_permissions: list[str] | None = Field(
        default=None,
        description="Override de permisos para esta sesión. None = no tocar; [] = limpiar; lista = reemplaza el override completo.",
    )
    editor_context: EditorContextRequest | None = None
    # None/"web" = interfaz web (default: genera imagen/audio/video). "vscode" =
    # extensión de VS Code (ver agent_core/context_service.py::_VSCODE_CLIENT_INSTRUCTION) —
    # ahí "página web"/"app"/"script" es un pedido de código, no de imágenes.
    client: str | None = Field(default=None, description="None/'web' = interfaz web. 'vscode' = extensión de VS Code.")


def _backend_model_for_tool(tool_name: str) -> str | None:
    """
    El modelo ESPECIALIZADO que de verdad ejecuta una herramienta —
    nunca el modelo de razonamiento/tool-calling (ese ya lo cubre la
    entrada "main_model" del recorrido en vivo). Pedido explícito del
    usuario tras preguntar por qué nunca veía "llava:13b" en el panel:
    antes solo se mostraba el nombre de la herramienta, nunca qué
    modelo hay detrás. None para herramientas sin un modelo de IA
    propio (p.ej. qr_code, propose_project_files) — el frontend
    entonces no agrega nada extra.
    """
    mapping = {
        "image_generation": settings.multimodal.image.model,
        "image_via_kernel": settings.multimodal.image.model,
        "image_inpaint_via_kernel": settings.multimodal.image_editing.inpaint_model,
        "analyze_image": settings.multimodal.vision.model,
        "audio_generation": settings.multimodal.audio.voice_model,
        "audio_via_kernel": settings.multimodal.audio.voice_model,
        "voice_roundtrip_via_kernel": settings.multimodal.audio.voice_model,
        "speech_to_text": f"whisper-{settings.multimodal.stt.model_size}",
    }
    return mapping.get(tool_name)


@router.post(
    "/chat",
    summary="Punto de entrada del agente",
    description=(
        "Procesa un mensaje del usuario: el agente decide qué herramientas llamar (mediadas por "
        "el kernel de permisos/sandbox/auditoría) y devuelve una respuesta final más los pasos "
        "intermedios. Ver /chat/progress/{session_id} para seguir un turno en curso."
    ),
)
def chat(req: ChatRequest):
    # Correlation ID (ver utils/correlation.py): un identificador corto
    # que va a aparecer en cada línea de logs/agent.log y en el context
    # de cada entrada de logs/audit.log generada mientras se procesa
    # este pedido — incluida cualquier skill sandboxeada que se llame en
    # el camino. Se devuelve en la respuesta para que, ante un fallo
    # real, alcance con este valor (no hay que reconstruir la cadena a
    # mano cruzando ambos logs).
    correlation_id = new_id()
    set_correlation_id(correlation_id)
    logger.info(f"POST /chat: {req.goal!r}")

    session = orchestrator.sessions.get_or_create(req.session_id)
    use_planner = req.use_planner if req.use_planner is not None else settings.llm.planning_enabled

    if req.deny_permissions is not None:
        try:
            denied = frozenset(Permission(p) for p in req.deny_permissions)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Permiso inválido en deny_permissions: {e}")
        orchestrator.sessions.update_denied_permissions(session, denied)

    editor_context = None
    if req.editor_context is not None:
        editor_context = EditorContextSignals(
            relative_path=req.editor_context.relative_path,
            language_id=req.editor_context.language_id,
            text=req.editor_context.text,
            is_selection=req.editor_context.is_selection,
            workspace_tree=req.editor_context.workspace_tree,
            open_editors=req.editor_context.open_editors,
        )
    context_bundle = orchestrator.context_service.build(session, editor_context, client=req.client)

    # Recorrido en vivo de ESTE turno (ver GET /chat/progress/{id} más
    # abajo) — se reinicia acá, antes de arrancar cualquier trabajo
    # real, para que el frontend nunca vea restos del turno anterior
    # mientras hace polling.
    orchestrator.sessions.clear_progress(session)

    # BUG REAL ENCONTRADO EN USO (kal-in issue #4, 2026-09-14/21): un
    # saludo trivial ("hola", "¿quién sos?", etc.) resolvía el turno
    # COMPLETO contra el modelo principal (con tool-calling habilitado)
    # — is_trivial_message() antes solo evitaba la llamada al
    # Conversation Engine, no el resto del flujo. SYSTEM_PROMPT ya le
    # pide al modelo "no llamar ninguna herramienta" para estos casos,
    # pero esa instrucción ya se había probado NO confiable (el modelo a
    # veces igual llama system_info) — confirmado en dos entornos reales
    # de Likay-OS: sin red (pull de imagen Docker cuelga sin timeout) y
    # con Podman rootless (agota max_steps reintentando sin responder).
    # get_trivial_reply() corta acá, ANTES de tocar cualquier modelo —
    # ver agent_core/conversation_engine.py para la lista completa y el
    # porqué de la respuesta enlatada por mensaje.
    trivial_reply = get_trivial_reply(req.goal)
    if trivial_reply is not None:
        orchestrator.sessions.record_turn(session, req.goal, trivial_reply)
        return {
            "session_id": session.id,
            "correlation_id": correlation_id,
            "goal": req.goal,
            "final_answer": trivial_reply,
            "status": "trivial_reply",
            "plan": [],
            "steps": [],
            # Ningún modelo resolvió este turno — reflectLastModelUsed()
            # en frontend/app.js ya maneja un valor falsy sin tocar el
            # selector visible.
            "model_used": None,
        }

    # Conversation Engine (ver agent_core/conversation_engine.py): paso
    # PREVIO y opcional, "fail-open" — si detecta baja confianza (pedido
    # ambiguo), responde de inmediato con la aclaración sin correr el
    # planner/agent_loop completo. Si falla por cualquier motivo o la
    # confianza alcanza, el flujo sigue exactamente como antes de este
    # cambio.
    ce_result = orchestrator.conversation_engine.classify(req.goal)
    if ce_result is not None:
        orchestrator.sessions.append_progress(session, {
            "stage": "conversation_engine", "model": settings.conversation_engine.model,
            "intent": ce_result.intent, "confidence": ce_result.confidence,
        })
    if ce_result is not None and ce_result.confidence < settings.conversation_engine.confidence_threshold:
        orchestrator.sessions.record_turn(session, req.goal, ce_result.user_reply)
        return {
            "session_id": session.id,
            "correlation_id": correlation_id,
            "goal": req.goal,
            "final_answer": ce_result.user_reply,
            "status": "needs_clarification",
            "plan": [],
            "steps": [],
            # El modelo que resolvió ESTE turno — acá, el del Conversation
            # Engine (nunca el "cerebro" principal, ver
            # utils/config.py::ConversationEngineConfig). Usado por el
            # frontend para mostrar "Último modelo utilizado" (ver
            # frontend/app.js).
            "model_used": settings.conversation_engine.model,
        }

    main_model = req.model or settings.llm.default_model
    orchestrator.sessions.append_progress(session, {"stage": "main_model", "model": main_model})

    def _on_step(step) -> None:
        # Recorrido en vivo — se llama para CUALQUIER paso (exitoso, con
        # error real, o rechazado por el tope de repeticiones); el
        # frontend decide cómo mostrar cada caso (ver frontend/app.js).
        entry = {"stage": "tool_call", "tool": step.tool_name, "ok": not step.observation.startswith("ERROR")}
        backend_model = _backend_model_for_tool(step.tool_name)
        if backend_model is not None:
            entry["backend_model"] = backend_model
        orchestrator.sessions.append_progress(session, entry)

    try:
        result = orchestrator.planning_agent.run(
            req.goal, model=req.model, use_planner=use_planner,
            history=context_bundle.history, session_context=context_bundle.session_context,
            denied_permissions=session.denied_permissions, client=req.client,
            # Ver agent_core/capability_broker.py: desbloquea SOLO las
            # herramientas multimedia que este pedido puntual necesita
            # (p.ej. una página web en VS Code que además pide una
            # imagen) — None si el clasificador falló/está deshabilitado,
            # preservando el comportamiento actual sin cambios.
            required_capabilities=ce_result.required_capabilities if ce_result is not None else None,
            on_step=_on_step,
        )
    except ProviderError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # BUG REAL ENCONTRADO EN USO (2026-08-23): un 500 real de Ollama
    # (ver agent_core/llm/agent_loop.py — ProviderError atrapado por
    # paso, nunca propaga como excepción) deja result.status=="llm_error"
    # y result.final_answer como el TEXTO CRUDO del error HTTP. Antes,
    # eso se grababa igual como si fuera una respuesta real — el
    # próximo pedido en la MISMA sesión mandaba ese error de vuelta al
    # modelo como su propio mensaje "assistant" anterior (ver
    # context_service.py::_windowed_history). Confirmado en vivo: tras
    # dos 500 seguidos, el pedido siguiente respondió con un mensaje
    # raro tipo "Entendido, usaré propose_project_files..." en vez de
    # llamar la herramienta de una — el modelo reaccionaba a ver su
    # "propia" respuesta anterior siendo un error técnico. session.turns
    # solo se usa para construir ESE historial (ver grep), nada más
    # depende de él — no grabar un turno de error no pierde nada que el
    # usuario necesite (ya ve el error en ESTA respuesta).
    if result.status != "llm_error":
        orchestrator.sessions.record_turn(session, req.goal, result.final_answer)
    all_steps = [s for step_result in result.step_results for s in step_result.result.steps]
    for step in all_steps:
        if step.artifact is not None and step.artifact.modality != "text":
            orchestrator.sessions.update_active_artifact(session, step.artifact)
            orchestrator.sessions.record_artifact(session, step.artifact, step.tool_name)

    # BUG REAL ENCONTRADO EN USO (2026-07-24): el autochequeo de
    # image_generation (generar -> analyze_image -> regenerar UNA vez si
    # hace falta, ver agent_loop.py::self_checked_tools) llama a la
    # MISMA herramienta dos veces en un turno — el frontend mostraba las
    # DOS imágenes como si fueran dos resultados distintos, en vez de la
    # segunda (post-autochequeo) reemplazando a la primera. Deliberado
    # usar `self_checked_tools` (ver agent_core/llm/agent_loop.py) en vez
    # de comparar argumentos idénticos: el modelo a veces REFORMULA el
    # prompt al regenerar (confirmado en vivo — "un globo aerostático en
    # el cielo azul" pasó a "...con una silueta de tierra y líneas
    # costeras" en el reintento), así que dos llamadas con argumentos
    # DISTINTOS también deben colapsarse si esa herramienta está
    # marcada como autochequeada este turno. Nunca afecta a una
    # herramienta que NO se autochequeó — dos imágenes de verdad
    # distintas pedidas en el mismo turno siguen mostrándose ambas.
    all_self_checked_tools: set[str] = set()
    for step_result in result.step_results:
        all_self_checked_tools |= set(step_result.result.self_checked_tools)

    _last_index_for_self_checked_tool: dict[str, int] = {}
    for i, step in enumerate(all_steps):
        if step.tool_name in all_self_checked_tools and step.artifact is not None and step.artifact.modality == "image":
            _last_index_for_self_checked_tool[step.tool_name] = i
    superseded_step_indices = {
        i
        for i, step in enumerate(all_steps)
        if step.tool_name in all_self_checked_tools
        and step.artifact is not None
        and step.artifact.modality == "image"
        and _last_index_for_self_checked_tool[step.tool_name] != i
    }

    def _step_artifact(step, index: int) -> dict | None:
        if index in superseded_step_indices:
            return None
        if step.artifact is None:
            return None
        if step.artifact.modality == "project_files":
            # A diferencia de image/audio/video, esto no es un archivo YA
            # generado en disco (uri) — es una PROPUESTA que la extensión
            # de VS Code todavía tiene que revisar y aplicar (ver
            # vscode-extension/src/projectFiles.ts). El backend nunca
            # escribe esto al disco real del usuario.
            return {
                "modality": "project_files",
                "request_id": step.artifact.metadata.get("request_id"),
                "files": step.artifact.metadata.get("files", []),
            }
        if step.artifact.modality == "workspace_file_request":
            # ReadWorkspaceFileTool (tool_integration/adapters/vscode_files.py)
            # nunca lee el archivo real acá — el backend no tiene acceso al
            # disco de VS Code. Esto solo le avisa a la extensión qué ruta
            # pedir; ella responde encadenando un /chat nuevo con el
            # contenido real (ver vscode-extension/src/readWorkspaceFile.ts).
            return {
                "modality": "workspace_file_request",
                "request_id": step.artifact.metadata.get("request_id"),
                "path": step.artifact.metadata.get("path"),
            }
        if step.artifact.modality == "android_build_request":
            # AndroidBuildScreenshotTool (tool_integration/adapters/vscode_android.py):
            # el backend no tiene acceso a ningún dispositivo Android ni al
            # proyecto real — esto solo le avisa a la extensión que hay un
            # pedido pendiente. A diferencia de workspace_file_request, la
            # extensión NO encadena la respuesta de vuelta acá (el modelo no
            # necesita "ver" la captura) — se la muestra al usuario
            # directamente (ver vscode-extension/src/androidBuild.ts).
            return {
                "modality": "android_build_request",
                "request_id": step.artifact.metadata.get("request_id"),
            }
        if step.artifact.modality not in ("image", "document"):
            return None
        url = _artifact_url(step.artifact.uri)
        if url is None:
            return None
        result = {"modality": step.artifact.modality, "url": url, "path": step.artifact.uri}
        if step.artifact.modality == "document":
            # CreateTextFileTool (tool_integration/adapters/text_file.py):
            # el frontend necesita el nombre real para el link de
            # descarga (el nombre del archivo en disco incluye un sufijo
            # uuid que no hace falta mostrarle al usuario).
            result["filename"] = step.artifact.metadata.get("filename", Path(step.artifact.uri).name)
        return result

    return {
        "session_id": session.id,
        "correlation_id": correlation_id,
        "goal": result.goal,
        "final_answer": result.final_answer,
        "status": result.status,
        "plan": [s.description for s in result.plan.steps],
        # El modelo que de verdad resolvió este turno — misma resolución
        # que ya hace OllamaClient.chat() internamente (model or
        # settings.llm.default_model), expuesta acá para que el frontend
        # pueda mostrar "Último modelo utilizado" (ver frontend/app.js).
        "model_used": main_model,
        "steps": [
            {
                "tool": s.tool_name, "arguments": s.arguments, "observation": s.observation,
                "artifact": _step_artifact(s, i),
            }
            for i, s in enumerate(all_steps)
        ],
    }


@router.get("/chat/progress/{session_id}", summary="Progreso en vivo de un turno en curso")
def chat_progress(session_id: str):
    """
    Recorrido en vivo del turno EN CURSO de esta sesión (ver
    Session.progress en agent_core/sessions.py) — pensado para que el
    frontend haga polling (cada ~1s, ver frontend/app.js) MIENTRAS un
    /chat todavía está procesando, y así mostrar qué modelo/herramienta
    está actuando en cada momento, no solo el resultado final. Un
    session_id desconocido devuelve una lista vacía (mismo criterio de
    degradación con gracia que el resto de /chat: nunca un error duro
    por un id que no existe todavía o ya no existe).
    """
    session = orchestrator.sessions.get_or_create(session_id)
    return {"progress": session.progress}


@router.get("/chat/sessions/{session_id}/artifacts", summary="Historial de artefactos de una sesión")
def chat_session_artifacts(session_id: str):
    """
    Historial completo de artefactos (generados o subidos) de esta
    sesión — a diferencia de `active_artifact` (solo el último, ver
    Session en agent_core/sessions.py), permite responder "qué generé
    en esta sesión" más allá del artefacto más reciente. Artefactos sin
    `uri` servible (p.ej. project_files, que no vive bajo data/artifacts/)
    quedan con `url: None` — el llamador decide si mostrarlos igual.
    """
    session = orchestrator.sessions.get_or_create(session_id)
    return {
        "artifacts": [
            {
                "modality": record.artifact.modality,
                "tool_name": record.tool_name,
                "created_at": record.created_at,
                "path": record.artifact.uri,
                "url": _artifact_url(record.artifact.uri),
            }
            for record in session.artifacts
        ]
    }


# --- Subida de imágenes propias ---

_ALLOWED_UPLOAD_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp"}


@router.post("/uploads", summary="Subir una imagen propia")
async def upload_image(file: UploadFile = File(...), session_id: str | None = Form(None)):
    """
    Sube una imagen propia del usuario (no generada por kal) y la
    convierte en el artefacto activo de la sesión — así el siguiente
    mensaje ("quitale el fondo") no necesita repetir ninguna ruta.

    Acción DIRECTA del usuario (como escribir un mensaje de chat), no
    una decisión autónoma del agente — no pasa por el pipeline de
    permisos/aprobación ni se audita, mismo criterio que /chat en sí
    (ver audit/audit_log.py: solo se registran ahí acciones SIN
    intervención humana directa).
    """
    if file.content_type not in _ALLOWED_UPLOAD_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no soportado: '{file.content_type}' (solo imágenes: png/jpeg/webp)",
        )

    cfg = settings.multimodal.uploads
    upload_dir = Path(cfg.artifact_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename or "").suffix or ".png"
    dest_path = upload_dir / f"{uuid.uuid4()}{suffix}"
    max_bytes = cfg.max_size_mb * 1024 * 1024

    size = 0
    with open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                dest_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"Archivo demasiado grande (máx {cfg.max_size_mb}MB)")
            f.write(chunk)

    session = orchestrator.sessions.get_or_create(session_id)
    artifact = Artifact(
        modality="image", uri=str(dest_path),
        metadata={"uploaded_by_user": True, "original_filename": file.filename},
    )
    orchestrator.sessions.update_active_artifact(session, artifact)
    orchestrator.sessions.record_artifact(session, artifact, tool_name="upload")

    return {
        "session_id": session.id,
        "path": str(dest_path),
        "url": _artifact_url(str(dest_path)),
    }

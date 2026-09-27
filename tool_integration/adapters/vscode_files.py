"""
Herramienta para que el agente IDE de VS Code cree archivos/carpetas
REALES en el proyecto del usuario — hasta ahora, kal solo devolvía el
código como texto en la respuesta final (ver
tool_integration/adapters/core_tools.py::CodeExecutionTool, que
explícitamente prohíbe escribir archivos "que el usuario se lleve"),
dejando que el usuario copie/pegue a mano.

Límite arquitectónico real: este backend de Python NUNCA sabe qué
carpeta tiene abierta VS Code — solo la extensión lo sabe. Por eso esta
Tool NUNCA escribe nada a disco ella misma: solo propone la lista de
archivos (rutas relativas + contenido), consulta al Permission Manager
del Kernel (kernel/permissions/filesystem_access_manager.py) para dejar
auditoría y confirmar que la acción está permitida, y devuelve la
propuesta estructurada — la escritura real ocurre del lado de la
extensión (vscode.workspace.fs), tras que el usuario la apruebe en una
vista previa (ver vscode-extension/src/projectFiles.ts).
"""
from __future__ import annotations

import base64
from pathlib import PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

from audit.audit_log import AuditEvent, audit_log
from kernel.permissions.filesystem_access_manager import filesystem_access_manager
from kernel.permissions.filesystem_permissions import FilesystemAction, FilesystemScope
from kernel.permissions.network_access_manager import network_access_manager
from kernel.permissions.network_permissions import NetworkAction, NetworkScope
from sdk.artifacts import Artifact
from sdk.skill import Tool, ToolManifest
from tool_integration.download_manager import DownloadValidationError, download_manager

# Nombre estable usado como skill_name ante el Permission Manager y
# como filtro de exclusión del toolset para clientes que no son VS Code
# (ver agent_core/llm/agent_loop.py::_VSCODE_ONLY_TOOL_NAMES) — el
# cliente web no tiene forma de escribir un archivo real, ofrecerle
# esta herramienta ahí solo generaría una propuesta que nadie puede
# aplicar nunca.
VSCODE_INTEGRATION_SKILL_NAME = "vscode_integration"


class ProjectFilesRejectedError(Exception):
    """Alguno de los paths propuestos no es válido — nunca se llega a proponer nada."""


def _validate_relative_path(path: str) -> None:
    if not path or not path.strip():
        raise ProjectFilesRejectedError("Un archivo propuesto tiene una ruta vacía.")
    pure = PurePosixPath(path.replace("\\", "/"))
    if pure.is_absolute():
        raise ProjectFilesRejectedError(
            f"'{path}' es una ruta absoluta — usá siempre rutas RELATIVAS al proyecto abierto."
        )
    if ".." in pure.parts:
        raise ProjectFilesRejectedError(f"'{path}' intenta salir de la carpeta del proyecto ('..') — no permitido.")


# Extensiones de contenido binario real — nunca texto legítimo. .svg queda
# afuera a propósito: es XML de texto plano, un ícono vectorial escrito a
# mano es un uso legítimo de propose_project_files.
_BINARY_MEDIA_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a",
    ".mp4", ".webm", ".mov", ".avi", ".mkv",
    ".woff", ".woff2", ".ttf", ".otf",
    ".pdf", ".zip",
})


def _reject_if_binary_media_extension(path: str) -> None:
    """
    BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): pedido de agregar
    fotos a una página — el modelo, sin haber llamado a browser ni a
    import_resource, propuso archivos "plato1.jpg"/"plato2.jpg" cuyo
    "contenido" era literalmente el texto "https://example.com/path/to/
    plato1.jpg" — ni una imagen real ni una descarga real, un archivo
    de TEXTO con extensión de imagen que el navegador no puede
    renderizar (ícono roto en la vista previa). propose_project_files
    solo maneja contenido de TEXTO (ver 'content': string en el
    schema) — la única vía real para bytes binarios reales es
    import_resource (Artifact Service, descarga real desde una URL
    confirmada). Rechazado acá, estructural, en vez de confiar en que
    el prompt solo alcance para evitarlo (mismo criterio que el resto
    de las validaciones de este archivo).
    """
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if suffix in _BINARY_MEDIA_EXTENSIONS:
        raise ProjectFilesRejectedError(
            f"'{path}' tiene una extensión de archivo binario (imagen/audio/video/fuente/etc.) — "
            "propose_project_files solo puede escribir contenido de TEXTO, nunca bytes reales. "
            "Para un archivo de este tipo, usá import_resource con una URL real confirmada "
            "(nunca inventada) en su lugar."
        )


class ProposeProjectFilesTool(Tool):
    manifest = ToolManifest(
        name="propose_project_files",
        description=(
            "Propone crear uno o más archivos/carpetas NUEVOS en el proyecto real que el "
            "usuario tiene abierto en su editor (una página web, un proyecto con varios "
            "archivos, etc.) — a diferencia de run_code, esto SÍ termina en archivos "
            "reales que el usuario se lleva. El usuario ve una vista previa y decide si "
            "aplicarla, nunca se escribe nada sin su aprobación explícita. "
            "'path' de cada archivo tiene que ser SIEMPRE una ruta RELATIVA a la raíz del "
            "proyecto (p.ej. 'index.html', 'css/estilos.css') — nunca una ruta absoluta "
            "(como '/home/...' o 'C:\\...') ni un path con '..'; esos se rechazan. "
            "'content' es SIEMPRE texto plano — nunca uses esta herramienta para una imagen/"
            "audio/video/fuente real (.jpg/.png/.mp3/.mp4/etc.), esos se rechazan; para un "
            "archivo binario real, usá import_resource con una URL real confirmada."
        ),
        created_by="system",
        requires_filesystem_write=True,
        parameters_schema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": "Lista de archivos a proponer",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Ruta RELATIVA al proyecto, p.ej. 'index.html'"},
                            "content": {"type": "string", "description": "Contenido completo del archivo"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            "required": ["files"],
        },
    )

    def execute(self, files: list[dict], **kwargs) -> Artifact:
        if not files:
            raise ProjectFilesRejectedError("'files' es requerido, con al menos un archivo.")
        for f in files:
            _validate_relative_path(f.get("path", ""))
            _reject_if_binary_media_extension(f.get("path", ""))

        decision = filesystem_access_manager.evaluate(
            skill_name=VSCODE_INTEGRATION_SKILL_NAME,
            scope=FilesystemScope.WORKSPACE,
            action=FilesystemAction.CREATE,
            resource_key="workspace",
        )

        if decision != "auto_allowed":
            # Fail-closed: sin un canal síncrono de aprobación humana a
            # mitad de un turno de chat, no se puede simplemente esperar
            # — mismo criterio que self-modification/tools dinámicos,
            # nunca se procede como si el permiso ya estuviera dado.
            return Artifact(
                modality="project_files",
                uri="",
                metadata={"status": "requires_approval", "files": []},
            )

        return Artifact(
            modality="project_files",
            uri="",
            metadata={
                "status": "proposed",
                "request_id": str(uuid4()),
                "files": [{"path": f["path"], "content": f["content"]} for f in files],
            },
        )


class ImportResourceTool(Tool):
    """
    Artifact Service (Fase 1): descarga un recurso REAL desde una URL
    (hoy solo imágenes) y lo propone como archivo del proyecto — mismo
    flujo de vista previa/aprobación que propose_project_files (de
    hecho, el MISMO tipo de Artifact "project_files", con
    encoding="base64" para el binario). Nunca escribe nada ella misma,
    igual que ProposeProjectFilesTool — la escritura real ocurre del
    lado de la extensión.

    Genérico a propósito (`type` es un parámetro, no algo hardcodeado):
    ver tool_integration/download_manager.py — solo "image" tiene un
    validador real implementado hoy, cualquier otro valor se rechaza
    con un mensaje claro en vez de aceptar un binario sin poder
    confirmar de verdad qué es.
    """

    manifest = ToolManifest(
        name="import_resource",
        description=(
            "Descarga un recurso REAL desde una URL (hoy solo imágenes) y lo propone como "
            "archivo del proyecto — el usuario ve una vista previa y decide si aplicarla, igual "
            "que propose_project_files. La URL tiene que ser una imagen REAL de un sitio "
            "permitido (usá la herramienta browser con action='images' primero para conseguir "
            "una URL real — NUNCA inventes una URL de Unsplash/Pexels/etc. a ciegas, no existe "
            "garantía de que sea real). 'destination_path' es SIEMPRE relativo al proyecto "
            "(p.ej. 'assets/foto.jpg'), igual que en propose_project_files."
        ),
        created_by="system",
        requires_filesystem_write=True,
        requires_network=True,
        parameters_schema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["image"],
                    "description": "Tipo de recurso a importar — hoy solo 'image'",
                },
                "url": {"type": "string", "description": "URL real de la imagen (conseguida vía browser action='images')"},
                "destination_path": {
                    "type": "string",
                    "description": "Ruta RELATIVA al proyecto donde guardarlo, p.ej. 'assets/foto.jpg'",
                },
            },
            "required": ["type", "url", "destination_path"],
        },
    )

    def execute(self, type: str, url: str, destination_path: str, **kwargs) -> Artifact:
        _validate_relative_path(destination_path)

        hostname = urlparse(url).hostname or url
        network_decision = network_access_manager.evaluate(
            skill_name=VSCODE_INTEGRATION_SKILL_NAME,
            scope=NetworkScope.INTERNET,
            action=NetworkAction.DOWNLOAD,
            resource_key=hostname,
        )
        if network_decision != "auto_allowed":
            pending = network_access_manager.create_pending_request(
                skill_name=VSCODE_INTEGRATION_SKILL_NAME,
                scope=NetworkScope.INTERNET,
                action=NetworkAction.DOWNLOAD,
                resource_key=hostname,
            )
            return Artifact(
                modality="text", uri="",
                metadata={
                    "status": "requires_approval", "resource_kind": "network", "request_id": pending.id,
                    "stderr": f"Descargar desde '{hostname}' requiere aprobación humana — pedile al usuario "
                              "que la apruebe (GET /network-access) antes de reintentar.",
                },
            )

        try:
            resource = download_manager.download_and_validate(url, expected_type=type)
        except DownloadValidationError as e:
            return Artifact(
                modality="text", uri="", metadata={"status": "error", "stderr": str(e)},
            )

        decision = filesystem_access_manager.evaluate(
            skill_name=VSCODE_INTEGRATION_SKILL_NAME,
            scope=FilesystemScope.WORKSPACE,
            action=FilesystemAction.CREATE,
            resource_key="workspace",
        )
        if decision != "auto_allowed":
            return Artifact(
                modality="project_files",
                uri="",
                metadata={"status": "requires_approval", "files": []},
            )

        request_id = str(uuid4())
        audit_log.record(
            AuditEvent(
                event_type="artifact_imported",
                summary=f"Importado '{destination_path}' desde {url}",
                context={
                    "url": url, "destination_path": destination_path, "sha256": resource.sha256,
                    "mime": resource.mime, "size_bytes": resource.size_bytes,
                },
                outcome="success",
            )
        )

        return Artifact(
            modality="project_files",
            uri="",
            metadata={
                "status": "proposed",
                "request_id": request_id,
                "files": [
                    {
                        "path": destination_path,
                        "content": base64.b64encode(resource.content).decode("ascii"),
                        "encoding": "base64",
                    }
                ],
            },
        )


class ReadWorkspaceFileTool(Tool):
    """
    Pieza mínima de "Editor Context Provider" (2026-07-20, pedido
    explícito del usuario): kal no puede leer un archivo arbitrario del
    workspace por sí mismo (mismo límite arquitectónico que el resto de
    este archivo — no sabe qué carpeta tiene abierta VS Code), así que
    esta Tool NUNCA lee nada ella misma. Devuelve un Artifact "pending"
    con la ruta pedida; la extensión (vscode-extension/src/
    readWorkspaceFile.ts) lee el archivo real del disco y vuelve a
    llamar a /chat automáticamente con su contenido — kal sigue
    razonando en un paso siguiente, invisible para el usuario (nunca
    tiene que pedirlo dos veces a mano). Sin este mecanismo encadenado,
    kal solo podría "leer" el ÚNICO archivo activo en el editor en el
    momento del pedido (ver EditorContextSignals) — nunca uno arbitrario
    del árbol, ni siquiera si el usuario lo tiene abierto en OTRA
    pestaña.
    """

    manifest = ToolManifest(
        name="read_workspace_file",
        description=(
            "Pide el contenido REAL de un archivo del proyecto abierto en VS Code, por su ruta "
            "relativa (p.ej. 'restaurante-web/estilos.css') — usá el árbol de archivos del "
            "contexto de esta conversación para saber qué rutas existen de verdad, nunca "
            "inventes una. El contenido NO llega en esta misma respuesta: la extensión lo lee del "
            "disco real y te lo entrega automáticamente en un paso siguiente de este mismo turno "
            "— no hace falta pedirlo de nuevo ni avisarle al usuario que esperés. Usala antes de "
            "asumir o inventar el contenido de un archivo que todavía no forma parte de esta "
            "conversación (no está abierto ni fue pegado en el chat)."
        ),
        created_by="system",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta RELATIVA al proyecto, p.ej. 'restaurante-web/index.html'",
                },
            },
            "required": ["path"],
        },
    )

    def execute(self, path: str, **kwargs) -> Artifact:
        _validate_relative_path(path)

        decision = filesystem_access_manager.evaluate(
            skill_name=VSCODE_INTEGRATION_SKILL_NAME,
            scope=FilesystemScope.WORKSPACE,
            action=FilesystemAction.READ,
            resource_key="workspace",
        )
        if decision != "auto_allowed":
            # Mismo criterio fail-closed que el resto del archivo — en la
            # config por defecto (filesystem_access.auto_allow.workspace
            # incluye "read") esta rama es puramente defensiva, nunca se
            # ejercita en la práctica.
            pending = filesystem_access_manager.create_pending_request(
                skill_name=VSCODE_INTEGRATION_SKILL_NAME,
                scope=FilesystemScope.WORKSPACE,
                action=FilesystemAction.READ,
                resource_key="workspace",
            )
            return Artifact(
                modality="text", uri="",
                metadata={
                    "status": "requires_approval", "resource_kind": "filesystem", "request_id": pending.id,
                    "stderr": f"Leer '{path}' requiere aprobación humana — pedile al usuario que la "
                              "apruebe (GET /filesystem-access) antes de reintentar.",
                },
            )

        return Artifact(
            modality="workspace_file_request",
            uri="",
            metadata={"status": "pending", "request_id": str(uuid4()), "path": path},
        )

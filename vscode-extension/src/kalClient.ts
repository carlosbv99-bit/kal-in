/**
 * Cliente HTTP hacia el backend de kal (agent_core/orchestrator.py,
 * FastAPI). Mismo contrato que ya consume frontend/app.js: POST /chat
 * con {goal, model, use_planner} -> {final_answer, status, plan, steps}.
 *
 * `fetchFn` es inyectable (por defecto nodeHttpFetch, NO el fetch()
 * global de Node) para poder testear sin red real — mismo patrón de DI
 * que ya usa el backend Python (ver tool_integration/adapters/
 * browser.py::BrowserTool(driver=...)). Ver nodeHttpFetch.ts para el
 * motivo: el fetch() global tiene un tope fijo de ~5 minutos que un
 * pedido real (varios pasos de un modelo local lento, ver
 * readWorkspaceFile.ts) puede superar.
 *
 * `editor_context`: señal CRUDA del editor (ver
 * agent_core/context_service.py) — este cliente nunca la formatea a
 * texto, solo la reenvía tal cual. Decidir cómo se ve en el prompt
 * final es responsabilidad del backend, no de esta extensión.
 */
import { EditorSnapshot } from "./editorContextFormat";
import { nodeHttpFetch } from "./nodeHttpFetch";

export interface ProjectFile {
  path: string;
  content: string;
  // Ausente/"utf-8" = texto plano (propose_project_files). "base64" =
  // binario (import_resource, Artifact Service — ver
  // tool_integration/adapters/vscode_files.py::ImportResourceTool),
  // `content` es el binario codificado en base64.
  encoding?: "utf-8" | "base64";
}

export interface ProjectFilesArtifact {
  modality: "project_files";
  request_id: string;
  files: ProjectFile[];
}

/**
 * Ver ReadWorkspaceFileTool (tool_integration/adapters/vscode_files.py)
 * y readWorkspaceFile.ts — el backend nunca lee el archivo real (no
 * tiene acceso al disco de VS Code), solo avisa QUÉ ruta pedir. La
 * extensión lee `path` del disco real y encadena un /chat nuevo con el
 * contenido (ver readWorkspaceFile.ts::resolvePendingWorkspaceFileReads).
 */
export interface WorkspaceFileRequestArtifact {
  modality: "workspace_file_request";
  request_id: string;
  path: string;
}

/**
 * Ver AndroidBuildScreenshotTool (tool_integration/adapters/
 * vscode_android.py) y androidBuild.ts — el backend no tiene acceso a
 * ningún dispositivo Android ni al proyecto real, solo avisa que hay
 * un pedido pendiente. A diferencia de workspace_file_request, esto
 * NO se encadena de vuelta al modelo: el resultado (captura real, o
 * el error de Gradle) se le muestra al usuario directamente.
 */
export interface AndroidBuildRequestArtifact {
  modality: "android_build_request";
  request_id: string;
}

export interface ChatStep {
  tool: string;
  arguments: Record<string, unknown>;
  observation: string;
  // Solo se tipan completos "project_files"/"workspace_file_request"/
  // "android_build_request" (lo único que esta extensión necesita leer
  // estructurado, ver projectFiles.ts/readWorkspaceFile.ts/
  // androidBuild.ts) — cualquier otro modality (p.ej. "image") llega
  // tal cual, sin usarse acá.
  artifact?: ProjectFilesArtifact | WorkspaceFileRequestArtifact | AndroidBuildRequestArtifact | Record<string, unknown> | null;
}

export interface ChatResult {
  session_id: string;
  goal: string;
  final_answer: string;
  status: string;
  plan: string[];
  steps: ChatStep[];
}

export type FetchFn = typeof fetch;

export class KalClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetchFn: FetchFn = nodeHttpFetch
  ) {}

  async chat(goal: string, model?: string, sessionId?: string, editorContext?: EditorSnapshot): Promise<ChatResult> {
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal,
          model: model || null,
          session_id: sessionId || null,
          editor_context: editorContext
            ? {
                relative_path: editorContext.relativePath,
                language_id: editorContext.languageId,
                text: editorContext.text,
                is_selection: editorContext.isSelection,
                workspace_tree: editorContext.workspaceTree ?? [],
                open_editors: editorContext.openEditors ?? [],
              }
            : null,
          client: "vscode",
        }),
      });
    } catch (e) {
      throw new Error(
        `No se pudo conectar con kal-in en ${this.baseUrl} — ¿está corriendo ./scripts/run_kal.sh? (${e})`
      );
    }

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new Error(`Kal-in respondió ${response.status}: ${detail}`);
    }

    return (await response.json()) as ChatResult;
  }

  async health(): Promise<boolean> {
    try {
      const response = await this.fetchFn(`${this.baseUrl}/health`);
      return response.ok;
    } catch {
      return false;
    }
  }

  /**
   * Deja constancia auditada de qué pasó DE VERDAD con una propuesta de
   * propose_project_files (ver tool_integration/adapters/vscode_files.py)
   * después de que el usuario decide en la vista previa — el Kernel ya
   * auto-permitió la acción por política, esto no pide ni espera
   * ninguna aprobación, solo audita. Sin token admin (ver
   * agent_core/orchestrator.py::report_filesystem_access_outcome).
   * Best-effort: si falla, no debe romper el flujo de escritura real,
   * que ya ocurrió (o se descartó) para cuando esto se llama.
   */
  async reportFilesystemAccessOutcome(
    requestId: string,
    outcome: "written" | "discarded",
    filesWritten: string[]
  ): Promise<void> {
    try {
      await this.fetchFn(`${this.baseUrl}/filesystem-access/${requestId}/report-outcome`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome, files_written: filesWritten }),
      });
    } catch {
      // best-effort, ver docstring
    }
  }

  /**
   * Deja constancia auditada de qué pasó DE VERDAD con un pedido de
   * android_build_and_screenshot (ver tool_integration/adapters/
   * vscode_android.py) — compilar/instalar/capturar ocurre enteramente
   * del lado de esta extensión (gradlew/adb como procesos nativos, sin
   * pasar por el backend en absoluto). Sin token admin ni decisión de
   * permiso que auditar (a diferencia de filesystem-access) — solo el
   * resultado real. Best-effort: si falla, no debe romper el flujo real
   * (compilar/instalar/mostrar), que ya ocurrió para cuando esto se llama.
   */
  async reportAndroidBuildOutcome(
    requestId: string,
    outcome: "installed" | "build_failed" | "no_device" | "discarded",
    detail: string = ""
  ): Promise<void> {
    try {
      await this.fetchFn(`${this.baseUrl}/android-build/${requestId}/report-outcome`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outcome, detail }),
      });
    } catch {
      // best-effort, ver docstring
    }
  }
}

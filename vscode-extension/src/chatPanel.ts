import * as vscode from "vscode";
import { maybeHandleAndroidBuild } from "./androidBuild";
import { buildChatHtml } from "./chatWebviewHtml";
import { EditorSnapshot } from "./editorContextFormat";
import { KalClient } from "./kalClient";
import { handleOpenOrCreateFolder, handleProjectFilesDecision, maybeHandleProjectFiles } from "./projectFiles";
import { resolvePendingWorkspaceFileReads } from "./readWorkspaceFile";

/**
 * WebviewPanel singleton para el chat con kal. El webview (media/chat.js)
 * nunca llama a la API de kal directamente — le hace postMessage a este
 * host (Node.js, sin restricción CORS), que es quien usa KalClient.
 */
export class ChatPanel {
  private static current: ChatPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly client: KalClient;
  private readonly extensionUri: vscode.Uri;
  private readonly context: vscode.ExtensionContext;
  private readonly disposables: vscode.Disposable[] = [];
  // Un panel = una conversación (ver agent_core/sessions.py) — vive
  // mientras el panel esté abierto, se pierde al cerrarlo (igual que
  // cerrar una pestaña de chat empieza una conversación nueva). Se
  // puede reiniciar sin cerrar el panel con el botón "Nuevo chat"
  // (ver handleNewSession abajo).
  private sessionId: string | undefined;
  // Señal cruda del editor, adjunta al PRÓXIMO mensaje que se mande
  // (un solo uso — mismo comportamiento que el prefill de texto que
  // reemplaza, ver Context Service: agent_core/context_service.py).
  // La extensión nunca la formatea a texto, solo la reenvía.
  private pendingEditorContext: EditorSnapshot | undefined;
  // BUG REAL ENCONTRADO EN USO (2026-07-30): "no se pueden mandar
  // mensajes a un webview oculto" (misma limitación documentada de la
  // API de vscode ya resuelta en ChatViewProvider) — con esta pestaña
  // en segundo plano (el usuario mirando otro archivo/pestaña) cuando
  // "ready" intentaba postearse, el mensaje se perdía en silencio y el
  // campo de texto quedaba deshabilitado para siempre, sin ninguna
  // forma de recuperarlo. Mismo fix: encolar y reenviar cuando el
  // panel vuelve a ser visible (ver onDidChangeViewState abajo).
  private pendingMessages: unknown[] = [];

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri, client: KalClient, context: vscode.ExtensionContext) {
    this.panel = panel;
    this.extensionUri = extensionUri;
    this.client = client;
    this.context = context;

    this.panel.webview.html = buildChatHtml(this.panel.webview, this.extensionUri);

    this.disposables.push(
      this.panel.webview.onDidReceiveMessage(async (message) => {
        if (message?.type === "ask" && typeof message.text === "string") {
          await this.handleAsk(message.text);
        } else if (message?.type === "dismiss-context") {
          this.pendingEditorContext = undefined;
        } else if (message?.type === "project-files-decision") {
          await handleProjectFilesDecision(message, this.client, (m) => this.post(m));
        } else if (message?.type === "open-or-create-folder") {
          await handleOpenOrCreateFolder(message.decision);
        } else if (message?.type === "new-session") {
          this.handleNewSession();
        }
      }),
      this.panel.onDidChangeViewState(() => {
        if (this.panel.visible) {
          this.flushPendingMessages();
        }
      }),
      this.panel.onDidDispose(() => this.dispose())
    );
  }

  /** Manda `message` ya mismo si el panel está visible; si no, la encola (ver docstring de pendingMessages). */
  private post(message: unknown): void {
    if (this.panel.visible) {
      this.panel.webview.postMessage(message);
    } else {
      this.pendingMessages.push(message);
    }
  }

  private flushPendingMessages(): void {
    const queued = this.pendingMessages;
    this.pendingMessages = [];
    for (const message of queued) {
      this.panel.webview.postMessage(message);
    }
  }

  /**
   * Pedido explícito del usuario (2026-07-30): poder arrancar una
   * conversación nueva sin cerrar y reabrir el panel. También sirve
   * como salida de emergencia si el campo de texto quedara
   * deshabilitado por cualquier motivo (ver bug de arriba) — "ready"
   * se manda siempre acá, sin depender de ningún pedido en curso.
   */
  private handleNewSession(): void {
    this.sessionId = undefined;
    this.pendingEditorContext = undefined;
    this.post({ type: "new-session-started" });
    this.post({ type: "ready" });
  }

  static createOrShow(
    extensionUri: vscode.Uri,
    client: KalClient,
    context: vscode.ExtensionContext,
    editorSnapshot?: EditorSnapshot
  ): void {
    if (ChatPanel.current) {
      ChatPanel.current.panel.reveal(vscode.ViewColumn.Beside);
    } else {
      const panel = vscode.window.createWebviewPanel(
        "kalChat",
        "Kal-in",
        vscode.ViewColumn.Beside,
        {
          enableScripts: true,
          retainContextWhenHidden: true,
          localResourceRoots: [vscode.Uri.joinPath(extensionUri, "media")],
        }
      );
      ChatPanel.current = new ChatPanel(panel, extensionUri, client, context);
    }

    if (editorSnapshot) {
      ChatPanel.current.pendingEditorContext = editorSnapshot;
      ChatPanel.current.panel.webview.postMessage({
        type: "context-attached",
        relativePath: editorSnapshot.relativePath,
        isSelection: editorSnapshot.isSelection,
      });
    }
  }

  private async handleAsk(text: string): Promise<void> {
    const config = vscode.workspace.getConfiguration("kal");
    const model = config.get<string>("model") || undefined;
    const editorContext = this.pendingEditorContext;
    this.pendingEditorContext = undefined;

    try {
      const result = await this.client.chat(text, model, this.sessionId, editorContext);
      this.sessionId = result.session_id;
      // Resuelve cualquier pedido pendiente de read_workspace_file
      // encadenando /chat automáticamente (ver readWorkspaceFile.ts) —
      // transparente para el usuario, solo se muestra la respuesta FINAL.
      const finalResult = await resolvePendingWorkspaceFileReads(result, this.client, model, editorContext);
      this.sessionId = finalResult.session_id;
      this.post({ type: "answer", result: finalResult });
      const postToChat = (m: unknown) => this.post(m);
      await maybeHandleProjectFiles(finalResult, this.client, postToChat);
      await maybeHandleAndroidBuild(finalResult, this.client, postToChat, this.context);
    } catch (e) {
      this.post({ type: "error", message: String(e instanceof Error ? e.message : e) });
    } finally {
      // BUG REAL ENCONTRADO EN USO: mandar esto ANTES de que
      // maybeHandleProjectFiles() termine (p.ej. junto con "answer")
      // dejaba mandar un pedido nuevo mientras la vista previa de
      // archivos del pedido actual todavía esperaba una decisión — ver
      // media/chat.js. Con `finally`, se habilita el envío recién
      // cuando TODO terminó (incluso si algo falló).
      this.post({ type: "ready" });
    }
  }

  private dispose(): void {
    ChatPanel.current = undefined;
    this.disposables.forEach((d) => d.dispose());
    this.panel.dispose();
  }
}

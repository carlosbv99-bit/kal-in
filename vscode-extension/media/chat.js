// Lado webview: nunca llama a la API de kal directamente (evita CSP/red
// desde el webview) — le manda postMessage al extension host, que es
// quien hace el HTTP real (ver src/chatPanel.ts).
(function () {
  const vscode = acquireVsCodeApi();
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const contextIndicatorEl = document.getElementById("context-indicator");
  const contextLabelEl = document.getElementById("context-label");
  const contextDismissBtn = document.getElementById("context-dismiss");
  const newSessionBtn = document.getElementById("new-session");

  contextDismissBtn.addEventListener("click", () => {
    contextIndicatorEl.style.display = "none";
    vscode.postMessage({ type: "dismiss-context" });
  });

  // Pedido explícito del usuario (2026-07-30): poder arrancar una
  // conversación nueva en cualquier momento, sin cerrar y reabrir el
  // panel. También sirve como salida de emergencia si el campo de
  // texto quedara deshabilitado por cualquier motivo — ver el
  // "ready" que manda el extension host al recibir "new-session".
  newSessionBtn.addEventListener("click", () => {
    vscode.postMessage({ type: "new-session" });
  });

  function appendMessage(text, className) {
    const div = document.createElement("div");
    div.className = "msg " + className;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    appendMessage(text, "msg-user");
    inputEl.value = "";
    contextIndicatorEl.style.display = "none"; // adjunto de un solo uso, ver chatPanel.ts
    const pending = appendMessage("kal-in está pensando...", "msg-pending");
    pending.dataset.pending = "true";
    // Se deshabilita mientras el pedido está en curso, y se vuelve a
    // habilitar con "ready" (el extension host lo manda cuando el
    // pedido — incluida la propuesta de archivos, si la hubo —
    // terminó de publicarse). Una propuesta de propose_project_files
    // YA NO bloquea esto esperando una decisión (ver src/projectFiles.ts,
    // 2026-07-30): sus botones quedan en el propio mensaje del chat,
    // persistentes, así que el usuario puede seguir escribiendo sin
    // perder la propuesta de vista.
    inputEl.disabled = true;
    sendBtn.disabled = true;
    vscode.setState({ lastQuestion: text });
    vscode.postMessage({ type: "ask", text: text });
  }

  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      send();
    }
  });

  function removePending() {
    const pending = messagesEl.querySelector('[data-pending="true"]');
    if (pending) pending.remove();
  }

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message.type === "context-attached") {
      const label = message.isSelection ? "selección" : "archivo completo";
      contextLabelEl.textContent = `📎 ${message.relativePath} (${label})`;
      contextIndicatorEl.style.display = "flex";
      inputEl.focus();
    } else if (message.type === "answer") {
      removePending();
      const result = message.result;
      if (result.plan && result.plan.length > 1) {
        appendMessage("Plan:\n" + result.plan.map((s, i) => `${i + 1}. ${s}`).join("\n"), "msg-plan");
      }
      appendMessage(result.final_answer, "msg-agent");
    } else if (message.type === "error") {
      removePending();
      appendMessage(message.message, "msg-error");
    } else if (message.type === "project-files-notice") {
      // Ver src/projectFiles.ts: notificaciones de solo texto — error
      // de validación, descartada, o el resultado final tras aplicar.
      appendMessage(message.text, "msg-notice");
    } else if (message.type === "project-files-proposal") {
      // BUG REAL ENCONTRADO EN USO (2026-07-30): antes, la ÚNICA forma
      // de decidir era un diálogo modal nativo de VS Code — el usuario
      // reportó que se cerró solo antes de poder leerlo, sin ninguna
      // forma de recuperar la propuesta. Ahora los botones viven ACÁ,
      // dentro del mensaje del chat — parte del historial persistente
      // de la conversación, no un diálogo transitorio que se pueda
      // perder. Ver src/projectFiles.ts::handleProjectFilesDecision.
      const fileList = message.files.map((f) => f.path).join("\n");
      const div = appendMessage(
        `📋 Kal-in propone crear ${message.files.length} archivo(s) — todavía NO se guardaron:\n${fileList}`,
        "msg-notice"
      );

      const buttonRow = document.createElement("div");
      buttonRow.className = "proposal-buttons";

      function sendDecision(decision) {
        vscode.postMessage({ type: "project-files-decision", requestId: message.requestId, decision: decision, files: message.files });
      }

      const detailBtn = document.createElement("button");
      detailBtn.textContent = "Ver detalle";
      detailBtn.addEventListener("click", () => sendDecision("detail"));

      const applyBtn = document.createElement("button");
      applyBtn.textContent = "Aplicar";
      applyBtn.addEventListener("click", () => {
        sendDecision("apply");
        // Se deshabilitan ambos botones de decisión final (no "Ver
        // detalle", que se puede reabrir sin riesgo) para que un
        // doble clic no aplique/descarte dos veces la misma propuesta.
        applyBtn.disabled = true;
        discardBtn.disabled = true;
      });

      const discardBtn = document.createElement("button");
      discardBtn.textContent = "Descartar";
      discardBtn.addEventListener("click", () => {
        sendDecision("discard");
        applyBtn.disabled = true;
        discardBtn.disabled = true;
      });

      buttonRow.appendChild(detailBtn);
      buttonRow.appendChild(applyBtn);
      buttonRow.appendChild(discardBtn);
      div.appendChild(buttonRow);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } else if (message.type === "no-workspace-notice") {
      // Ver src/projectFiles.ts::handleOpenOrCreateFolder — pedido
      // explícito del usuario (2026-08-23): ofrecer abrir una carpeta
      // EXISTENTE o crear una NUEVA, como botones reales acá en vez de
      // un diálogo modal nativo con una sola opción.
      const div = appendMessage(message.text, "msg-notice");
      const buttonRow = document.createElement("div");
      buttonRow.className = "proposal-buttons";

      const openBtn = document.createElement("button");
      openBtn.textContent = "Abrir carpeta existente";
      openBtn.addEventListener("click", () => {
        vscode.postMessage({ type: "open-or-create-folder", decision: "open" });
      });

      const createBtn = document.createElement("button");
      createBtn.textContent = "Crear carpeta nueva";
      createBtn.addEventListener("click", () => {
        vscode.postMessage({ type: "open-or-create-folder", decision: "create" });
      });

      buttonRow.appendChild(openBtn);
      buttonRow.appendChild(createBtn);
      div.appendChild(buttonRow);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } else if (message.type === "android-build-notice") {
      // Ver src/androidBuild.ts: resultado real de compilar/instalar
      // una app Android — a diferencia de project-files-notice, puede
      // traer una captura de pantalla REAL del dispositivo (base64,
      // nunca sube al backend, viaja directo de la extensión al
      // webview) para mostrar inline.
      const div = appendMessage(message.text, "msg-notice");
      if (message.imageDataUri) {
        const img = document.createElement("img");
        img.src = message.imageDataUri;
        img.className = "android-screenshot";
        div.appendChild(img);
      }
    } else if (message.type === "new-session-started") {
      // Limpia la conversación visible — el session_id real ya se
      // reinició del lado del extension host (ver chatPanel.ts/
      // chatViewProvider.ts::handleNewSession).
      messagesEl.innerHTML = "";
      contextIndicatorEl.style.display = "none";
    } else if (message.type === "ready") {
      // También el recovery path del botón "Nuevo chat" si el
      // campo hubiera quedado deshabilitado por cualquier motivo (ver
      // BUG REAL ENCONTRADO EN USO en chatPanel.ts).
      inputEl.disabled = false;
      sendBtn.disabled = false;
      inputEl.focus();
    }
  });
})();

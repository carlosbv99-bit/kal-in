// kal — frontend vanilla, sin build step. Habla con la API FastAPI del
// propio proceso (mismo origen, sin CORS que configurar).

const API = "";

// Token administrativo (self-modification, aprobación/rollback de
// herramientas, configuración del modelo, etc.) — ver
// utils/admin_token.py y agent_core/orchestrator.py.
const ADMIN_TOKEN_STORAGE_KEY = "kal_admin_token";

function persistAdminTokenFromUrl() {
  const fromUrl = new URLSearchParams(window.location.search).get("admin_token");
  if (fromUrl) localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, fromUrl);
}

// FRICCIÓN REAL ENCONTRADA EN USO: pedirle a un usuario no-programador
// que copie el token de una terminal era impracticable. Si el
// navegador corre en la MISMA máquina que kal (el caso normal), el
// backend lo entrega solo (GET /admin-token, responde SOLO a loopback
// — ver orchestrator.py) y listo, nadie copia nada a mano. Si ya hay
// un token guardado (de la URL de arriba, o de una visita anterior),
// no se pisa. Si kal corre en otra máquina de la LAN, esto no
// devuelve nada (403) y sigue haciendo falta el prompt() de api() más
// abajo — que es exactamente el caso que el token protege.
async function ensureAdminToken() {
  persistAdminTokenFromUrl();
  if (localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)) return;
  try {
    const res = await fetch(API + "/admin-token");
    if (res.ok) {
      const data = await res.json();
      localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, data.token);
    }
  } catch (e) {
    // Sin red/backend todavía — no es fatal, el prompt() de api() sigue de respaldo.
  }
}

// ---------- Utilidades ----------

async function api(path, options = {}, _isRetry = false) {
  const adminToken = localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
  const res = await fetch(API + path, {
    headers: {
      "Content-Type": "application/json",
      ...(adminToken ? { "X-Kal-Admin-Token": adminToken } : {}),
    },
    ...options,
  });
  if (res.status === 401 && !_isRetry) {
    // Sin esto, el usuario tiene que ir a buscar la URL con
    // ?admin_token=... a mano — fricción real, encontrada en uso
    // repetidas veces. Le pedimos el token acá mismo, lo guardamos, y
    // reintentamos la MISMA acción una sola vez (nunca en loop).
    const pasted = window.prompt(
      "Esta acción necesita el token administrativo de kal-in.\n" +
      "Se imprime en la terminal donde corre ./scripts/run_kal.sh (busca 'Token administrativo').\n" +
      "Pégalo aquí:"
    );
    if (pasted) {
      localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, pasted.trim());
      return api(path, options, true);
    }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// BUG REAL ENCONTRADO EN USO (2026-09-22): el modelo devuelve markdown
// real (**negrita**, listas numeradas con títulos en negrita) — hasta
// ahora la respuesta se insertaba con textContent, así que el usuario
// veía los asteriscos literales en vez de texto en negrita. Acá se
// arma el HTML a mano, NUNCA con una librería que interprete markdown
// arbitrario sobre texto sin escapar primero: se escapa TODO el texto
// (neutraliza cualquier <, >, & que el modelo haya escrito, literal o
// como parte de un intento de inyección) ANTES de agregar las ÚNICAS
// etiquetas reales que insertamos nosotros (<strong>/<em>) — nunca se
// interpreta HTML que vino del modelo, solo la sintaxis de markdown
// que nosotros mismos traducimos a etiquetas seguras y conocidas.
// Alcance deliberadamente angosto (negrita/cursiva) — no un parser de
// markdown completo (tablas, código, links), que no hace falta para
// lo que el modelo realmente genera hoy.
function renderMarkdownLite(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
}

function formatTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

// ---------- Franja de estado (el elemento distintivo) ----------

async function refreshStatus() {
  const strip = document.getElementById("status-strip");
  let status;
  try {
    status = await api("/status");
  } catch (e) {
    strip.querySelectorAll(".lamp").forEach((l) => (l.className = "lamp critical"));
    return;
  }

  setLamp("audit_chain_verified", status.audit_chain_verified ? "ok" : "critical");
  setLamp("sandbox_network_mode", status.sandbox_network_mode === "none" ? "ok" : "warn");
  setLamp("open_circuit_breakers", status.open_circuit_breakers === 0 ? "ok" : "critical");

  const pendingTotal = (status.pending_tool_approvals || 0) + (status.pending_self_modification_approvals || 0);
  setLamp("pending_approvals", pendingTotal === 0 ? "ok" : "warn");

  setLamp("llm_available", status.llm_available ? "ok" : "critical");
}

function setLamp(key, state) {
  const node = document.querySelector(`.status-lamp[data-key="${key}"] .lamp`);
  if (node) node.className = `lamp ${state}`;
}

// ---------- Modelos ----------

// El selector junta TODAS las fuentes disponibles a la vez — Ollama
// local + cada perfil en la nube guardado que responda de verdad
// ahora mismo (nunca uno roto, ver GET /settings/llm/sources) — no
// solo la del proveedor ACTIVO. Elegir cualquier modelo activa su
// fuente automáticamente (ver el listener de "change" más abajo).
async function loadModels() {
  const select = document.getElementById("model-select");
  select.innerHTML = "";

  let defaultModel = null;
  let currentProvider = null;
  try {
    const settings = await api("/settings/llm");
    defaultModel = settings.default_model;
    currentProvider = settings.provider;
  } catch (e) { /* no crítico para armar la lista */ }

  let sourcesData;
  try {
    sourcesData = await api("/settings/llm/sources");
  } catch (e) {
    select.appendChild(el("option", null, "No se pudo cargar ningún modelo — ver pestaña Modelo"));
    return;
  }

  const sources = sourcesData.sources || [];
  if (sources.length === 0) {
    select.appendChild(el("option", null, "Sin modelos disponibles — ver pestaña Modelo"));
    return;
  }

  for (const source of sources) {
    const group = document.createElement("optgroup");
    group.label = source.label;
    for (const name of source.models) {
      const opt = el("option", null, name);
      opt.value = name;
      opt.dataset.source = source.name;
      group.appendChild(opt);
    }
    select.appendChild(group);
  }

  // Buscar el modelo por defecto actual en los modelos disponibles
  // Si no está disponible, seleccionar el primer modelo del proveedor actual
  let modelToSelect = defaultModel;
  if (defaultModel) {
    const match = Array.from(select.options).find((opt) => opt.value === defaultModel);
    if (!match) {
      // El modelo por defecto no está disponible, buscar uno compatible
      if (currentProvider === "ollama") {
        // Buscar en el grupo de Ollama
        const ollamaGroup = Array.from(select.children).find(child => child.label === "Local (Ollama)");
        if (ollamaGroup && ollamaGroup.children.length > 0) {
          modelToSelect = ollamaGroup.children[0].value;
        }
      } else {
        // Buscar en el grupo del proveedor en la nube actual
        const currentProfileGroup = Array.from(select.children).find(child => 
          child.label !== "Local (Ollama)" && child.children.length > 0
        );
        if (currentProfileGroup) {
          modelToSelect = currentProfileGroup.children[0].value;
        }
      }
    }
  } else if (sources.length > 0) {
    // Si no hay modelo por defecto, tomar el primer modelo disponible
    if (sources[0].models && sources[0].models.length > 0) {
      modelToSelect = sources[0].models[0];
    }
  }

  // Seleccionar el modelo adecuado
  if (modelToSelect) {
    const optionToSelect = Array.from(select.options).find((opt) => opt.value === modelToSelect);
    if (optionToSelect) {
      optionToSelect.selected = true;
    }
  }
}

// "Último modelo utilizado" (pedido explícito del usuario, 2026-07-24):
// con el Conversation Engine de por medio, el modelo que de verdad
// resolvió el ÚLTIMO turno puede ser distinto del configurado como
// default_model (p.ej. una aclaración rápida la resuelve el modelo
// chico del Conversation Engine, no el principal) — /chat ahora manda
// `model_used` (ver agent_core/routers/chat.py) reflejando eso. Solo
// actualiza la SELECCIÓN visible, nunca dispara el listener de
// "change" de abajo (no persiste ningún cambio de configuración real).
function reflectLastModelUsed(modelUsed) {
  if (!modelUsed) return;
  const select = document.getElementById("model-select");
  const match = Array.from(select.options).find((opt) => opt.value === modelUsed);
  if (match) {
    match.selected = true;
  }
}

document.getElementById("model-select").addEventListener("change", async (ev) => {
  const source = ev.target.selectedOptions[0]?.dataset.source;
  const modelName = ev.target.value;
  if (!source) return;
  
  // BUG REAL ENCONTRADO EN USO: esto no tenía try/catch — si el backend
  // rechazaba el cambio (p.ej. un modelo local sin soporte de
  // tool-calling, ver agent_core/llm_settings.py::update_llm_settings),
  // la falla quedaba silenciosa (una promesa rechazada sin manejar) y
  // el selector quedaba mostrando una opción que en realidad nunca se
  // activó, sin ninguna explicación visible para el usuario.
  try {
    if (source === "ollama") {
      // Para Ollama, verificar que el modelo no sea un modelo de nube
      if (modelName.endsWith(":cloud")) {
        alert(`El modelo ${modelName} requiere una sesión de Ollama en la nube. Inicia sesión con 'ollama login' primero.`);
        await loadModels();
        return;
      }

      await api("/settings/llm", { method: "POST", body: JSON.stringify({ provider: "ollama", default_model: modelName }) });
    } else {
      // Para proveedores en la nube, activamos el perfil y luego actualizamos el modelo
      await api("/settings/llm/activate-profile", { method: "POST", body: JSON.stringify({ name: source }) });
      await api("/settings/llm", { method: "POST", body: JSON.stringify({ default_model: modelName }) });
    }
  } catch (e) {
    // El mensaje ya viene explicado en detalle desde el backend (p.ej.
    // "no soporta llamadas a herramientas... elegí otro modelo").
    alert(e.message);
    await loadModels(); // restaura el selector al modelo realmente activo
    return;
  }

  await refreshStatus();
});

// ---------- Chat ----------

const chatScroll = document.getElementById("chat-scroll");
const chatEmpty = document.getElementById("chat-empty");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");

// Continuidad conversacional (ver agent_core/sessions.py): null hasta el
// primer /chat, el backend crea la sesión y devuelve su id — a partir de
// ahí se reusa mientras dure esta pestaña (recargar la página = chat
// nuevo, comportamiento esperable).
let sessionId = null;

function appendUserMessage(text) {
  chatEmpty.style.display = "none";
  const msg = el("div", "msg msg-user", text);
  chatScroll.appendChild(msg);
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

// El indicador de "trabajando" es un único elemento fijo, pegado arriba
// del botón de modelo — no un mensaje más del historial del chat.
const pendingIndicator = document.getElementById("pending-indicator");
const pendingCancelBtn = document.getElementById("pending-cancel-btn");
let currentAbortController = null;

function showPending(controller, sessionIdForPolling) {
  currentAbortController = controller;
  pendingIndicator.hidden = false;
  if (sessionIdForPolling) startProgressPolling(sessionIdForPolling);
}

function hidePending() {
  currentAbortController = null;
  pendingIndicator.hidden = true;
  stopProgressPolling();
}

pendingCancelBtn.addEventListener("click", () => {
  if (currentAbortController) currentAbortController.abort();
});

// ---------- Recorrido en vivo (qué modelo/herramienta está actuando
// AHORA MISMO, ver GET /chat/progress/{session_id} en
// agent_core/routers/chat.py) — mientras /chat todavía está en curso
// del lado del servidor. /chat es sincrónico (una sola llamada larga),
// así que esto hace polling: pregunta cada ~900ms qué se acumuló hasta
// el momento y agrega solo las líneas nuevas. ----------

const progressTrace = document.getElementById("progress-trace");
let progressPollTimer = null;

function _progressLineFor(entry) {
  let text;
  if (entry.stage === "conversation_engine") {
    text = `🧠 ${entry.model} analizó tu pedido (intención: ${entry.intent}, confianza: ${entry.confidence.toFixed(2)})`;
  } else if (entry.stage === "main_model") {
    // NUNCA "modelo principal" acá — kal no tiene un "cerebro" fijo (ver
    // agent_core/llm/provider.py y la aclaración del usuario en
    // docs/HISTORY.md sobre la terminología "cerebro"): esto es
    // simplemente el modelo configurado que está resolviendo ESTE turno.
    text = `⚙️ Modelo trabajando: ${entry.model}`;
  } else if (entry.stage === "tool_call") {
    // backend_model: el modelo ESPECIALIZADO detrás de esta herramienta
    // puntual (p.ej. llava:13b para analyze_image, SDXL-Turbo para
    // image_generation) — ver agent_core/routers/chat.py::
    // _backend_model_for_tool. Pedido explícito del usuario: antes solo
    // se veía el nombre de la herramienta, nunca qué modelo la resuelve.
    text = entry.backend_model ? `🔧 ${entry.tool} (${entry.backend_model})` : `🔧 ${entry.tool}`;
  } else {
    text = JSON.stringify(entry);
  }
  const line = el("div", "progress-trace-line", text);
  if (entry.stage === "tool_call" && !entry.ok) line.classList.add("is-error");
  return line;
}

function startProgressPolling(sessionIdForPolling) {
  stopProgressPolling();
  progressTrace.innerHTML = "";
  progressTrace.hidden = false;
  let shown = 0;
  progressPollTimer = setInterval(async () => {
    try {
      const data = await api(`/chat/progress/${sessionIdForPolling}`);
      const entries = data.progress || [];
      for (; shown < entries.length; shown++) {
        progressTrace.appendChild(_progressLineFor(entries[shown]));
      }
      progressTrace.scrollTop = progressTrace.scrollHeight;
    } catch (e) {
      // El polling es un plus informativo — un fallo puntual (red,
      // backend reiniciando) nunca debe romper el chat en sí.
    }
  }, 900);
}

function stopProgressPolling() {
  if (progressPollTimer) {
    clearInterval(progressPollTimer);
    progressPollTimer = null;
  }
  progressTrace.hidden = true;
  progressTrace.innerHTML = "";
}

// ---------- Imágenes en el chat ----------
// Toda imagen subida, generada, o editada/corregida aparece como un
// mensaje más del chat, en el orden en que ocurrió.

function appendImageMessage(url, altText) {
  chatEmpty.style.display = "none";
  const wrapper = el("div", "msg msg-agent");
  const link = document.createElement("a");
  link.className = "chat-image-link";
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  link.title = "Abrir imagen";
  const img = document.createElement("img");
  img.className = "chat-image-message";
  img.src = url;
  img.alt = altText || "Imagen";
  link.appendChild(img);
  wrapper.appendChild(link);
  chatScroll.appendChild(wrapper);
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

// ---------- Audio (generado por audio_generation, o subido por el usuario) en el chat ----------
// BUG REAL ENCONTRADO EN USO (2026-09-22): un artefacto con
// modality === "audio" nunca tenía ninguna rama de renderizado acá —
// el agente podía generar audio de verdad y el usuario nunca lo veía
// (ni podía escucharlo) en el chat.

function appendAudioMessage(url, label) {
  chatEmpty.style.display = "none";
  const wrapper = el("div", "msg msg-agent");
  if (label) wrapper.appendChild(el("div", "msg-audio-label", label));
  const audio = document.createElement("audio");
  audio.className = "chat-audio-message";
  audio.controls = true;
  audio.src = url;
  wrapper.appendChild(audio);
  chatScroll.appendChild(wrapper);
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

// ---------- Documentos de texto (CreateTextFileTool) en el chat ----------
// A diferencia de una imagen, un documento se ofrece como descarga, no
// se renderiza inline — el navegador ya sabe hacer esto con `download`.

function appendDocumentMessage(url, filename) {
  chatEmpty.style.display = "none";
  const wrapper = el("div", "msg msg-agent");
  const link = document.createElement("a");
  link.className = "chat-document-message";
  link.href = url;
  link.download = filename || "documento.txt";
  link.textContent = `📄 Descargar ${filename || "documento.txt"}`;
  wrapper.appendChild(link);
  chatScroll.appendChild(wrapper);
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

function appendAgentResult(result) {
  if (result.plan && result.plan.length > 1) {
    const planBox = el("div", "msg-steps");
    const title = el("div", "msg-step-tool", "Plan:");
    planBox.appendChild(title);
    result.plan.forEach((step, i) => {
      planBox.appendChild(el("div", "msg-step-line", `${i + 1}. ${step}`));
    });
    chatScroll.appendChild(planBox);
  }

  if (result.steps && result.steps.length > 0) {
    const stepsBox = el("div", "msg-steps");
    for (const step of result.steps) {
      const line = el("div", "msg-step-line");
      const toolSpan = el("span", "msg-step-tool", `${step.tool}`);
      line.appendChild(toolSpan);
      line.appendChild(document.createTextNode(`(${JSON.stringify(step.arguments)}) → ${truncate(step.observation, 200)}`));
      stepsBox.appendChild(line);

      // Toda imagen generada/editada/compuesta aparece como un
      // mensaje más del chat, sin que el usuario tenga que hacer nada.
      if (step.artifact && step.artifact.modality === "image") {
        appendImageMessage(step.artifact.url, "Imagen generada por kal-in");
      }
      if (step.artifact && step.artifact.modality === "audio") {
        appendAudioMessage(step.artifact.url, "Audio generado por kal-in");
      }
      if (step.artifact && step.artifact.modality === "document") {
        appendDocumentMessage(step.artifact.url, step.artifact.filename);
      }
    }
    chatScroll.appendChild(stepsBox);
  }

  if (result.status === "llm_error") {
    chatScroll.appendChild(el("div", "msg-error", `No pude contactar a Ollama: ${result.final_answer}`));
  } else {
    const msg = el("div", "msg msg-agent");
    msg.innerHTML = renderMarkdownLite(result.final_answer);
    if (result.status === "max_steps_exceeded") {
      msg.appendChild(el("div", "dash-item-meta", "(se agotó el límite de pasos antes de una respuesta final)"));
    }
    chatScroll.appendChild(msg);
  }
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

function truncate(text, n) {
  if (!text) return "";
  return text.length > n ? text.slice(0, n) + "…" : text;
}

// Auto-crece con el contenido (p.ej. pegar código) hasta el límite de
// max-height del CSS (160px, ahí ya sigue con scroll interno normal),
// y vuelve a su tamaño de una línea después de enviar.
function autoResizeChatInput() {
  chatInput.style.height = "auto";
  chatInput.style.height = `${chatInput.scrollHeight}px`;
}

chatInput.addEventListener("input", autoResizeChatInput);

chatForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const goal = chatInput.value.trim();
  const fileToUpload = pendingUploadFile;
  if (!goal && !fileToUpload) return;

  chatInput.value = "";
  autoResizeChatInput(); // vuelve a una línea
  sendBtn.disabled = true;

  // Cancelar solo corta la ESPERA del lado del navegador — /chat es un
  // endpoint sincrónico (ver README), así que kal puede seguir
  // trabajando un rato más del lado del servidor aunque cancelemos acá;
  // no hay forma barata de interrumpir a mitad una llamada al modelo o
  // una generación de imagen ya en curso.
  const controller = new AbortController();
  // El recorrido en vivo (ver startProgressPolling) necesita un
  // session_id DESDE ANTES de que /chat responda — para el primer
  // mensaje de una sesión, el backend recién lo genera DENTRO de esa
  // misma llamada, así que se genera acá (el backend acepta cualquier
  // session_id desconocido y arranca una sesión nueva bajo ese mismo
  // id, ver agent_core/sessions.py::SessionManager.get_or_create).
  if (!sessionId) sessionId = crypto.randomUUID();
  showPending(controller, sessionId);

  const model = document.getElementById("model-select").value;

  try {
    // Adjuntar una imagen y escribir una instrucción para ella eran
    // dos acciones separadas (subía sola, apenas se elegía el
    // archivo) — ahora la imagen queda ESPERANDO (ver
    // imageUploadInput más abajo) hasta que el usuario aprieta Enter,
    // y acá se sube justo antes de mandar el pedido, en el mismo
    // envío. Si la subida falla, no se llega a mandar el pedido de
    // texto (el archivo queda esperando para reintentar).
    if (fileToUpload) {
      const uploaded = await uploadImage(fileToUpload, sessionId, controller);
      sessionId = uploaded.session_id;
      if (fileToUpload.type.startsWith("audio/")) {
        appendAudioMessage(uploaded.url, "Audio subido");
      } else {
        appendImageMessage(uploaded.url, "Imagen subida");
      }
      setPendingUploadFile(null);
    }

    if (!goal) {
      hidePending();
      return;
    }

    appendUserMessage(goal);
    const result = await api("/chat", {
      method: "POST",
      body: JSON.stringify({ goal, model: model || null, session_id: sessionId }),
      signal: controller.signal,
    });
    sessionId = result.session_id;
    hidePending();
    appendAgentResult(result);
    reflectLastModelUsed(result.model_used);
  } catch (e) {
    hidePending();
    if (e.name === "AbortError") {
      chatScroll.appendChild(el("div", "msg-error", "Cancelado. Kal-in puede seguir trabajando en el fondo un rato más, pero ya no esperamos esa respuesta."));
    } else {
      chatScroll.appendChild(el("div", "msg-error", `Error: ${e.message}`));
    }
  } finally {
    sendBtn.disabled = false;
    refreshDashTab(currentTab); // lo que kal hizo probablemente cambió algo en el panel
    refreshStatus();
  }
});

// ---------- Adjuntar una imagen propia (queda esperando hasta el Enter) ----------
//
// BUG DE UX ENCONTRADO EN USO: antes, elegir un archivo lo subía DE
// INMEDIATO — el usuario no tenía forma de escribir una instrucción
// para esa imagen ANTES de que se subiera; tenía que subir primero y
// después mandar un mensaje aparte. Ahora el archivo elegido queda
// "esperando" (pendingUploadFile) — se muestra como un chip arriba del
// textarea, el foco pasa al textarea, y recién se sube de verdad
// cuando el usuario aprieta Enter (ver chatForm submit más arriba),
// en el mismo envío que su instrucción.

const imageUploadInput = document.getElementById("image-upload-input");
const pendingAttachment = document.getElementById("pending-attachment");
const pendingAttachmentName = document.getElementById("pending-attachment-name");
const pendingAttachmentRemove = document.getElementById("pending-attachment-remove");
let pendingUploadFile = null;

function setPendingUploadFile(file) {
  pendingUploadFile = file;
  pendingAttachment.hidden = !file;
  if (file) {
    pendingAttachmentName.textContent = file.name;
    chatInput.focus();
  }
}

imageUploadInput.addEventListener("change", () => {
  const file = imageUploadInput.files[0];
  if (!file) return;
  setPendingUploadFile(file);
  imageUploadInput.value = ""; // permite elegir el mismo archivo de nuevo si se quita y se re-adjunta
});

pendingAttachmentRemove.addEventListener("click", () => setPendingUploadFile(null));

// ---------- Hablarle a kal-in por micrófono ----------
//
// Graba con MediaRecorder. A diferencia de un archivo de audio elegido
// a mano (que sí queda como adjunto para que el AGENTE lo transcriba
// con speech_to_text), acá la transcripción pasa ACÁ MISMO, en vivo,
// vía POST /transcribe (faster-whisper directo, sin LLM, sin sesión) —
// el texto transcripto se pone directo en el textarea, como si el
// usuario lo hubiera tecleado, y se envía solo al cortar. Dos partes:
// - VAD por volumen (Web Audio API, 100% local): corta la grabación
//   sola cuando detecta silencio sostenido después de haber escuchado
//   algo de voz, sin que el usuario tenga que hacer un segundo clic.
// - Transcripción PARCIAL cada ~2.5s mientras se graba, mostrada en
//   #voice-preview — "en vivo" en el sentido de "se actualiza mientras
//   hablás", no transcripción incremental real (cada parcial vuelve a
//   transcribir el audio acumulado hasta ese momento entero, faster-
//   whisper no soporta streaming incremental) — igual da la sensación
//   de fluidez que se pidió, con costo de CPU acotado (audio corto).
//
// DECISIÓN DE DISEÑO: no usa la Web Speech API nativa del navegador
// (SpeechRecognition) — manda el audio a un servicio en la nube del
// proveedor del navegador (Google en Chrome/Opera), rompiendo el
// principio "local-first, sin red inesperada" del resto de kal-in. Ver
// el mismo comentario en agent_core/routers/chat.py::transcribe_audio.
//
// LIMITACIÓN REAL, no un bug: getUserMedia exige un contexto seguro
// (HTTPS), con la excepción de "localhost"/"127.0.0.1" que los
// navegadores tratan como seguro para desarrollo local. Si kal-in corre
// en otra máquina de la LAN y se accede por su IP en HTTP plano (mismo
// caso ya documentado arriba para el token admin), el micrófono queda
// bloqueado por el navegador — no hay forma de evitar esto desde acá,
// haría falta HTTPS real.

const micBtn = document.getElementById("mic-btn");
const voicePreview = document.getElementById("voice-preview");

const VAD_SAMPLE_INTERVAL_MS = 200;
const VAD_SILENCE_RMS_THRESHOLD = 0.02; // amplitud RMS normalizada (0-1), ajustado a mano contra ruido de micrófono típico
const VAD_SILENCE_TIMEOUT_MS = 1800; // silencio sostenido antes de cortar sola
const VAD_MAX_RECORDING_MS = 30000; // tope de seguridad si el VAD nunca detecta silencio (ambiente ruidoso)
const PARTIAL_TRANSCRIBE_INTERVAL_MS = 2500;

let mediaRecorder = null;
let recordingStream = null;
let recordedChunks = [];
let audioContext = null;
let analyserNode = null;
let vadIntervalId = null;
let partialIntervalId = null;
let recordingStartedAt = 0;
let lastSpeechAt = 0;
let hasSpokenAtLeastOnce = false;

function micSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
}

function recordingMimeType() {
  // audio/webm es lo que soportan Chrome/Firefox de forma nativa; si no
  // está disponible (p.ej. Safari), se deja que MediaRecorder elija su
  // propio default en vez de forzar un tipo no soportado.
  return MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
}

async function transcribeBlob(blob, mimeType) {
  const extension = mimeType.includes("ogg") ? "ogg" : "webm";
  const formData = new FormData();
  formData.append("file", new File([blob], `voz.${extension}`, { type: mimeType }));
  const res = await fetch(API + "/transcribe", { method: "POST", body: formData });
  if (!res.ok) return null; // chunk parcial todavía no decodificable — normal, se reintenta en el próximo intervalo
  const data = await res.json();
  return data.text || "";
}

function startVAD() {
  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioContext.createMediaStreamSource(recordingStream);
  analyserNode = audioContext.createAnalyser();
  analyserNode.fftSize = 512;
  source.connect(analyserNode);
  const data = new Uint8Array(analyserNode.fftSize);

  recordingStartedAt = Date.now();
  lastSpeechAt = Date.now();
  hasSpokenAtLeastOnce = false;

  vadIntervalId = setInterval(() => {
    analyserNode.getByteTimeDomainData(data);
    let sumSquares = 0;
    for (let i = 0; i < data.length; i++) {
      const normalized = (data[i] - 128) / 128;
      sumSquares += normalized * normalized;
    }
    const rms = Math.sqrt(sumSquares / data.length);
    const now = Date.now();

    if (rms > VAD_SILENCE_RMS_THRESHOLD) {
      lastSpeechAt = now;
      hasSpokenAtLeastOnce = true;
    }

    const totalDuration = now - recordingStartedAt;
    const silenceDuration = now - lastSpeechAt;

    if (totalDuration > VAD_MAX_RECORDING_MS) {
      stopRecording();
    } else if (hasSpokenAtLeastOnce && silenceDuration > VAD_SILENCE_TIMEOUT_MS) {
      stopRecording();
    }
  }, VAD_SAMPLE_INTERVAL_MS);
}

function stopVAD() {
  if (vadIntervalId) clearInterval(vadIntervalId);
  vadIntervalId = null;
  if (audioContext) audioContext.close().catch(() => {});
  audioContext = null;
  analyserNode = null;
}

function startPartialTranscription(mimeType) {
  partialIntervalId = setInterval(() => {
    if (!mediaRecorder || mediaRecorder.state !== "recording") return;
    // requestData() fuerza un dataavailable con lo grabado hasta ACÁ,
    // sin cortar la grabación — el handler de abajo empuja ese chunk a
    // recordedChunks antes de que este setTimeout corto lo lea.
    mediaRecorder.requestData();
    setTimeout(async () => {
      if (recordedChunks.length === 0) return;
      const blob = new Blob(recordedChunks, { type: mimeType });
      const text = await transcribeBlob(blob, mimeType);
      if (text) {
        voicePreview.hidden = false;
        voicePreview.textContent = text;
      }
    }, 200);
  }, PARTIAL_TRANSCRIBE_INTERVAL_MS);
}

function stopPartialTranscription() {
  if (partialIntervalId) clearInterval(partialIntervalId);
  partialIntervalId = null;
}

async function startRecording() {
  if (!micSupported()) {
    window.alert("Este navegador no soporta grabar audio (falta MediaRecorder/getUserMedia).");
    return;
  }
  try {
    // BUG REAL ENCONTRADO EN USO: `{ audio: true }` (sin más) deja que
    // el navegador elija sus propios defaults de procesamiento de
    // audio, que en la práctica no siempre activan reducción de ruido
    // real — pedir estas tres constraints explícitas SÍ mejora la señal
    // real capturada (confirmado: el audio grabado tenía mucho ruido
    // ambiental de fondo y whisper no reconocía nada).
    recordingStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    window.alert("No se pudo acceder al micrófono — revisá los permisos del navegador para este sitio.");
    return;
  }

  const mimeType = recordingMimeType();
  mediaRecorder = mimeType ? new MediaRecorder(recordingStream, { mimeType }) : new MediaRecorder(recordingStream);
  recordedChunks = [];
  voicePreview.hidden = true;
  voicePreview.textContent = "";

  mediaRecorder.addEventListener("dataavailable", (ev) => {
    if (ev.data.size > 0) recordedChunks.push(ev.data);
  });

  mediaRecorder.addEventListener("stop", async () => {
    stopVAD();
    stopPartialTranscription();
    recordingStream.getTracks().forEach((track) => track.stop());
    recordingStream = null;
    // Deshabilitado hasta terminar de transcribir — sin esto, el botón
    // ya se ve "listo" (stopRecording() ya sacó .recording) mientras la
    // transcripción final todavía corre en segundo plano, dejando
    // arrancar una segunda grabación que pisa mediaRecorder/chunks de
    // la que se está por mandar.
    micBtn.disabled = true;

    const type = mediaRecorder.mimeType || mimeType || "audio/webm";
    voicePreview.hidden = false;
    voicePreview.textContent = "Transcribiendo…";

    const blob = new Blob(recordedChunks, { type });
    const text = await transcribeBlob(blob, type);

    voicePreview.hidden = true;
    voicePreview.textContent = "";
    micBtn.disabled = false;

    if (!text || !text.trim()) {
      window.alert("No se detectó nada en la grabación — probá de nuevo.");
      chatInput.focus();
      return;
    }

    chatInput.value = text.trim();
    chatForm.requestSubmit();
  });

  mediaRecorder.start();
  startVAD();
  startPartialTranscription(mimeType || mediaRecorder.mimeType);
  micBtn.classList.add("recording");
  micBtn.title = "Detener grabación";
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  micBtn.classList.remove("recording");
  micBtn.title = "Hablarle a kal-in por micrófono";
}

micBtn.addEventListener("click", () => {
  if (micBtn.classList.contains("recording")) {
    stopRecording();
  } else {
    startRecording();
  }
});

async function uploadImage(file, sessionIdForUpload, controller) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("session_id", sessionIdForUpload || "");
  const res = await fetch(API + "/uploads", { method: "POST", body: formData, signal: controller.signal });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

chatInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    chatForm.requestSubmit();
  }
});

// ---------- Dashboard: tabs ----------

let currentTab = "tasks";

document.getElementById("dash-tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".dash-tab");
  if (!btn) return;
  const tab = btn.dataset.tab;
  currentTab = tab;

  document.querySelectorAll(".dash-tab").forEach((t) => t.classList.toggle("active", t === btn));
  document.querySelectorAll(".dash-view").forEach((v) => v.classList.toggle("active", v.dataset.view === tab));

  refreshDashTab(tab);
});

document.querySelectorAll("[data-refresh]").forEach((btn) => {
  btn.addEventListener("click", () => refreshDashTab(btn.dataset.refresh));
});

function refreshDashTab(tab) {
  if (tab === "tasks") return refreshTasks();
  if (tab === "tools") return refreshTools();
  if (tab === "selfmod") return refreshSelfMod();
  if (tab === "audit") return refreshAudit();
  if (tab === "integrations") return refreshIntegrations();
  if (tab === "modelo") return refreshModelSettings();
}

// ---------- Tareas ----------

async function refreshTasks() {
  const list = document.getElementById("tasks-list");
  list.innerHTML = "";
  let tasks;
  try {
    tasks = await api("/tasks");
  } catch (e) {
    list.appendChild(el("div", "dash-empty", "No se pudo cargar tareas."));
    return;
  }
  if (tasks.length === 0) {
    list.appendChild(el("div", "dash-empty", "Sin tareas todavía."));
    return;
  }
  for (const t of tasks.slice(0, 30)) {
    const item = el("div", "dash-item");
    const title = el("div", "dash-item-title");
    title.appendChild(el("span", null, truncate(t.description, 40)));
    title.appendChild(statusBadge(t.status));
    item.appendChild(title);
    item.appendChild(el("div", "dash-item-meta", formatTime(t.created_at) + (t.error ? " · " + truncate(t.error, 60) : "")));
    list.appendChild(item);
  }
}

function statusBadge(status) {
  const map = { success: "badge-success", failed: "badge-critical", escalated: "badge-critical", running: "badge-warn", pending: "" };
  return el("span", `badge ${map[status] || ""}`, status);
}

// ---------- Memoria ----------

document.getElementById("memory-search-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = document.getElementById("memory-query").value.trim();
  const results = document.getElementById("memory-results");
  results.innerHTML = "";
  if (!q) return;

  let data;
  try {
    data = await api(`/memory/search?q=${encodeURIComponent(q)}&top_k=5`);
  } catch (e) {
    results.appendChild(el("div", "dash-empty", "Error al buscar."));
    return;
  }

  let any = false;
  for (const [tier, items] of Object.entries(data)) {
    for (const item of items) {
      any = true;
      const box = el("div", "dash-item");
      box.appendChild(el("div", "dash-item-title", truncate(item.content, 80)));
      box.appendChild(el("div", "dash-item-meta", tier));
      results.appendChild(box);
    }
  }
  if (!any) results.appendChild(el("div", "dash-empty", "Sin resultados."));
});

// ---------- Herramientas ----------

async function refreshTools() {
  const activeList = document.getElementById("tools-active-list");
  const pendingList = document.getElementById("tools-pending-list");
  activeList.innerHTML = "";
  pendingList.innerHTML = "";

  let data;
  try {
    data = await api("/tools");
  } catch (e) {
    activeList.appendChild(el("div", "dash-empty", "No se pudo cargar herramientas."));
    return;
  }

  if (data.active.length === 0) activeList.appendChild(el("div", "dash-empty", "Ninguna."));
  for (const tool of data.active) {
    const item = el("div", "dash-item");
    item.appendChild(el("div", "dash-item-title", tool.name));
    item.appendChild(el("div", "dash-item-meta", tool.description));
    activeList.appendChild(item);
  }

  if (data.pending.length === 0) pendingList.appendChild(el("div", "dash-empty", "Ninguna pendiente."));
  for (const tool of data.pending) {
    const item = el("div", "dash-item");
    const title = el("div", "dash-item-title");
    title.appendChild(el("span", null, tool.name));
    title.appendChild(el("span", "badge badge-warn", "pendiente"));
    item.appendChild(title);
    item.appendChild(el("div", "dash-item-meta", tool.description));

    const approveBtn = el("button", "mini-btn", "Aprobar");
    approveBtn.style.marginTop = "6px";
    approveBtn.addEventListener("click", async () => {
      approveBtn.disabled = true;
      try {
        await api(`/tools/${encodeURIComponent(tool.name)}/approve`, {
          method: "POST",
          body: JSON.stringify({ approved_by: "kalin (frontend)" }),
        });
        refreshTools();
        refreshStatus();
      } catch (e) {
        approveBtn.disabled = false;
        alert("No se pudo aprobar: " + e.message);
      }
    });
    item.appendChild(approveBtn);
    pendingList.appendChild(item);
  }
}

// ---------- Self-modification ----------

async function refreshSelfMod() {
  const list = document.getElementById("selfmod-list");
  list.innerHTML = "";
  let proposals;
  try {
    proposals = await api("/self-modification");
  } catch (e) {
    list.appendChild(el("div", "dash-empty", "No se pudo cargar propuestas."));
    return;
  }
  if (proposals.length === 0) {
    list.appendChild(el("div", "dash-empty", "Sin propuestas todavía."));
    return;
  }
  for (const p of proposals) {
    const item = el("div", "dash-item");
    const title = el("div", "dash-item-title");
    title.appendChild(el("span", null, p.target_path));
    title.appendChild(selfModBadge(p.status));
    item.appendChild(title);
    item.appendChild(el("div", "dash-item-meta", truncate(p.justification, 70)));
    if (p.detail) item.appendChild(el("div", "dash-item-meta", truncate(p.detail, 90)));

    if (p.status === "pending_human_approval") {
      const applyBtn = el("button", "mini-btn", "Aplicar");
      applyBtn.style.marginTop = "6px";
      applyBtn.addEventListener("click", async () => {
        if (!confirm(`¿Aplicar el cambio propuesto a ${p.target_path}?`)) return;
        applyBtn.disabled = true;
        try {
          await api("/self-modification/apply", {
            method: "POST",
            body: JSON.stringify({ proposal_id: p.id, approved_by: "kalin (frontend)" }),
          });
          refreshSelfMod();
        } catch (e) {
          applyBtn.disabled = false;
          alert("No se pudo aplicar: " + e.message);
        }
      });
      item.appendChild(applyBtn);
    }
    list.appendChild(item);
  }
}

function selfModBadge(status) {
  const map = {
    applied: "badge-success",
    pending_human_approval: "badge-warn",
    blocked_core: "badge-critical",
    rejected_unsafe: "badge-critical",
    regression_detected: "badge-critical",
    rolled_back: "",
  };
  return el("span", `badge ${map[status] || ""}`, status);
}

// ---------- Auditoría ----------

async function refreshAudit() {
  const list = document.getElementById("audit-list");
  list.innerHTML = "";
  let data;
  try {
    data = await api("/audit/tail?n=40");
  } catch (e) {
    list.appendChild(el("div", "dash-empty", "No se pudo cargar auditoría."));
    return;
  }

  const header = el("div", "dash-item-meta", data.verified ? "cadena íntegra ✓" : "¡CADENA ROTA!");
  header.style.color = data.verified ? "var(--accent)" : "var(--critical)";
  list.appendChild(header);

  for (const entry of data.entries) {
    const item = el("div", "dash-item");
    const title = el("div", "dash-item-title");
    title.appendChild(el("span", null, entry.event_type));
    title.appendChild(el("span", `badge ${entry.outcome === "success" ? "badge-success" : entry.outcome === "failure" ? "badge-critical" : "badge-warn"}`, entry.outcome));
    item.appendChild(title);
    item.appendChild(el("div", "dash-item-meta", truncate(entry.summary, 90)));
    list.appendChild(item);
  }
}

// ---------- Integraciones de IDE ----------
// v1 escopado a VS Code únicamente (ver agent_core/vscode_integration.py):
// no instala VS Code mismo, solo la extensión de kal sobre un VS Code que
// ya asumimos instalado.

async function refreshIntegrations() {
  const list = document.getElementById("integrations-list");
  list.innerHTML = "";

  let status;
  try {
    status = await api("/integrations/vscode/status");
  } catch (e) {
    list.appendChild(el("div", "dash-empty", "No se pudo consultar el estado de la integración."));
    return;
  }

  const card = el("div", "dash-item");
  const title = el("div", "dash-item-title");
  title.appendChild(el("span", "integration-card-name", "VS Code"));
  title.appendChild(
    status.installed
      ? el("span", "badge badge-success", "instalado")
      : el("span", "badge badge-warn", "no instalado")
  );
  card.appendChild(title);

  if (!status.code_cli_available) {
    card.appendChild(el("div", "dash-item-meta", "No se encontró el comando 'code' en el PATH — instalalo desde VS Code: Ctrl+Shift+P → \"Shell Command: Install code command in PATH\"."));
  }

  const installBtn = el("button", "mini-btn integration-install-btn", status.installed ? "Reinstalar" : "Instalar");
  installBtn.disabled = !status.code_cli_available;
  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    installBtn.textContent = "Instalando…";
    try {
      await api("/integrations/vscode/install", { method: "POST" });
      refreshIntegrations();
    } catch (e) {
      alert("No se pudo instalar la extensión: " + e.message);
      installBtn.disabled = false;
      installBtn.textContent = status.installed ? "Reinstalar" : "Instalar";
    }
  });
  card.appendChild(installBtn);

  list.appendChild(card);
}

// ---------- Modelo del agente (local u en la nube) ----------
// kal se distribuye a usuarios con hardware muy distinto — esta pestaña
// tiene DOS acciones nada más: descargar un modelo Ollama local nuevo, y
// configurar la API key de un proveedor en la nube (Qwen, Grok/xAI,
// OpenAI...). Elegir CUÁL modelo usar en cada momento ya lo resuelve el
// selector de la barra de chat (arriba a la izquierda, ver #model-select)
// — acá nunca se elige "el modelo por defecto" a mano, eso es lo que en
// el futuro va a decidir kal mismo según la tarea.

// "Groq" (api.groq.com) y "Grok" (xAI, api.x.ai) son DOS empresas y
// APIs distintas — confusión real
// encontrada en uso: una API key que empieza con "gsk_..." es de Groq,
// nunca de xAI (esas empiezan con "xai-...").
const CLOUD_PROVIDER_PRESETS = {
  qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  grok: "https://api.x.ai/v1",
  groq: "https://api.groq.com/openai/v1",
  openai: "https://api.openai.com/v1",
  custom: "",
};

// Nombre corto de PERFIL guardado (ver agent_core/llm_settings.py::
// save_cloud_profile) — es lo que va a aparecer como grupo en el
// selector de modelo del chat, así que tiene que ser corto, a
// diferencia del texto largo/aclaratorio de las opciones del <select>
// de arriba. "custom" no tiene uno fijo — se pide en el campo de al lado.
const CLOUD_PROVIDER_PROFILE_NAMES = {
  qwen: "Qwen",
  grok: "Grok (xAI)",
  groq: "Groq",
  openai: "OpenAI",
};

const modelActiveProviderStatus = document.getElementById("model-active-provider-status");
const modelActivateOllamaBtn = document.getElementById("model-activate-ollama-btn");
const modelLocalList = document.getElementById("model-local-list");
const modelPullForm = document.getElementById("model-pull-form");
const modelPullNameInput = document.getElementById("model-pull-name");
const modelPullBtn = document.getElementById("model-pull-btn");
const modelPullFeedback = document.getElementById("model-pull-feedback");
const modelCloudForm = document.getElementById("model-cloud-form");
const modelCloudPresetSelect = document.getElementById("model-cloud-preset");
const modelCloudCustomUrlRow = document.getElementById("model-cloud-custom-url-row");
const modelCloudCustomNameInput = document.getElementById("model-cloud-custom-name");
const modelCloudCustomUrlInput = document.getElementById("model-cloud-custom-url");
const modelApiKeyInput = document.getElementById("model-api-key");
const modelApiKeyToggle = document.getElementById("model-api-key-toggle");
const modelApiKeyStatus = document.getElementById("model-api-key-status");
const modelSettingsFeedback = document.getElementById("model-settings-feedback");

modelApiKeyToggle.addEventListener("click", () => {
  modelApiKeyInput.type = modelApiKeyInput.type === "password" ? "text" : "password";
});

modelCloudPresetSelect.addEventListener("change", () => {
  modelCloudCustomUrlRow.hidden = modelCloudPresetSelect.value !== "custom";
});

modelActivateOllamaBtn.addEventListener("click", async () => {
  // BUG REAL ENCONTRADO EN USO: sin este botón, no había forma de
  // volver a Ollama local desde la interfaz una vez activado un
  // proveedor en la nube que dejó de responder (p.ej. sin créditos) —
  // el selector de modelo de la barra de chat quedaba inutilizable
  // sin ninguna salida.
  modelActivateOllamaBtn.disabled = true;
  modelSettingsFeedback.textContent = "Activando Ollama local…";
  try {
    await api("/settings/llm", { method: "POST", body: JSON.stringify({ provider: "ollama" }) });
    modelSettingsFeedback.textContent = "Ollama local activado.";
    await refreshModelSettings();
    await loadModels();
  } catch (e) {
    modelSettingsFeedback.textContent = "Error: " + e.message;
  } finally {
    modelActivateOllamaBtn.disabled = false;
  }
});

async function refreshModelSettings() {
  modelSettingsFeedback.textContent = "";
  modelPullFeedback.textContent = "";
  // Se recalcula acá también (no solo en el "change" del select) para
  // que nunca quede visible de más si se reabre la pestaña en un
  // estado raro.
  modelCloudCustomUrlRow.hidden = modelCloudPresetSelect.value !== "custom";

  let settings;
  try {
    settings = await api("/settings/llm");
  } catch (e) {
    modelActiveProviderStatus.textContent = "No se pudo cargar la configuración del modelo.";
    return;
  }
  
  // Mostrar información detallada del proveedor y modelo activo
  if (settings.provider === "ollama") {
    modelActiveProviderStatus.textContent = `Proveedor activo: Local (Ollama), Modelo: ${settings.default_model}`;
  } else {
    // Determinar el nombre del proveedor basado en la URL para mostrarlo más claramente
    let providerName = "Proveedor en la nube";
    if (settings.base_url.includes("api.x.ai")) {
      providerName = "Grok (xAI)";
    } else if (settings.base_url.includes("api.groq.com")) {
      providerName = "Groq";
    } else if (settings.base_url.includes("api.openai.com")) {
      providerName = "OpenAI";
    } else if (settings.base_url.includes("dashscope.aliyuncs.com")) {
      providerName = "Qwen (Alibaba)";
    }
    
    modelActiveProviderStatus.textContent = `Proveedor activo: ${providerName} (${settings.base_url}), Modelo: ${settings.default_model}`;
  }
  
  modelApiKeyStatus.textContent = settings.has_api_key ? "(ya configurada)" : "(no configurada)";
  modelApiKeyInput.value = "";

  modelLocalList.innerHTML = "";
  let ollamaModels;
  try {
    ollamaModels = await api("/settings/llm/ollama/models");
  } catch (e) {
    modelLocalList.appendChild(el("div", "dash-empty", "No se pudo consultar Ollama local."));
    return;
  }
  if (!ollamaModels.ollama_available) {
    modelLocalList.appendChild(el("div", "dash-empty", "Ollama no está corriendo en esta máquina."));
  } else if (ollamaModels.models.length === 0) {
    modelLocalList.appendChild(el("div", "dash-empty", "Sin modelos descargados todavía."));
  } else {
    // Anota cada modelo con lo que puede hacer de verdad (ver
    // GET /settings/llm/ollama/models -> "capabilities", vía Ollama
    // /api/show) — BUG REAL ENCONTRADO EN USO: sin esto, un modelo de
    // solo visión (llava:13b) simplemente desaparecía del selector de
    // arriba sin ninguna explicación de por qué.
    const capabilities = ollamaModels.capabilities || {};
    for (const name of ollamaModels.models) {
      const caps = capabilities[name] || [];
      const item = el("div", "dash-item", name);
      if (caps.includes("tools")) {
        item.appendChild(el("span", "dash-item-meta", " — puede usarse como modelo del chat"));
      } else if (caps.includes("vision")) {
        item.appendChild(
          el("span", "dash-item-meta", " — solo visión, no soporta herramientas (no aparece en el selector)")
        );
      }
      modelLocalList.appendChild(item);
    }
  }
}

modelPullForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const model = modelPullNameInput.value.trim();
  if (!model) return;

  modelPullBtn.disabled = true;
  modelPullFeedback.textContent = `Descargando "${model}"… puede tardar varios minutos (son varios GB), no cierres esta pestaña.`;
  try {
    await api("/settings/llm/ollama/pull", { method: "POST", body: JSON.stringify({ model }) });
    modelPullFeedback.textContent = `"${model}" descargado.`;
    modelPullNameInput.value = "";
    await refreshModelSettings();
    await loadModels();
  } catch (e) {
    modelPullFeedback.textContent = "Error: " + e.message;
  } finally {
    modelPullBtn.disabled = false;
  }
});

modelCloudForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  modelSettingsFeedback.textContent = "Guardando…";

  const preset = modelCloudPresetSelect.value;
  const baseUrl = preset === "custom" ? modelCloudCustomUrlInput.value.trim() : CLOUD_PROVIDER_PRESETS[preset];
  // Guardarlo con un nombre (ver profile_name) lo deja disponible como
  // perfil reusable — vuelve a aparecer en el selector de modelo del
  // chat más adelante, junto con cualquier otro perfil ya guardado.
  const profileName = preset === "custom" ? modelCloudCustomNameInput.value.trim() : CLOUD_PROVIDER_PROFILE_NAMES[preset];
  const body = {
    provider: "openai_compatible",
    base_url: baseUrl || null,
    api_key: modelApiKeyInput.value.trim() || null, // vacío = no tocar la que ya está guardada
    profile_name: profileName || null,
  };

  try {
    await api("/settings/llm", { method: "POST", body: JSON.stringify(body) });
    modelSettingsFeedback.textContent = "Guardado. Proveedor en la nube activado.";
    await refreshModelSettings();
    await loadModels(); // el selector de modelo del chat ahora incluye este perfil
  } catch (e) {
    modelSettingsFeedback.textContent = "Error: " + e.message;
  }
});

// ---------- Arranque ----------

ensureAdminToken().finally(() => {
  refreshStatus();
  loadModels();
  refreshTasks();
  setInterval(refreshStatus, 6000);
});

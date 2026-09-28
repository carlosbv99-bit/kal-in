# Auditoría de seguridad — kal-in

**Fecha:** 2026-09-26 · **Revisor:** auditoría externa (rol: ingeniero de sistemas / seguridad)
**Alcance:** ~37 000 líneas Python (kernel, agent_core, tool_integration, sdk, code_analysis, audit, task_execution, error_handling), frontend web, extensión VS Code, despliegue (Docker/compose/CI), cadena de dependencias.
**Método:** revisión estática de código con trazado de flujo de datos de punta a punta. Verificación empírica de las hipótesis de mayor impacto (lectura por symlink, detección de credenciales, robustez del log de auditoría, traversal de rutas, mime/extensiones). No se ejecutó el sistema con un LLM real ni se modificó ningún archivo del repositorio.

---

## 1. Resumen ejecutivo

kal-in es un proyecto con **una cultura de seguridad muy por encima de la media**: hay un modelo de amenaza explícito, un kernel de permisos en capas, sandbox Docker real, allowlist de red deny-by-default, verificación anti-DNS-rebinding, firma Ed25519 de skills y herramientas, un log de auditoría encadenado por hash, y comentarios que documentan vulnerabilidades *ya corregidas* con su razonamiento. El trabajo de hardening es genuino y no cosmético.

El problema no es la falta de mecanismos: es que **los mecanismos no cubren la frontera donde realmente vive el riesgo**, y que la postura global descansa en una suposición —*"el proceso orquestador es de confianza"*— que el propio diseño viola por múltiples caminos. En concreto, los tres invariantes que el README afirma como garantías del sistema **hoy no se sostienen**:

| Invariante declarado | Estado real |
|---|---|
| *"memory content matching known credential patterns gets redacted before it's ever kept long-term"* | **Falso.** La única ruta de redacción (`promote_mid_to_long`) es **inalcanzable**: nada incrementa `repetitions` ni `relevance_score`, así que `redact()` nunca se ejecuta. Además el patrón de OpenAI no matchea el formato actual (`sk-proj-…`) ni Anthropic, Stripe, Slack, Google, Groq, HF, GitHub PAT ni AWS temporales. |
| *"every Skill runs in an isolated, non-root Docker container"* | **Cierto para la ejecución**, pero el código de la skill nunca se audita como *contenido*: un symlink dentro de `skills/<x>/` hace que el proceso **host** lea archivos arbitrarios del host y los empaquete dentro del contenedor. |
| *"self-modification requires explicit human approval before anything reaches disk"* | **Cierto en su propia ruta**, pero la aprobación depende de un token que se imprime en los logs, se guarda en `localStorage`, y cualquier XSS same-origin (hay una cadena completa) lo roba; y `core/` está protegido por hardcode, no por el modelo de permisos. |

### Cadena de compromiso más grave (end-to-end, confirmada a nivel de código)

```
POST /uploads sin auth  (content_type lo elige el cliente, la EXTENSIÓN sale de file.filename)
   └─► se escribe  data/artifacts/uploads/<uuid>.html
        └─► GET /artifacts/uploads/<uuid>.html  → StaticFiles lo sirve como text/html, sin nosniff
             └─► JS ejecuta en el MISMO ORIGEN que el panel que guarda kal_admin_token en localStorage
                  └─► robo del token admin
                       └─► POST /self-modification/propose + /apply
                            └─► ESCRITURA DE CÓDIGO ARBITRARIO en el repo  →  RCE como el usuario
```
No se requiere ni siquiera esa cadena larga si el atacante ya tiene un proceso local: `GET /admin-token` entrega el token a **cualquier** proceso local (`orchestrator.py:326-335`).

---

## 2. Tabla de hallazgos

| ID | Sev | Hallazgo | Ubicación |
|---|---|---|---|
| C-1 | **CRÍTICO** | `COPY . .` sin `.dockerignore`: clave privada Ed25519 + token admin horneados en la imagen | `Dockerfile:11` |
| C-2 | **CRÍTICO** | `/uploads` sin auth escribe con extensión elegida por el cliente + `/artifacts` estático sin `nosniff` → XSS almacenado same-origin → token admin → RCE | `agent_core/routers/chat.py:493-550`, `orchestrator.py:457` |
| C-3 | **CRÍTICO** | Código arbitrario ejecutable sin auth en `sandbox_runner`, alcanzable desde `agent_net` (red compartida) | `kernel/api/sandbox_api.py:29-38` |
| C-4 | **CRÍTICO** | ~25 endpoints sin autenticación, incluido `/chat` (agente con tool-calling) y toda la API de memoria | `agent_core/routers/*.py` |
| C-5 | **CRÍTICO** | `run_kal.sh` bindea `0.0.0.0`: la API sin auth queda expuesta a la LAN; `TrustedHost` no frena clientes crudos (`curl -H 'Host: localhost'`) | `scripts/run_kal.sh:38` |
| A-1 | **ALTO** | `extra_mounts` sin validación + recolección de skills sigue symlinks: lectura arbitraria de archivos del host para una skill | `kernel/lifecycle/docker_runner.py:112-118,196-197`; `kernel/registry/sandboxed_skill.py:258-268` |
| A-2 | **ALTO** | Contenido de archivos del editor (atacante-controlado) concatenado **dentro del rol `system`**, con fence rompible y sin tope de tamaño | `agent_core/context_service.py:164-171`; `agent_core/llm/agent_loop.py:537-540` |
| A-3 | **ALTO** | Cero defensas de prompt injection indirecta: la salida de herramientas (web, OCR, archivos) se reinyecta cruda | `agent_core/llm/agent_loop.py:229-291,736` |
| A-4 | **ALTO** | Envenenamiento de memoria: `remember()` no clasifica; `ShortTermMemory` es un buffer **global del proceso** y `recall()` ignora la query → fuga entre sesiones | `tool_integration/adapters/core_tools.py:76-81`; `agent_core/memory/short_term.py:20-43` |
| A-5 | **ALTO** | La cascada de permisos es un **no-op** para casi todo: `system` concede los 9 permisos y `FILESYSTEM_READ` es implícito universal | `config/config.yaml:381-390`; `sdk/skill.py:44-51`; `permission_cascade.py:75-94` |
| A-6 | **ALTO** | La redacción de secretos **nunca corre** (ruta inalcanzable) y los patrones fallan contra los formatos actuales — verificado ejecutando `classify()` | `agent_core/memory/manager.py:142-145`; `security_policy.py:54-63` |
| A-7 | **ALTO** | SSRF autenticado + fuga de API key + `http://` en claro vía `base_url` del proveedor LLM; `is_unsafe_ip` no se aplica | `agent_core/llm_settings.py:111-136`; `openai_compatible_client.py:126-136` |
| A-8 | **ALTO** | Objetivo completo de cada `/chat` (y credenciales pegadas por el usuario) en `logs/agent.log` sin redacción | `agent_core/routers/chat.py:114` |
| A-9 | **ALTO** | Ejecución automática de `gradlew assembleDebug` + `adb install` a partir de un artefacto del modelo, sin confirmación modal | `vscode-extension/src/androidBuild.ts:138-247` |
| A-10 | **ALTO** | El log de auditoría se puede forjar e inflar sin autenticación, y un solo renglón corrupto lo bloquea por completo | `agent_core/routers/permissions.py:89-105`, `android_build.py:30-39`; `audit/audit_log.py:126-135` |
| M-1 | MEDIO | Token admin en `localStorage` + en query string + impreso en logs; sin rotación ni expiración | `frontend/app.js:9-13`; `orchestrator.py:296-300` |
| M-2 | MEDIO | Sin CSP, sin `X-Frame-Options`, sin `nosniff` en ningún punto → clickjacking sobre aprobaciones y agravamiento de cualquier XSS | `frontend/index.html`; `orchestrator.py` |
| M-3 | MEDIO | `verify`/`pin` de memoria sin auth y con `verified_by` **autodeclarado** por el cliente (mismo antipatrón ya corregido para `approved_by`) | `agent_core/routers/memory.py:35-50` |
| M-4 | MEDIO | SSRF por redirects en `DownloadManager`: se valida la IP del host inicial, no la del destino final | `tool_integration/download_manager.py:143-160` |
| M-5 | MEDIO | Inyección de variables en `.env` e inyección de YAML en `config.yaml` sin escapado | `agent_core/llm_settings.py:477-501` |
| M-6 | MEDIO | `compare_digest` con header no-ASCII → `TypeError` no capturado → 500 alcanzable sin auth | `orchestrator.py:304` |
| M-7 | MEDIO | Estado sin cota (`sessions`, `tasks`) + `verify_chain()` completo en cada `GET /status` + `top_k=-1` → volcado total | `sessions.py:88-100`; `health.py:34`; `memory.py:19` |
| M-8 | MEDIO | Subrecursos del navegador no validados: una página en dominio permitido puede hacer fetch/XHR a `127.0.0.1` y servicios internos | `tool_integration/adapters/browser.py:102-115` (límite documentado) |
| M-9 | MEDIO | Dependencias sin cota superior, sin lockfile y sin hashes; actions de CI pinneadas por tag mutable; imágenes Docker `:latest` sin digest | `requirements*.txt`; `.github/workflows/*`; `docker-compose.yml` |
| M-10 | MEDIO | `joblib.load` (pickle) de un artefacto versionado, en tiempo de import, sin verificación de integridad | `agent_core/tool_need_classifier.py:35` |
| M-11 | MEDIO | Proxy del socket Docker sin autenticación y alcanzable desde `agent_net`; con `POST=1`+`CONTAINERS=1` permite crear un contenedor privilegiado con `/` del host | `docker-compose.yml:50-70` |
| M-12 | MEDIO | La cadena del log de auditoría es SHA-256 sin clave: quien pueda escribir el archivo reescribe la cadena entera; no aporta autenticidad de origen | `audit/audit_log.py:71-79` |
| B-1…B-6 | BAJO | `exec()`/array JSON crudo aceptado como tool call; `.env` y `cloud_profiles.json` en 0644 con API keys; `video_gen` sin tope de escenas; `UnicodeDecodeError` mata el thread del bus; nombre de herramienta inválido → 500; `/docs` y `/openapi.json` expuestos | varios |

---

## 3. Detalle de los hallazgos críticos

### C-1 — Claves privadas y token admin dentro de la imagen Docker

**Confirmado.** No existe `.dockerignore` en el repositorio (`ls .dockerignore` → no existe) y `Dockerfile:11` hace `COPY . .`. Verificado en el árbol real:

```
$ ls -la data/keys/
-rw------- admin_token          (43 bytes)   ← token de /self-modification/apply
-rw------- tool_signing_key     (32 bytes)   ← clave privada Ed25519
-rw-rw-r-- tool_signing_key.pub
```

`kernel/registry/signing.py:46` confirma que `tool_signing_key` es la clave con la que se firman todas las herramientas dinámicas (`key_dir: data/keys`, `config/config.yaml:453`).

**Impacto.** Cualquiera que construya, reciba o descargue la imagen obtiene en sus capas (a) la clave privada de firma → puede firmar herramientas arbitrarias que el kernel aceptará como legítimas en cualquier instancia que comparta esa clave; (b) el `admin_token` → puede invocar `/self-modification/apply` y aprobar herramientas. Además viajan `.git` (historial completo, remotos) y `.venv` (~7 GB).

**Remediación.**
1. Crear `.dockerignore` con: `.env*`, `.git`, `.venv`, `venv`, `data/`, `logs/`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `tests/`, `docs/`, `dist/`, `vscode-extension/node_modules`, `vscode-extension/out`.
2. Montar `data/keys` como volumen externo; **nunca** incluirla en la imagen.
3. **Rotar** `tool_signing_key` y `admin_token` (deben considerarse comprometidos desde el primer build que los incluyó).
4. Añadir un gate de CI que falle si una capa de la imagen contiene `tool_signing_key`, `admin_token` o `LLM_API_KEY`.

### C-2 — XSS almacenado en el origen de la aplicación → token admin → RCE

**Confirmado.** Dos defectos independientes que se combinan:

```python
# agent_core/routers/chat.py:507-525
if file.content_type in _ALLOWED_IMAGE_UPLOAD_CONTENT_TYPES:   # ← header del CLIENTE, no se valida el contenido
    modality = "image"
...
suffix = Path(file.filename or "").suffix or (...)               # ← extensión elegida por el atacante
dest_path = upload_dir / f"{uuid.uuid4()}{suffix}"
```
```python
# agent_core/orchestrator.py:457
app.mount("/artifacts", StaticFiles(directory=str(_ARTIFACTS_DIR)), name="artifacts")   # sin auth, sin nosniff
```

`StaticFiles` deduce el `Content-Type` **de la extensión**. Enviando `filename="pwn.html"` con `Content-Type: image/png` se escribe `data/artifacts/uploads/<uuid>.html` y se sirve como `text/html` en el mismo origen que el panel. Lo mismo con `.svg` (`image/svg+xml` ejecuta script). El endpoint devuelve la URL exacta en la respuesta.

No hay ningún límite en el otro extremo: el `X-Content-Type-Options` no existe en todo el proyecto (grep: 0 coincidencias), y el token admin vive en `localStorage` (`frontend/app.js:9-13`), accesible a cualquier JS same-origin.

**Remediación.** Derivar la extensión **siempre** del `content_type` validado (mapa explícito png/jpeg/webp/wav/mpeg/webm/ogg/mp4) y nunca de `file.filename`; validar magic bytes (`PIL.Image.open().verify()`, `ffprobe`) antes de persistir; servir artefactos con `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`; y no guardar el token admin en `localStorage`.

### C-3 — Ejecución de código arbitrario sin autenticación en la red interna

```python
# kernel/api/sandbox_api.py:29-38
@app.post("/execute")
def execute(req: ExecuteRequest):
    result = executor.execute(req.source_code, req.context)   # sin auth, sin límite de tamaño
```
Se sirve en `0.0.0.0:9000` (`kernel/lifecycle/Dockerfile:32`) dentro de `agent_net`, la **misma red** que el contenedor `agent` y que `docker_socket_proxy`. La única barrera es el denylist AST (`code_analysis/denylist.py`) — que su propio docstring declara heurístico y no exhaustivo — más el aislamiento Docker.

**Remediación.** Secreto compartido en header entre `agent` y `sandbox_runner`, límite de tamaño de body, y desplegarlo en una red exclusiva (no la del agente).

### C-4 / C-5 — Superficie HTTP sin autenticar, publicada en la LAN

El token admin protege **13 endpoints**; todo el resto es abierto. Los relevantes para un atacante:

| Endpoint | Efecto | Línea |
|---|---|---|
| `POST /chat` | maneja el agente con tool-calling; incluye `deny_permissions: []` que **limpia** restricciones de sesión | `routers/chat.py:104,119-124` |
| `POST /uploads` | base de C-2 | `routers/chat.py:493` |
| `POST /transcribe` | `async def` llamando a un servicio **síncrono** con lock → bloquea el event loop completo | `routers/chat.py:566,609` |
| `POST /memory/{tier}/{id}/verify` · `/pin` | marca memoria como *verificada por humano* / permanente, con identidad **autodeclarada** | `routers/memory.py:35-50` |
| `POST /memory/consolidate` | fuerza la persistencia a SQLite de memoria envenenada | `routers/memory.py:30` |
| `GET /memory/search` | volcado de toda la memoria (incluidos secretos pegados) | `routers/memory.py:18` |
| `POST /filesystem-access/{id}/report-outcome` | **forja eventos** en el log de auditoría | `routers/permissions.py:89-105` |
| `GET /audit/tail?n=0` | devuelve el log entero; `n` negativo también | `routers/audit.py:10`; `audit_log.py:199` |
| `GET /skill-proposals/{id}` · `/self-modification/{id}` | devuelve código fuente completo propuesto | `skill_creator.py:37`; `self_modification.py:48` |

`scripts/run_kal.sh:38` arranca con `--host 0.0.0.0` (y es la ruta documentada en `README.md:178`), contradiciendo `docker-compose.yml:13` (`127.0.0.1:8000`), que el propio `orchestrator.py:288-294` cita como "la primera capa de defensa". `TrustedHostMiddleware` frena a un **navegador** de la LAN (DNS rebinding), pero no a un cliente crudo: `curl -H 'Host: localhost' http://192.168.x.x:8000/chat` pasa los filtros.

**Remediación.** Binding a `127.0.0.1` por defecto (opt-in explícito y advertido para LAN); autenticar todo endpoint que mute estado o procese trabajo costoso; `require_admin_token` en memoria, reportes de outcome y lecturas de propuestas.

---

## 4. Detalle de los hallazgos altos

### A-1 — Lectura arbitraria de archivos del host a través de una skill

`DockerSandboxRunner._collect_output_files()` ya usa `os.lstat` + `resolve()`/`is_relative_to` desde una auditoría previa (K-2). **Ese mismo fix nunca se aplicó al camino de entrada**, que es simétrico:

```python
# kernel/registry/sandboxed_skill.py:258-268
for path in self.skill_dir.rglob("*"):
    if not path.is_file():          # is_file() SIGUE symlinks
        continue
    ...
    files[f"skill/{rel}"] = path.read_text(...)   # lee el destino real
```

Verificado empíricamente con la réplica exacta del método:

```
skill/logo.png: 'root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:daemo'   # /etc/passwd vía symlink
```

Una carpeta `skills/<x>/` con un symlink a `/etc/passwd`, `~/.ssh/id_rsa`, `data/keys/admin_token` o `.env` hace que el **proceso host** lea ese archivo y lo empaquete en `/workspace/skill/` dentro del contenedor, donde la skill lo exfiltra si tiene `Permission.NETWORK`. Adicionalmente, `read_bytes()` sobre un symlink a `/dev/zero` (o un archivo enorme) produce agotamiento de memoria del proceso principal — verificado: el proceso queda bloqueado leyendo sin fin. Y `pathlib.Path.rglob` en Python ≥3.13 sigue symlinks a directorios.

**Relacionado:** `extra_mounts` (`docker_runner.py:196-197`) se pasa a `volumes[...] = {"bind": ..., "mode": "rw"}` sin validar que `host_path` esté en una allowlist. Hoy sus dos llamadores lo alimentan con rutas fijas de primera parte (dir temporal del socket, copia del proyecto en self-modification), así que **no es explotable in situ**, pero es un footgun latente: un llamador futuro o un refactor convierte una manifest en un montaje rw de `/` del host. Igual `_prepare_workdir` (`:112-118`) y `output_dir` (`:192-193`) aceptan claves con `../` sin normalizar.

**Remediación.** Usar `os.lstat` + descartar no-regulares en `_collect_skill_files` (y en `_skill_files` de `skill_signing.py`, que tiene el mismo patrón), `resolve()`/`is_relative_to` sobre `skill_dir`; imponer tope de tamaño por archivo; validar `extra_mounts`/`workspace_files`/`output_dir` contra una raíz permitida.

### A-2 / A-3 — Prompt injection: sin defensas, y con la peor superficie posible

`agent_loop.py:537-540` funde el contexto de sesión **dentro del mensaje `system`**:

```python
system_content = SYSTEM_PROMPT
if session_context:
    system_content = f"{SYSTEM_PROMPT}\n\n{session_context['content']}"
```

Y ese contexto incluye, sin sanitizar, el **texto completo del archivo abierto** (`context_service.py:164-171`, envuelto en un fence cuyo triple backtick el contenido puede cerrar), el árbol de archivos y las pestañas abiertas. La extensión manda el documento entero (`vscode-extension/src/editorContext.ts:24,37`). Es decir: **abrir un README de un repo clonado coloca sus instrucciones en el rol de máxima prioridad**, detrás del prompt legítimo. `EditorContextRequest.text` no tiene `max_length`.

Del otro lado, `_artifact_to_observation` (`:229-291`) devuelve el texto crudo del `browser` (el `inner_text()` del `<body>` completo), del OCR, de archivos del workspace y del stdout del sandbox, y `:736` lo agrega como `role="tool"` sin marcarlo como no confiable. Grep de delimitadores/spotlighting/provenance en `agent_core/`: **cero resultados**. El `SYSTEM_PROMPT` no menciona "inyección" ni "contenido no confiable".

Es importante ser justo: el proyecto **sí** endureció la *entrada* del navegador (allowlist, re-chequeo tras redirect, `is_unsafe_ip`). Eso reduce *qué* contenido entra, no *qué puede hacer* una vez dentro. Como el modelo puede invocar herramientas con efectos (A-5), una inyección exitosa tiene consecuencias reales, no solo de texto.

**Amplificador (B-1):** `json_extraction.py:20-23` acepta como tool call cualquier bloque ```` ```json {...} ``` ```` **o JSON desnudo** presente en la respuesta del modelo (`agent_loop.py:581-586`). Si el modelo *cita* un bloque JSON que venía en una página web, el loop lo **ejecuta** sin que el modelo haya decidido llamar nada. Además acepta un array JSON crudo como `propose_project_files`.

**Remediación.** Mover todo contenido de terceros a `role="user"`/`tool` con delimitador no falsificable y regla de jerarquía explícita; neutralizar fences y saltos de línea en rutas/nombres; tope duro de tamaño para `editor_context.text` y para cada observación; re-anclar las reglas críticas *después* del bloque no confiable; exigir `tool_calls` nativo para herramientas con efectos.

### A-4 — Envenenamiento de memoria que cruza sesiones

```python
# agent_core/memory/short_term.py:20-43
self._buffer: deque[MemoryItem] = deque()          # ÚNICO POR PROCESO (MemoryManager es singleton)
def retrieve(self, query, top_k=5):
    return list(self._buffer)[-top_k:]              # ignora la query
```
`remember()` no clasifica ni redacta (`core_tools.py:76-81`), `recall()` reinyecta el contenido crudo (`core_tools.py:102-129`) y `consolidate_short_to_mid()` lo persiste a `data/mid_term/memory.db` en claro durante 30 días. Lo dispara `POST /memory/consolidate`, **sin autenticación**.

Consecuencia concreta: contenido inyectado en la sesión A vía `browser` se guarda y reaparece como memoria "del sistema" en la sesión B, porque `retrieve()` devuelve los últimos N items sin importar la consulta. Agravado por M-3: `POST /memory/{tier}/{id}/verify` acepta `verified_by` como string libre, así que un atacante marca ese contenido como *verificado por un humano* y `pin` lo hace permanente.

**Remediación.** `classify()`/`redact()` (o rechazo) en `remember()` y obligatorio en `consolidate_short_to_mid()`; `ShortTermMemory` por sesión; `require_admin_token` en memoria; y derivar la identidad de verificación del principal autenticado, nunca del cuerpo del request.

### A-5 — La cascada de permisos no es un control efectivo para el toolset real

```python
# config/config.yaml:369-390
globally_denied: []          # vacío
trust_tier_caps:
  system: [filesystem_read, filesystem_write, network, browser, gpu, camera, microphone, clipboard, docker]
```
```python
# sdk/skill.py:50
derived.add(Permission.FILESYSTEM_READ)   # TODA herramienta hereda esto siempre
```
De las ~15 herramientas de primera parte, **una sola** declara un permiso explícito (`browser.py:246`). Con eso, `missing_permissions()` devuelve vacío para casi todas y el chequeo de `agent_loop.py:827-845` no deniega nada. El control real de escritura (`filesystem_access_manager`) solo lo consulta el adaptador de VS Code, no `create_text_file` ni `propose_project_files`.

Esto **no** significa que hoy haya una escalada directa — los adaptadores confinan sus escrituras por convención (nombres `uuid4`, `_SAFE_FILENAME_RE`), y la contención dura la aporta Docker para el código no confiable. Significa que **el mecanismo diseñado para ser la frontera de autorización no lo es**: la seguridad depende de que cada adaptador se acuerde de validar por su cuenta, que es precisamente el patrón que este proyecto critica en su propia documentación.

**Remediación.** `globally_denied` no vacío por defecto para `docker`/`clipboard`/`camera`/`microphone`; quitar el `FILESYSTEM_READ` implícito del `ToolManifest` o dejar de tratarlo como autorización; un nivel de "acción sensible" (write/exec/network) que exija aprobación; y un test de contrato que falle si una herramienta peligrosa no es denegada con el techo por defecto.

### A-6 — La política de secretos en memoria es código muerto, y sus patrones no cubren la realidad

Dos defectos que se refuerzan:

1. **La redacción nunca se ejecuta.** `promote_mid_to_long()` es el único lugar con `classify()`/`redact()` (`manager.py:142-145`). Sus candidatos salen de `candidates_for_promotion()` (`mid_term.py:143-150`), que filtra `repetitions >= 3 AND relevance_score >= 0.75`, con defaults `1` y `0.0`. Verificado por grep exhaustivo: **nada en todo el repositorio incrementa esos campos**. La función siempre devuelve vacío.
2. **Los patrones no cubren los formatos actuales.** Ejecutando `classify()`/`redact()` reales:

| Formato | Detectado |
|---|---|
| `sk-` + 40 alfanuméricos (OpenAI clásico) | ✅ `secret` |
| `sk-proj-…` (**OpenAI actual**) | ❌ `public` |
| `sk-ant-api03-…` (Anthropic) | ❌ `public` |
| `ASIA…` (AWS temporales) | ❌ `public` |
| `github_pat_…` (GitHub fine-grained) | ❌ `public` |
| `xoxb-…` (Slack) | ❌ `public` |
| `sk_live_…` (Stripe) | ❌ `public` |
| `AIzaSy…` (Google) | ❌ `public` |
| `gsk_…` (Groq) · `hf_…` (HuggingFace) | ❌ `public` |

Mientras tanto el mismo secreto se persiste **en claro y de forma garantizada** en `logs/agent.log` vía `logger.info(f"POST /chat: {req.goal!r}")` (`routers/chat.py:114`) — el log tiene 980 KB y 376 conversaciones en este checkout, con permisos 0644.

**Remediación.** Añadir `sk-proj-`, `sk-ant-`, `github_pat_`, `xox[baprs]-`, `sk_live_|rk_live_`, `AIza[0-9A-Za-z_-]{35}`, `gsk_`, `xai-`, `hf_`, `ASIA[0-9A-Z]{16}`, `npm_`, `SG.`, `pypi-`, `glpat-`, más un detector de entropía para cadenas largas sin prefijo; llamar `classify()`/`redact()` en **todos** los puntos de escritura; redactar el `goal` antes de loguearlo (o loguear solo longitud/hash); y hacer la promoción alcanzable o eliminar la política muerta para no dar cobertura falsa.

### A-7 — SSRF autenticado, fuga de API key y HTTP en claro

`POST /settings/llm` acepta `base_url` libre; `_first_chat_capable_model()` hace `GET {base_url}/models` **enviando `Authorization: Bearer <LLM_API_KEY>`** (`openai_compatible_client.py:126-136`). No hay validación de esquema, host ni IP, y `is_unsafe_ip()` — que existe y se usa correctamente en browser/download — **no se aplica acá**. Con el token admin (que cualquier proceso local obtiene de `GET /admin-token`), un `base_url` a `169.254.169.254` o a un servicio interno hace que el servidor haga la petición desde su red y devuelva fragmentos de la respuesta en el mensaje de error. Se acepta `http://` explícito, mandando la key en claro.

### A-8 — Conversaciones completas en el log

`logger.info(f"POST /chat: {req.goal!r}")`. Todo lo que el usuario escribe —incluidas credenciales pegadas "para depurar", que es exactamente el caso que `security_policy.py` dice querer proteger— queda en disco, sin redacción, sin rotación y con permisos por defecto. El `README` afirma que el contenido con credenciales "gets redacted before it's ever kept long-term"; el log lo conserva igual.

### A-9 — La extensión de VS Code ejecuta builds sin confirmación

`vscode-extension/src/androidBuild.ts:138-247`: al recibir un artefacto `android_build_request` (que el modelo puede provocar) ejecuta `./gradlew assembleDebug` en la raíz del proyecto y `adb install -r` en el dispositivo conectado. El único "control" es `ensureSecurityNoticeShownOnce` (`:113-127`), un aviso **no modal** que no espera respuesta: el build ya se disparó. `gradlew`, `build.gradle` y `buildSrc` son código del repositorio → abrir un proyecto Android malicioso y disparar una inyección de prompt equivale a ejecución de código con los privilegios del usuario. La herramienta declara **cero permisos** en su manifest (`vscode_android.py`), así que la cascada del kernel no interviene.

Además `runProcess` usa `spawn(..., { shell: process.platform === "win32" })` con `apkPath` y `packageName` derivados del workspace (`:43,187,207,222`): en Windows es una vía de command injection.

### A-10 — Auditoría forjable y frágil

Dos problemas independientes:

1. **Forja sin autenticación.** `POST /filesystem-access/{id}/report-outcome` y `POST /android-build/{id}/report-outcome` escriben `AuditEvent` sin token, con `outcome` y `files_written` libres. Un atacante puede inyectar eventos, tapar evidencia o inflar el archivo. La cadena de hashes no ayuda: es SHA-256 **sin clave**, así que cualquiera con escritura al archivo recalcula la cadena entera y `verify_chain()` sigue diciendo "íntegra". Es integridad accidental, no autenticidad.
2. **Un renglón corrupto bloquea la auditoría.** `_read_last_hash()` hace `json.loads()` sobre la última línea sin `try`. Verificado: con una línea no-JSON, `record()` lanza `JSONDecodeError` y **todo** evento posterior falla. El propio módulo documenta que existen múltiples escritores concurrentes; una escritura parcial (crash, kill -9) deja el sistema sin auditoría.

---

## 5. Fortalezas verificadas (lo que está bien hecho)

Enumerarlas no es cortesía: es lo que permite acotar el esfuerzo de remediación.

- **Sandbox Docker real y bien construido** (`docker_runner.py:209-231`): `network_mode=none` por defecto, `read_only=True`, `cap_drop=["ALL"]`, `no-new-privileges`, `user=UID:GID` del host (sin el `chmod 0777` histórico), `tmpfs` con `noexec,nosuid`, límites de memoria/CPU/PIDs, y TLS de `extra_mounts`. El contenedor se elimina siempre.
- **`_collect_output_files` endurecido** contra symlinks con `os.lstat` + `resolve()`/`is_relative_to` — el fix correcto, con test (`tests/test_docker_runner_output_collection.py`).
- **Allowlist de red deny-by-default** con re-chequeo de la URL **final** tras redirect y validación de la IP real vía `Response.server_addr()` (`browser.py:385-422`) — mitiga DNS rebinding de forma genuina. `download_manager.py:126-135` aplica el mismo `is_unsafe_ip` post-resolución.
- **Sin CORS permisivo**: no existe `CORSMiddleware` ni cabeceras `Access-Control-*` en todo el repo. Auth por header personalizado ⇒ resistencia estructural a CSRF clásico.
- **Token comparado con `secrets.compare_digest`** (timing-safe), en 0600, generado con `secrets.token_urlsafe(32)`.
- **Sin secretos versionados**: `git ls-files` no lista ningún `.env`, `.pem`, `.key`, `data/` ni `logs/`; `git log --all --diff-filter=A` tampoco. `.env.example` con claves vacías.
- **Firma Ed25519 a dos niveles** (identidad de kal para herramientas dinámicas, identidad del autor para skills), con re-verificación **fresca en cada `execute()`** de skill (`sandboxed_skill.py:191-198`) — cierra correctamente el TOCTOU de carga vs. ejecución.
- **Sin `yaml.load` inseguro**: 100 % `yaml.safe_load`. **Sin `eval`/`exec`/`pickle`** en código de producción salvo un único `joblib.load` (M-10).
- **Path traversal bien resuelto** donde se trató: `self_modification._safe_join` (con `PathTraversalError`), `orchestrator._artifact_url` (`.resolve()` antes de `relative_to`), `versioning._tool_dir`, `skill_creator` (`^[a-z][a-z0-9_]*$`), `sandboxed_skill` (K-1).
- **Escrituras de adaptadores confinadas** por convención sólida: nombres `uuid4`, `_SAFE_FILENAME_RE` en `text_file.py`, sin `shell=True` ni interpolación de comandos en ningún adaptador (verificado: ffmpeg se usa vía API Python de moviepy, no por shell).
- **`json_extraction` sin `eval`**, solo `json.loads`.
- **Frontend sin XSS por contenido del modelo**: `renderMarkdownLite` escapa `&`, `<`, `>` **antes** de aplicar markdown; la única asignación de `innerHTML` con datos del servidor (`app.js:479`) pasa por esa función; el resto de los sinks usan `textContent`. Sin `eval`, `new Function`, `document.write`, `postMessage` ni WebSockets.
- **Extensión VS Code**: webview con CSP estricta y nonce, todo con `textContent`, `validateRelativeFilePath` + `isWithinRoot` + confirmación modal para escribir archivos, sin secretos almacenados, `package.json` sin `activationEvents: *`.
- **Aprovechamiento honesto de sus propios incidentes**: los comentarios documentan vulnerabilidades reales corregidas (K-1…K-6, DNS rebinding, race del log de auditoría, `chmod 0777`, `docker run` sin timeout) con el razonamiento completo. Esa trazabilidad es un activo real.
- **`ImportErrorStrategy`** con `--only-binary :all:` y gate de aprobación humana para la excepción de red.

---

## 6. Plan de remediación priorizado

**Bloque 0 — hoy (impacto máximo, esfuerzo mínimo)**
1. Crear `.dockerignore` y **rotar** `tool_signing_key` + `admin_token` (C-1).
2. `scripts/run_kal.sh`: `--host 127.0.0.1` (C-5).
3. Derivar la extensión de `/uploads` del `content_type` validado; `X-Content-Type-Options: nosniff` + `Content-Disposition: attachment` en `/artifacts` (C-2).
4. `require_admin_token` en `/memory/*`, `/filesystem-access/*/report-outcome`, `/android-build/*/report-outcome`, y token compartido en `sandbox_api /execute` (C-3, C-4, A-10).
5. `_collect_skill_files`: `os.lstat` + descartar no-regulares (A-1).

**Bloque 1 — esta semana**
6. Sacar el token admin de `localStorage` y de la query string; no imprimirlo en logs; añadir CSP, `X-Frame-Options: DENY`, `Referrer-Policy` (M-1, M-2).
7. `classify()`/`redact()` en `remember()` y `consolidate_short_to_mid()`; ampliar los patrones de credenciales; dejar de loguear el `goal` crudo; arreglar `_read_last_hash` con `try/except` (A-6, A-8, A-10).
8. Mover el contexto del editor y la salida de herramientas fuera del rol `system`, con delimitadores y topes de tamaño; exigir `tool_calls` nativo para herramientas con efectos (A-2, A-3, B-1).
9. Confirmación modal bloqueante antes de `gradlew`/`adb install`; quitar `shell: true` (A-9).
10. Validar `base_url` del LLM con `is_unsafe_ip` + exigir `https` (A-7); escapar `.env`/YAML con serializador (M-5).

**Bloque 2 — hardening estructural**
11. Hacer la cascada de permisos efectiva: `globally_denied` por defecto, quitar el `FILESYSTEM_READ` implícito, test de contrato (A-5).
12. `ShortTermMemory` por sesión + provenance `EXTERNA` propagada (A-4, M-3).
13. Lockfile con hashes; actions pinneadas por SHA; digests en imágenes; `.joblib` a formato sin ejecución o con hash verificado; limpieza de `sessions`/`tasks`; `top_k`/`n` acotados; `run_in_threadpool` en `/transcribe` (M-7, M-9, M-10).
14. Autenticar el proxy Docker y aislar `sandbox_runner` en su propia red; firmar (HMAC) el log de auditoría (M-11, M-12).

---

## 7. Alcance, método y limitaciones

- **Cubierto en profundidad:** `kernel/permissions/*`, `kernel/lifecycle/*` (docker_runner, executor, skill_runner, ebpf), `kernel/registry/*` (registry, skills, sandboxed_skill, signer, versioning), `kernel/api/*`, `audit/`, `code_analysis/`, `agent_core/routers/*`, `agent_core/orchestrator.py`, `agent_core/sessions.py`, `agent_core/llm/*`, `agent_core/memory/*`, `agent_core/context_service.py`, `agent_core/self_modification.py`, `agent_core/llm_settings.py`, `agent_core/skill_creator.py`, `tool_integration/` (adapters, services, download_manager, malware_scan), `frontend/`, `vscode-extension/src/`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/`, `requirements*.txt`, `.env.example`, `.gitignore`, `scripts/`.
- **No cubierto en profundidad (declarado para honestidad):** los adaptadores multimodales de generación pura (`image_gen`, `audio_gen`, `video_gen`, `image_composition`, `ocr`, `speech_to_text`) se revisaron de forma dirigida en busca de shells, rutas y URLs, pero no línea por línea; `task_execution/` y `error_handling/` se revisaron por encima de forma dirigida; `utils/config.py` (641 líneas de esquema Pydantic) no se auditó completo.
- **Verificado empíricamente:** lectura por symlink en `_collect_skill_files` (con `/etc/passwd` y `/dev/zero`); detección de credenciales de `classify()`/`redact()` contra 10 formatos reales; fragilidad de `_read_last_hash` ante línea no-JSON; `TypeError` de `compare_digest` con string no-ASCII; contenido de `data/keys/` y ausencia de `.dockerignore`; `git ls-files` / `git log --all` en busca de secretos.
- **Marcado como SOSPECHADO (requiere verificación en runtime):** el bypass de `TrustedHost` con `Host: localhost` desde un cliente crudo (deducido del comportamiento estándar de Starlette, no probado end-to-end); `get_admin_token_endpoint` confiando en `request.client.host` si hay un proxy/túnel en la misma máquina; el `TypeError` de M-6 verificado a nivel `h11`+`secrets`, no contra un uvicorn real.
- **Todo el análisis fue de solo lectura.** Ningún archivo del proyecto fue modificado.

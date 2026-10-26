# Qué se trabaja en cada repositorio

Este archivo es el **autoritativo para kal-in**. Lo que hay que hacer en `kal` se anota en
`kal/docs/PENDIENTE.md`, y lo de Likay-OS, en el repo de Likay: cada espacio lleva su propia
lista para que no haya dos copias que se desincronicen.

Regla operativa (ver `CONTRIBUTING.es.md`, "Commits que no mezclan dos casos"):

- **Un caso, un repo.** Si toca `kernel/`, `sdk/`, `audit/` o `code_analysis/`, son dos
  commits: primitiva en `kal` primero, port acá segundo, y el mensaje del segundo cita el
  hash del primero.
- **Rutas explícitas, nunca `git add -A`.**
- Antes de commitear: `python3 scripts/estado_de_repos.py --check` (sale 1 si algún repo
  tiene archivos compartidos sin commitear).

## Lo que corresponde a kal-in (acá)

| Ámbito | Archivos |
|---|---|
| El agente y su host | `agent_core/` (ruta `/chat`, cliente del LLM, catálogo de tools, sesiones, memoria, capability broker) |
| Medición y calibración | `agent_core/epistemics/measurement/`, `scripts/measure_epistemics.py` |
| Arranque y operación | `scripts/run_kal.sh`, `scripts/estado_de_repos.py`, `scripts/install_git_hooks.py`, `scripts/hooks/` |
| Documentación del host | `README.md`, `CONTRIBUTING.*`, `docs/` (salvo lo que sea del kernel) |

## Lo que NO se hace acá (va a `kal`)

| Si el cambio está en… | Va a | Y vuelve acá como |
|---|---|---|
| `kernel/`, `sdk/`, `audit/`, `code_analysis/` | `kal` (primitiva) | port con el hash citado |
| Una skill que existe en ambos repos | `kal` si es de las compartidas | port |
| Política de producto de Likay-OS (su UI, su pin, qué modelos permite) | Likay | — |

## Pendiente en kal-in, en orden

1. **Test del invariante de CORS** (`agent_core/orchestrator.py`): hoy no hay
   `CORSMiddleware`, y eso protege — la API solo acepta JSON y los endpoints admin piden
   header propio, así que una página cross-origin no pasa el preflight (DNS rebinding
   bloqueado por accidente). El día que alguien agregue `allow_origins=["*"]` para que
   funcione el frontend, abre justo esa puerta. El test tiene que fallar si aparece un
   comodín.
2. **Piso de versión de Ollama** (`scripts/run_kal.sh`): hoy avisa si Ollama no responde,
   no si es viejo. `Bleeding Llama` afecta a todo lo anterior a 0.17.1 y el updater sin
   firma a lo anterior a 0.23.3. Aviso, no bloqueo.
3. **Huella de la plantilla de chat** — *bloqueado en `kal`*: necesita el tipo de evento de
   auditoría (archivo compartido) antes de la parte del host. Cuando esté, acá va la
   llamada a `/api/show` + hash + registro por sesión, con aviso si cambia a mitad de
   sesión.
4. **Allowlist de claves de autor** — *bloqueado en `kal`*: la primitiva va en
   `kernel/registry/skill_signing.py` y `scripts/install_from_market.py`. Acá queda la
   **política**: qué claves aceptamos y el test de "clave desconocida → rechazo auditado".

## Lo que ya está cerrado (para no repetirlo)

- `audit_log.record()` dejó de ser O(tamaño del log) — port B-8 desde kal (`2d59b41`).
- Los 4 archivos con "drift de código real" investigados: ninguno requería port hacia acá
  (`f301a5b`).
- Denegaciones humanas, apagado de tools dinámicas y auditoría de manipulación: cerradas
  acá, **pendientes de port hacia `kal`** (ver la lista de kal).

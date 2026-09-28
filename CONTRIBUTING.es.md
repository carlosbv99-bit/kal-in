# Contribuir con Kal-in

🇬🇧 [English](CONTRIBUTING.md) | 🇪🇸 Español

> Este repo es **kal-in** — el agente de referencia de kal, construido
> sobre el kernel kal (ahora un repo separado,
> [carlosbv99-bit/kal](https://github.com/carlosbv99-bit/kal)). Este
> repo todavía trae su propia copia del kernel en vez de depender de
> ese paquete — ver la nota al principio de [README.es.md](README.es.md).

Hay dos cosas distintas que podés querer contribuir: **código**
(`kernel/`, `sdk/`, `agent_core/`, `tool_integration/`, tests), o una
**Skill** al Skill Market. Tienen flujos distintos, cubiertos en las
dos secciones de abajo.

## Contribuir código

1. Forkeá el repo y clonalo localmente.
2. Armá un virtualenv e instalá las dependencias:
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements-core.txt -r requirements-dev.txt
   ```
   `requirements-multimodal.txt` (diffusers, faster-whisper, piper-tts,
   playwright, etc. — varios GB) solo hace falta si tocás código de
   imagen/audio/video/STT/browser específicamente; la mayoría de la
   suite corre sin eso (esos tests se saltan solos vía
   `pytest.importorskip(...)` cuando falta).
3. Corré los tests:
   ```
   python -m pytest tests/ -q
   ```
   Es el mismo comando que corre la CI. Un puñado de tests necesitan
   Docker corriendo (`requires_docker` en `tests/conftest.py`) o una
   instancia real de Ollama (`tests/test_*_integration.py`) — esos se
   saltan solos automáticamente cuando la dependencia no está
   disponible.
4. Lint (el mismo chequeo que aplica la CI — errores reales, no una
   opinión de estilo):
   ```
   python -m ruff check --select=E9,F .
   ```
5. Abrí un pull request contra `main`.

**Dónde vive cada cosa**, si no estás seguro dónde entra un cambio:
- `kernel/` — sandboxing, permisos, el registro de Skills, el Kernel
  Bus. Infraestructura de seguridad pura: sin LLM, sin ML, sin lógica
  de agente. Cero dependencia de `agent_core/`/`tool_integration/` —
  es deliberado, mantenelo así (verificado con grep, no solo por
  convención).
- `sdk/` — la API pública que importa una Skill (`Tool`,
  `ToolManifest`, `Artifact`, `Permission`, `call()`). 100% stdlib a
  propósito: este paquete se copia tal cual dentro del contenedor
  Docker de cada Skill (ver `kernel/registry/sandboxed_skill.py`), así
  que nunca puede ganar una dependencia que no esté ya dentro del
  contenedor.
- `agent_core/` — kal-in en sí: el loop del agente LLM, el
  orquestador, la memoria, el Conversation Engine. Depende de
  `kernel/`/`sdk/`, nunca al revés.
- `tool_integration/` — las herramientas concretas que usa un agente
  (generación de imagen/audio/video, browser, integración con VS
  Code) más los Kernel Services (`tool_integration/services.py`) que
  sostienen al Kernel Bus — capacidad de agente, no mecanismo de
  kernel, por eso vive fuera de `kernel/`.
- `tests/` — refleja el módulo que testea (`test_agent_loop.py` →
  `agent_core/llm/agent_loop.py`, etc.). Un sufijo `*_integration.py`
  significa que necesita un servicio real (Ollama, Docker) y se salta
  solo si no está disponible.

Un PR que cambia comportamiento debería venir con un test que hubiera
fallado antes del cambio — `docs/HISTORY.md` de este repo es un diario
largo y honesto de bugs reales encontrados en uso real, cada uno con
el test que ahora lo cubre; ese es el estándar que se espera de una
contribución nueva, no cobertura del 100% por sí misma.

Si tu cambio es chico y bien acotado, buscá un issue etiquetado
**good first issue** — están elegidos para entenderse sin tener que
leer todo el código primero.

## Contribuir con una Skill

El Skill Market de Kal-in ([explorarlo acá](https://carlosbv99-bit.github.io/kal-in/))
es la carpeta `skills/` de este repositorio. Publicar una Skill
significa abrir un pull request contra ella.

### Cómo publicar

1. Forkeá este repo, agregá tu Skill en `skills/<nombre-de-tu-skill>/`
   (`skill.yaml` + tu código — mirá cualquier skill existente en
   `skills/` para el formato del manifiesto).
2. Firmala con tu **propio** keypair, nunca el de otra persona:
   ```
   python3 scripts/sign_skill.py skills/<nombre-de-tu-skill>/ --key-dir <tu-directorio-de-claves>
   ```
   Guardá `<tu-directorio-de-claves>` en un lugar persistente — firmar
   una versión futura con el mismo directorio la atribuye al mismo
   autor.
3. Abrí un pull request.

### Qué se chequea automáticamente, y qué no

Un check de CI (`scripts/validate_skills.py`) corre en cada pull
request y bloquea el merge hasta que pase. Verifica **únicamente la
integridad del paquete**:
- Que tu `skill.yaml` parsea correctamente.
- Que tu `skill.sig` está presente y verifica criptográficamente
  contra el contenido actual de la carpeta de tu Skill.

**No** chequea, ni puede chequear:
- Si tu código hace lo que la descripción dice.
- Si los permisos que declaraste tienen sentido para lo que la Skill
  realmente hace.
- Si la Skill es segura, está bien escrita, o es maliciosa.

Una firma válida prueba que el paquete no fue alterado desde que lo
firmaste — no dice nada sobre si el contenido debería ser confiable.
Por eso cada pull request también recibe una **revisión manual de un
mantenedor** antes de mergear, hoy enteramente un juicio humano, no
automatizado. Esto es un cuello de botella real al tamaño actual de
este proyecto, no una solución que escale — puede evolucionar a medida
que la comunidad crezca.

### Sandbox local, no una API con ruedas de entrenamiento

Las Skills siempre corren en un contenedor Docker efímero y aislado
por cada llamada — sin red, filesystem de solo lectura, non-root, sin
acceso permanente a nada fuera de `/workspace` — sin importar cómo
fueron instaladas. Ver [README.es.md](README.es.md) para la
arquitectura completa.

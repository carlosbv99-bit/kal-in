# Kal-in

🇬🇧 [English](README.md) | 🇪🇸 Español

> **Kal-in** (antes este repo era simplemente `kal`) es el agente de
> referencia de kal, construido sobre el kernel kal. El kernel puro
> (sin agente, sin ML — Access Manager, sandbox, audit log, Kernel
> Service Bus) ahora vive en su propio repo,
> [carlosbv99-bit/kal](https://github.com/carlosbv99-bit/kal), para que
> otros agentes puedan usarlo de forma independiente (ver
> [Likay-OS](https://github.com/Kevindelb/Likay-OS) para el primer caso
> de uso). Este README describe todo el código actual de kal-in, que
> todavía trae su propia copia del kernel en vez de depender del
> paquete `kal` separado — esa migración es trabajo futuro, contado aquí
> con honestidad en vez de disimulado.

**Un microkernel seguro para capacidades inteligentes.**

Dale a un LLM la capacidad de ejecutar código, instalar un plugin, o
tocar su propio código fuente, y le diste la capacidad de hacer daño
de verdad en el momento en que algo sale mal — una llamada a
herramienta mal hecha, una dependencia envenenada, una inyección de
prompt que lo convence de hacer algo que no debería. Kal-in está
construido alrededor de ese riesgo específico, no alrededor de confiar
en que el modelo se porte bien: cada Skill corre en un contenedor
Docker aislado y non-root sin importar de dónde vino, una cascada de
permisos por niveles decide qué puede tocar cualquier pieza de código,
el contenido de memoria que coincide con patrones de credenciales
conocidos se redacta antes de persistir a largo plazo, y la
auto-modificación requiere aprobación humana explícita antes de que
nada llegue a disco. Ver [Seguridad primero](#seguridad-primero) más
abajo para exactamente qué cubre esto, y qué no.

La mayoría de los asistentes de IA acoplan sus funcionalidades a un
modelo y proveedor específicos. Kal-in separa las capacidades
inteligentes de los motores de IA subyacentes mediante una
arquitectura de microkernel segura: las capacidades son **Skills**
aisladas y sandboxeadas que hablan con el kernel a través de un
protocolo estable, nunca directamente con un modelo concreto — así,
una Skill escrita hoy sigue funcionando cuando el modelo detrás de
ella cambia mañana.

Kal-in es local-first (Ollama, o cualquier endpoint compatible con
OpenAI — sin necesidad de GPU, todo funciona primero en CPU) y de
código abierto ([Apache 2.0](LICENSE)).

```
                          Usuario
                           │
          Extensión de VS Code / Frontend web
                           │
              Kernel (agent_core/orchestrator.py)
   ─────────────────────────────────────────────────
    Cascada de Permisos   Registro de Herram.   Audit Log
    Kernel Bus            Sandbox               Circuit Breaker
   ─────────────────────────────────────────────────
    Kernel Services: imagen · audio · voz-a-texto
   ─────────────────────────────────────────────────
    Skills (sandboxeadas, sin confianza permanente)
```

## ¿Por qué Kal-in?

| Asistentes de IA tradicionales        | Kal-in                                                            |
|-----------------------------------------|-------------------------------------------------------------------|
| Acoplados a un solo modelo              | Agnóstico al modelo — local (Ollama) o cualquier endpoint compatible con OpenAI |
| Capacidades integradas en la app        | Cada capacidad es una **Skill**, cargada de forma independiente   |
| Los plugins tienen acceso interno directo | Las Skills corren en un contenedor Docker efímero: sin red, filesystem de solo lectura, non-root, `cap_drop=ALL` por defecto |
| Extensibilidad ad-hoc / sin documentar  | Las Skills declaran un manifiesto (permisos, dependencias, servicios del kernel) verificado antes de correr |
| Generalmente cloud-first                | Local-first — pipelines de ML solo-CPU, sin necesidad de GPU ni API key |

## Arquitectura

- **Kernel** (`agent_core/`) — coordina el loop de conversación del
  LLM, los permisos, el sandboxing y la auditoría. No implementa
  capacidades de IA por sí mismo.
- **Kernel Services** (`kernel/services/services.py`) — servicios
  compartidos y persistentes que mantienen un recurso pesado (un
  modelo de ML cargado) para que nunca se recargue en cada llamada.
  Hoy: generación de imágenes, inpainting de imágenes, síntesis de
  audio, voz-a-texto.
- **Skills** (`skills/`) — capacidades sandboxeadas. Una Skill nunca
  carga un modelo ni toca el filesystem/red directamente; le pide a un
  Kernel Service lo que necesita a través del Kernel Bus, mediante el
  **SDK** oficial (`sdk/`) — nunca una ruta interna del kernel.
- **Kernel Bus** (`kernel/api/`) — el protocolo JSON-RPC (sobre un
  socket Unix, nunca un puerto de red) que le permite a una Skill
  sandboxeada llamar a un Kernel Service sin salir nunca de su
  contenedor.

## Seguridad primero

- Las Skills se ejecutan en un contenedor Docker efímero por llamada:
  sin red por defecto, filesystem de solo lectura fuera de
  `/workspace`, usuario non-root, `cap_drop=ALL`, límites de recursos.
- Una cascada de permisos por niveles gobierna todo lo que puede hacer
  una pieza de código — herramientas de primera parte, herramientas
  dinámicas propuestas por el agente, y Skills, cada una en un techo de
  confianza distinto, decidido por *cómo* se registró el código, nunca
  por lo que declara sobre sí mismo.
- El análisis estático AST es una primera línea de defensa para
  herramientas creadas dinámicamente y propuestas de auto-modificación
  — un filtro barato, no el límite de seguridad real (eso es el
  sandbox).
- Toda acción sensible se registra en un audit log append-only y
  encadenado por hash — alterar una entrada pasada rompe la cadena de
  forma visible.
- El contenido de memoria que coincide con patrones de credenciales
  conocidos (API keys, tokens) se redacta antes de persistir a largo
  plazo; el contenido nunca se comparte con un proveedor de LLM en la
  nube salvo que esté explícitamente marcado como compartible,
  chequeado al recuperarlo sin importar el nivel de memoria.
- Los paquetes de Skills están firmados (Ed25519) y se verifican antes
  de cargarlos — un paquete alterado se rechaza directamente,
  fail-closed.
- Instalar una Skill desde un market remoto **requiere** una firma
  válida — sin excepciones. Integridad, no confianza en el autor: una
  política de publicación curada es un próximo paso deliberado, todavía
  no construido.
- La auto-modificación (el agente proponiendo un cambio a su propio
  código) está deshabilitada por defecto, requiere aprobación humana
  explícita antes de tocar disco, y está permanentemente bloqueada para
  los módulos centrales del kernel.

## Estado del proyecto

**Implementado**
- Ejecución sandboxeada de Skills (aislamiento Docker, deny-by-default)
- Cascada de permisos por niveles
- Kernel Bus + 4 Kernel Services reales (generación/inpainting de
  imágenes, síntesis de audio, voz-a-texto)
- Firma de paquetes de Skills (Ed25519) + instalación local guiada
- Instalación remota de Skills desde un market basado en Git, con
  verificación de firma obligatoria
- Audit log append-only, encadenado por hash
- Memoria de tres niveles (corto/mediano/largo plazo)
- Pipeline de auto-modificación (con aprobación humana obligatoria,
  deshabilitado por defecto)
- Observabilidad a nivel de syscall vía eBPF (Linux)
- Extensión de VS Code (chat dentro del editor)
- SDK oficial de Skills (`sdk/`) — lo único que una Skill importa, 100%
  stdlib, independiente del resto del kernel
- Access Manager unificado (permisos) — filesystem y red comparten un
  único motor genérico de concesión/aprobación en vez de dos paralelos

**En progreso**
- Cobertura más amplia de Kernel Services (solo 4 hoy — browser/OCR
  siguen siendo adaptadores directos, todavía no Kernel Services)

**Planeado**
- Un market navegable (sitio estático) sobre el mismo catálogo basado
  en Git
- Una política de publicación curada/revisada para el market
- Una capa de observabilidad equivalente para Windows (no hay eBPF
  fuera de Linux)

## Roadmap

1. Asistente de IA (proceso único, LLM local) — ✓
2. Herramientas multimodales + ejecución sandboxeada real — ✓
3. Pivote al Kernel — Skills como plugins aislados, zero-trust — ✓
4. Kernel Bus — servicios compartidos para Skills — ✓
5. Integridad de paquetes + instalación guiada (firma) — ✓
6. Comunidad — repo público, instalación remota desde un market — ✓
7. Market navegable + publicación curada — siguiente
8. Cobertura más amplia de Kernel Services, SDK más rico — futuro

## Visión

Creemos que las capacidades inteligentes no deberían estar atadas a un
solo modelo, proveedor, o aplicación monolítica. Kal-in es un microkernel
seguro donde los desarrolladores construyen Skills en vez de
integraciones puntuales — el modelo, el almacenamiento, el proveedor
de IA específico son detalles intercambiables detrás de un límite
estable, nunca supuestos incrustados en cada herramienta. Así como un
kernel de sistema operativo habilitó un ecosistema de aplicaciones
independientes, Kal-in apunta a habilitar un ecosistema de Skills de IA
independientes y confiables.

## Cómo empezar

- **[Explorar el Skill Market](https://carlosbv99-bit.github.io/kal/)** — mira qué hay disponible antes de instalar nada.
- `scripts/run_kal.sh` — correr kal-in localmente.
- `scripts/enable_skill.py` — instalar una Skill desde una carpeta local.
- `scripts/install_from_market.py --list` — explorar e instalar una
  Skill desde el market basado en Git (por defecto, este mismo repo).
- `scripts/sign_skill.py` — firmar una Skill que hayas escrito.

## Documentación

El historial de ingeniería detallado de este proyecto — cada fase,
decisión de diseño, y bug real encontrado en el camino, en español —
vive en [docs/HISTORY.md](docs/HISTORY.md).

## Cómo colaborar

Es un proyecto joven, en desarrollo activo, y hay lugar de verdad para
aportar — no hace falta ser un especialista en seguridad para tener
algo útil que sumar. Código, revisión de seguridad, probarlo en
hardware o configuraciones que aquí no probamos, e incluso simplemente
explicarle esto a gente que no programa, todo eso es genuinamente
útil.

- Empieza por [`CONTRIBUTING.md`](CONTRIBUTING.es.md) — tiene el
  setup, la estructura del repo, y el criterio con el que se evalúa
  cada cambio.
- Abre un [issue](https://github.com/carlosbv99-bit/kal-in/issues)
  para proponer algo, reportar un bug, o simplemente decir que quieres
  ayudar.
- Si quieres escribir o hablar sobre este proyecto, este README y el
  código mismo son la fuente primaria — cada afirmación aquí está
  pensada para poder verificarse contra lo que realmente hay en el
  repo. Ese mismo nivel de minuciosidad es el método diario de
  trabajo: cada cambio se revisa y se verifica en coordinación
  permanente con Claude (Anthropic), no solo se documenta después.

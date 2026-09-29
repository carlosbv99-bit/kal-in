# Roadmap — Capa epistémica (hipótesis conservadas, procedencia y "no lo sé")

> **Estado:** F0 (contratos) y F1 (matcher determinista + ledger en memoria +
> abstención estable) **implementados** como subpaquete `kal_epistemics/` de
> `kal-in` — stdlib puro, sin dependencias del host (verificado con tests, no
> declarado), y endurecido contra input no confiable ANTES de exponerlo al
> modelo. **F2 completa** (las cuatro rebanadas), y **F6 completa** (medición y
> calibración: corpus A/B/C/R/D + 41 respuestas etiquetadas, calibración de los
> marcadores de hedge y del umbral, reporte generado y guarda de regresión).
> **F3 completa** (ancla + reconciliación del drift) y **F4 completa** (D4:
> SQLite propio; D5: ámbito por instalación): el ledger sobrevive al proceso, la
> misma pregunta en otra sesión cae en el mismo registro, el estado se compacta
> por presupuesto, y las reaperturas tienen presupuesto por hipótesis **y por
> pregunta** (R8). **F5 completa** (skill `claim_verifier`, de primera parte por
> D6, firmada y con permisos de red explícitos): el sistema ya puede salir a
> buscar evidencia exógena en vez de esperar a que pase por las herramientas
> existentes. **F2b completa** (fachada advisory para terceros, con la librería
> vendorizada adentro y sus límites declarados en cada respuesta) y **F7 completa**
> (la costura entre seguridad del contenedor e integridad epistemológica, con el
> arreglo general del bug de composición que encontró F5). **F8** (introspección,
> alternativas incompatibles y validez temporal) llegó después, rescatada de una
> revisión de literatura, y está **completa**; con ella el esquema pasó a v2 con
> lectura de v1. D2 (que el vencimiento dispare una acción) queda esperando
> medición, que es lo único que la justificaría.
>
> Los dos hallazgos de la medición quedaron cerrados: D10 (el orden de las
> condiciones dejó de importar) el mismo día, y D9 (el déficit que la tool
> declaraba sin condición de pago) al resolver el backend de F4.
>
> **Medición (F6), en una línea:** 10/12 de lo imposible no sale como hecho,
> 0/12 de abstención falsa sobre lo verificable, 0 hipótesis auto-sostenidas,
> 27/27 evidencias ancladas al audit log, y el heurístico de hedge calibrado de
> 0.78 a 0.97 de exactitud sobre el conjunto etiquetado. Reporte completo, con
> sus límites: `docs/MEDICION-CAPA-EPISTEMICA.md`; se regenera con
> `python scripts/measure_epistemics.py --escribir`.
>
> **Ubicación (decidida):** el código vive en **`kal-in/kal_epistemics/`**, como
> subpaquete de nivel superior con la misma disciplina que `sdk/` — 100% stdlib
> y sin importar nada del host, de modo que se puede copiar a otro agente tal
> cual, y extraer a su propio repo el día que haga falta sin tocar una línea.
> La fachada Skill para agentes de terceros vive en `kal-in/skills/`. El ancla
> de procedencia (`event_id` → registro verificable) es lo único que toca
> `kal`, y nada de esta capa es scope del kernel. **Este documento vive acá,
> en `kal-in/docs/`**, porque describe al agente y no al kernel.
>
> **Origen:** conversación de diseño sobre alucinaciones en modelos actuales
> (2026-09). El núcleo acordado: *"no lo sé" es un estado almacenado, no un
> fallo*; una hipótesis abierta es una **deuda con condición de pago**, no un
> texto guardado.

---

## 0. Principios no negociables

Estos siete principios resuelven, por sí solos, la mayoría de las decisiones
de implementación que siguen. Si un entregable los contradice, está mal.

1. **Endógeno ≠ exógeno.** Lo que el modelo escribió tiene peso 0 como
   evidencia, siempre. Lo que aporta valor es la observación mediada por el
   kernel y, con menor fuerza, el texto externo traído por una herramienta
   (que es exógeno pero **no es verdad**: puede mentir y puede inyectar).
2. **La procedencia la produce el kernel, no la narra el modelo.** Un
   `reasoning_chain` redactado por el modelo es teatro epistémico. Un
   `event_id` que resuelve a un registro encadenado por hash, no.
3. **El gate no puede ser salteable.** Debe estar en la ruta de salida, no
   ser una tool que el modelo decide llamar. Un gate que el modelo puede
   saltear no es un gate.
4. **La repetición no es evidencia.** Guardar, recuperar y volver a guardar
   una hipótesis 50 veces no la refuerza. Ni la antigüedad, ni haber
   "sobrevivido" sin ser refutada.
5. **Contratos ahora, inteligencia después.** Doctrina ya establecida en
   `kal-in/docs/HISTORY.md` (modelo Visión → Arquitectura → Infraestructura →
   Funcionalidad): los contratos y eventos que la inteligencia futura va a
   necesitar se preparan ya, porque son lo más caro de cambiar después; los
   umbrales, redacciones y heurísticas esperan datos reales.
6. **Validación sintáctica ≠ validación de seguridad ≠ validación epistémica.**
   Son ejes independientes y ninguno sustituye a otro: una hipótesis puede ser
   sintácticamente válida, segura de almacenar y epistemológicamente absurda —
   y eso no es un error del parser. En el código son tres mecanismos distintos
   (`__post_init__` de los dataclasses, `safety.py`, `ledger.validate()`), y el
   error clásico es creer que "el parser lo aceptó" significa "está
   justificado".
7. **Todo lo que entra desde el modelo es no confiable DE ORIGEN, aunque sea
   estructurado.** Un campo `source`, una palabra clave o una descripción no son
   más seguros por estar en un campo tipado: su procedencia sigue siendo el
   modelo. Y hay un caso que va más allá de la seguridad — si una herramienta
   que devuelve texto del propio agente se ingiere como observación, el modelo
   **lava su origen**: guarda una conjetura, la recupera, y su propio texto
   entra al ledger como observación del kernel. Ese lazo está cerrado por
   allowlist y hay un test que lo verifica.


### Seguridad primero: la superficie de ataque de esta capa

El proyecto es seguridad primero, y esta capa introduce una superficie nueva.
Va nombrada antes de abrirla, no después:

1. **Regex sobre texto no confiable = ReDoS.** Las condiciones de reapertura
   llevan patrones y el matcher corre en el camino crítico en cada evidencia
   ingerida. Una inyección de prompt puede hacer que el modelo registre un
   patrón catastrófico (`(a+)+$`) que cuelga al agente. Mitigación
   **estructural**: el camino no confiable no acepta regex en absoluto
   (`ReopenCondition.from_untrusted()` solo toma selectores enumerados y
   substrings); los campos de regex quedan para condiciones escritas por código
   o por un humano, con tope de longitud y rechazo de cuantificadores anidados,
   y fallan cerrado si no se pueden analizar.
2. **Contenido externo = dato, nunca instrucción** (§0), reforzado con el tope
   de escaneo.
3. **Crecimiento del ledger = agotamiento de recursos.** Topes explícitos
   (`safety.py`) que fallan cerrado con `LedgerLimitExceeded`.
4. **El gate falla cerrado, no abierto.** Cualquier error en el camino de
   decisión termina en "no emitir", nunca en "emitir igual". La librería **no
   traga excepciones** (un bug tiene que ser ruidoso); el fail-closed se impone
   en el gate, que es donde el error se convertiría en silencio.
5. **Evidencia que vuelve al contexto.** Si el contenido de una evidencia se
   reinyecta en el prompt, tiene que pasar por la política de memoria que ya
   existe (`agent_core/memory/security_policy.py`: clasificación + redacción de
   credenciales), o el ledger se vuelve un canal de exfiltración de secretos
   hacia el proveedor de LLM.
6. **Admisión ≠ creencia.** `EvidenceStatus` separa "¿puedo dejar que esto
   entre al razonamiento?" de "¿cuánto debo creerlo?", porque la respuesta
   puede ser *sí puedo almacenarlo, no debo creerlo*. Lo puesto en cuarentena
   se conserva y **se puede vincular** — para no perder la trazabilidad de que
   una hipótesis se apoyó en material hoy cuestionado — pero no sostiene, no
   contradice y no reabre: se **marca** con `Flag.RESTING_ON_QUARANTINED` en
   vez de borrarse. Y un **presupuesto de reaperturas por hipótesis** acota el
   amplificador de ruido de una condición genérica, sin depender de que la
   palabra clave que escribió el modelo sea buena.

> Lección registrada en `HISTORY.md`: la primera versión del detector de ReDoS
> **no detectaba nada** (`SubPattern` de `re` itera por `__getitem__`, no por
> `__iter__`). El control existía, se veía bien y no hacía nada — la peor clase
> de fallo, porque produce confianza sin cobertura. Lo atraparon los tests.

---

## 1. Arquitectura objetivo

```
                        ┌──────────────────────────────────────┐
                        │  kal-in/kal_epistemics/ (stdlib)     │
                        │  Evidence · Hypothesis · Question    │
                        │  ReopenCondition · HypothesisLedger  │
                        │  should_reopen() · render_abstention │
                        └───────────────┬──────────────────────┘
                                        │ consumido por
                        ┌───────────────▼──────────────────────┐
                        │  kal-in (agente)                     │
                        │  · gate en la ruta de salida final   │
                        │  · tools: open_question /            │
                        │    record_hypothesis                 │
                        │  · bloque "expediente abierto"       │
                        │  · adaptador de eventos del kernel   │
                        └───────┬───────────────────────┬──────┘
                                │                       │
              ┌─────────────────▼──────┐   ┌────────────▼─────────────┐
              │  kal (kernel)          │   │  Skill claim_verifier    │
              │  event_id → registro   │   │  (sandbox, permisos,     │
              │  verificable           │   │   firmada) → evidencia   │
              │  (procedencia          │   │   exógena con procedencia│
              │   infalsificable)      │   └──────────────────────────┘
              └────────────────────────┘
```

**Reparto y por qué** (detalle en la conversación de diseño):

| Pieza | Forma | Hogar |
|---|---|---|
| Contratos + lógica determinista | subpaquete stdlib, sin deps del host | `kal-in/kal_epistemics/` |
| Gate, tools, política de prompt | código de agente | `kal-in/agent_core/epistemics/` |
| `event_id` → registro verificable | mecanismo | `kal` |
| Verificación de claims | Skill | marketplace |
| Fachada para agentes de terceros (**advisory**) | Skill | `kal-in/skills/` |

**Naming:** el paquete y el módulo **no** se llaman "Knowledge" nada. En
`kal-in/agent_core/knowledge/__init__.py` ya hay una advertencia explícita
sobre dos componentes de nombre parecido que se confunden entre sí
("Knowledge Service" vs Knowledge Base/Miner); un tercero garantiza
confusión. Quedó **`kal_epistemics/`** con `HypothesisLedger`, y el adaptador en
`agent_core/epistemics/`.


### La fachada Skill: qué puede y qué no

Se pidió que esto sea integrable como Skill en kal-in o en cualquier otro
agente. Se puede, con una distinción que tiene que quedar escrita para que
nadie crea que compró una garantía que no es:

- **La Skill es advisory.** Una Skill corre en un contenedor, la invoca el
  agente, y no ve el loop de inferencia ni la ruta de salida. Un agente de
  terceros que la adopte obtiene el matcher y el ledger como *capacidad*:
  puede registrar hipótesis, consultar déficits y recibir reaperturas. Lo que
  **no** obtiene es la garantía de que el gate se aplique — eso requiere
  código en la ruta de salida, y ninguna Skill puede imponerlo desde afuera
  (§0, principio 3).
- **En kal-in sí es enforced**: ahí el subpaquete se consume directo desde
  `agent_core/epistemics/` y el gate vive en la ruta de salida (F2).
- **Fail-closed solo del lado del host.** Si la Skill no está disponible, un
  agente de terceros pierde una capacidad, no un invariante. Por eso la vía
  recomendada para terceros es *vendorizar* la librería portable, y la Skill
  es la vía de integración rápida.

Consecuencia de diseño: la fachada Skill **no** duplica lógica. Es un
envoltorio delgado sobre `kal_epistemics` (JSON de entrada/salida), y la
lógica determinista se testea una sola vez, en el subpaquete.

**Implementada en F2b (2026-09-29): `skills/epistemic_ledger/`.** Cómo se
resolvieron las dos cosas que el boceto no decía:

- **La librería viaja vendorizada adentro de la skill** (copia byte a byte de
  `kal_epistemics/`, con un test que falla si las dos copias se separan, y la
  firma de la skill cubriendo los 12 archivos). No es una preferencia: el runner
  del kernel le copia a una skill solo `sdk/` y su propia carpeta, y meter
  `kal_epistemics/` en el runner sería que el kernel conozca la capa epistémica
  del agente. Es además la vía que este documento ya recomendaba a terceros: la
  Skill es la integración rápida, vendorizar la librería es la fuerte.
- **El estado entra y sale en cada llamada** (snapshot JSON): una Skill vive en un
  contenedor efímero, así que no hay dónde recordar entre invocaciones. El
  adoptante guarda el snapshot donde quiera.
- **Los cuatro límites viajan declarados en CADA respuesta** (bloque `advisory`),
  para que nadie los descubra después: el gate no se aplica desde acá; el `origen`
  de cada evidencia lo declara el llamador y la skill no puede verificarlo; no hay
  persistencia; y no se aplica la política de exposición del host (el saneamiento
  vive en `agent_core/epistemics/`, que no es parte de la librería portable).
- Operaciones: `estado_vacio`, `reanudar`, `consultar` (preguntas, déficits,
  hipótesis con nivel y flags), `abrir_pregunta`, `registrar_hipotesis`,
  `ingerir_evidencia` (con reaperturas automáticas), `simular_evidencia`
  (dry-run sobre una copia), `abstencion` (texto estable entre paráfrasis) y
  `compactar`. Las condiciones entran por el camino no confiable
  (`from_untrusted`): marcadores, nunca patrones, porque el llamador de una Skill
  puede estar reflejando texto no confiable.
- **No es ingerible**, y hay test: su salida es un eco del estado que el llamador
  ya tiene, así que ingerirla sería el lavado de origen que F2 cerró por
  allowlist, con una vuelta más.

---

## 2. Vista de fases

| Fase | Qué entrega | Estado | Depende de |
|---|---|---|---|
| **F0** | Contratos congelados + validación estructural | ✅ hecho | — |
| **F1** | Matcher determinista + ledger en memoria + abstención estable | ✅ hecho | F0 |
| **F2** | Gate real + tools + adaptador en `agent_core/epistemics/` | ✅ hecho | F1 |
| **F2b** | Fachada Skill para terceros (advisory, sin duplicar lógica) | ✅ hecho (2026-09-29) | F1 |
| **F3** | Ancla de procedencia verificable | ✅ hecho (2026-09-29) | F0 |
| **F4** | Ledger durable, dedup entre sesiones, compactación | ✅ hecho (2026-09-29) | F2, F3 |
| **F5** | Skill `claim_verifier` | ✅ hecho (2026-09-29) | F3 |
| **F6** | Suite de medición y calibración | ✅ hecho (2026-09-29) | F2 |
| **F7** | Costura seguridad ↔ epistemología | ✅ hecho (2026-09-29) | F5, F6 |
| **F8** | Introspección, alternativas incompatibles y validez | ✅ hecha (C, D1, esquema v2) | F4, F7 |

El orden importa más que las estimaciones. F2 antes de F4 es deliberado: ver
§5, riesgo R1.

### Estado de F0 + F1 + F2 (implementado, 155 tests)

```
kal-in/kal_epistemics/
  __init__.py      API pública y __version__
  errors.py        EpistemicsError y derivadas
  models.py        Evidence · Hypothesis · Question · EvidenceRequest ·
                   ReopenCondition · EvidenceLink · HistoryEntry · Flag
  conditions.py    should_reopen() — determinista y fail-closed
  ledger.py        HypothesisLedger — en memoria (sin persistencia)
  abstention.py    render_abstention() · classify_abstention()
  safety.py        topes + validación de patrones (la superficie de ataque)
kal-in/tests/test_kal_epistemics_{models,conditions,abstention,ledger,portabilidad,seguridad}.py
```

Seis cosas quedaron como **invariantes probados**, no como convenciones
(la sexta la agregó F6, ver D10):

1. `MODEL_GENERATED` no reabre nada — ni siquiera con una condición que lo
   declare explícitamente entre los orígenes aceptados (test enumerativo
   sobre todos los orígenes posibles).
2. Un estado que exige respaldo (`SUPPORTED`, `REACTIVATED`) sin evidencia
   exógena levanta `InvalidHypothesis`, incluso con 20 vínculos endógenos.
3. La repetición no refuerza: la misma evidencia (mismo hash) tres veces deja
   un solo vínculo y un solo evento de reapertura.
4. El ledger no es un `MemoryBackend` ni expone `recall`/`retrieve`/`store`:
   la garantía anti-contaminación (R2), escrita como test.
5. **El camino no confiable no puede pasar regex**: `from_untrusted()` no tiene
   el parámetro. No es un filtro — el parámetro no existe.
6. **El resultado no depende del orden de las condiciones.** Si la misma
   evidencia dispara varias acciones, se registran TODOS los vínculos y el
   estado es el más cauto (`REFUTE > WEAKEN > REOPEN`); el dedup es por
   (condición, evidencia), y el presupuesto de reaperturas se consume una vez
   por evidencia. Test parametrizado con los dos órdenes de declaración.

Y la portabilidad es verificable, no declarada: un test lee las fuentes y
otro importa el paquete en un proceso limpio, para confirmar que no arrastra
`agent_core`/`kernel`/`sdk` ni gana dependencias fuera de stdlib.

---

## F0 — Contratos congelados ✅ *hecho*

**Objetivo:** que el esquema deje de ser una discusión. Sin lógica, sin
store, sin LLM.

**Entregables**

- Paquete `kal_epistemics` (nombre a confirmar), stdlib puro, `schema_version`
  desde el primer commit.
- `Evidence`: `id`, `origin` (`KERNEL_OBSERVED | TOOL_EXTERNAL |
  USER_ASSERTED | MODEL_GENERATED`), `anchor` (`audit_event_id | hash | uri |
  turn_id`), `method`, `acquired_at`, `recheckable`.
- `Hypothesis`: `id`, `claim` (una sola proposición), `status`, `level`
  (derivado, no declarado), `depends_on`, `support`/`contra` como
  `[(evidence_id, polaridad)]`, `falsifier` (predicado), `validity_conditions`,
  `reopen_conditions`, `history` append-only.
- `Question`: `id`, `canonical_key` (clave de dedup), `text`, `first_asked`,
  `times_asked`, `open_hypotheses`, `missing` (los `E7`).
- `ReopenCondition`: predicado mecánicamente evaluable sobre `Evidence`.

**Criterios de aceptación**

- Round-trip de serialización para los cuatro objetos, con historial intacto.
- Validación estructural que **rechaza** un `Hypothesis` en estado
  `SUPPORTED` sin al menos una `Evidence` con `origin ∈ {KERNEL_OBSERVED,
  TOOL_EXTERNAL, USER_ASSERTED}`. Un `MODEL_GENERATED` no alcanza nunca.
- Cero dependencias, verificado con grep — no por convención (mismo criterio
  que usa `kal` para "sin LLM").
- `history` append-only a nivel de tipo: no existe API para mutar un evento
  pasado.

**Guardas de alcance:** nada de persistencia, nada de matcher, nada de
puntajes. Si aparece la tentación de agregar `confidence: float`, se anota en
§7 (decisiones abiertas) y no se implementa.

**Resultado (implementado).** Tres decisiones del contrato cambiaron al
escribirlo, y conviene que queden registradas:

- La polaridad **no** vive en `Evidence` sino en `EvidenceLink`: la misma
  observación puede sostener una hipótesis y contradecir otra, y con la
  polaridad en la evidencia habría que duplicarla para poder usarla en
  contra de algo.
- El falsificador **no** es un tipo aparte: es una `ReopenCondition` con
  `action=REFUTE`. "Qué me reabriría" y "qué me refutaría" son la misma
  pregunta con distinta consecuencia.
- `level` y `confidence` no existen como campos: `level` lo calcula el
  ledger sobre el grafo, y `confidence` no existe en absoluto. Hay un test
  que falla si alguien agrega cualquiera de los dos (D7).

---

## F1 — Matcher determinista y render de abstención ✅ *hecho*

**Objetivo:** probar que la reapertura se puede decidir **sin el modelo**. Es
la fase que valida o refuta todo el diseño, y es barata.

**Entregables**

- `should_reopen(condition, evidence) -> Decision` — función pura.
- `HypothesisLedger` en memoria: alta, consulta por `canonical_key`,
  transición de estado, historial.
- `render_abstention(question) -> str`: redacción **estable** (la misma
  pregunta produce el mismo texto), con el déficit nombrado.
- Los tres modos de recuperación, con el default correcto: disparador
  determinista, consulta de dedup, e inyección acotada. **Nunca similaridad
  semántica por defecto.**

**Criterios de aceptación**

- Test enumerativo sobre **todas** las combinaciones de `origin`: ninguna
  condición reabre con `MODEL_GENERATED`.
- No reabre por repetición (la misma hipótesis re-registrada N veces no
  cambia el estado).
- No reabre por re-fetch idéntico: misma fuente, mismo hash → no es evidencia
  nueva.
- Sí reabre con evidencia exógena que matchea el predicado, y registra **qué**
  cambió.
- La dedup funciona: la misma pregunta con dos redacciones distintas
  normaliza al mismo `canonical_key`.

**Métrica de esta fase:** 100% determinista. Si en algún camino hace falta
una llamada al modelo para decidir si reabrir, el contrato de `ReopenCondition`
está mal y hay que volver a F0.

**Resultado (implementado).** Todos los criterios de aceptación tienen test.
Dos hallazgos de la implementación:

- `HypothesisLedger.consider()` es el único camino por el que una hipótesis
  se reabre sola, y no pasa por el modelo en ningún punto de ese camino.
- Los ciclos son **inconstruibles** por la API: una dependencia tiene que
  existir antes que su dependiente, así que el grafo es un DAG por
  construcción. Igual hay un test que inyecta un ciclo a mano (por el
  atributo privado) para verificar que `level()` no se cuelga y marca
  `CIRCULAR` — pensando en la carga desde disco de F4, donde el DAG ya no
  está garantizado por el orden de construcción.

---

## F2 — Gate real y reapertura intra-tarea en kal-in

**Objetivo:** el mecanismo haciendo algo, en el loop real, sin persistencia
todavía.

**Entregables**

- `agent_core/epistemics/` como adaptador: consume el paquete de F0/F1.
- Tools `open_question(...)` y `record_hypothesis(...)` con esquema validado
  — conservar tiene que ser un **acto**, no una intención. Registradas por el
  camino normal de herramientas, para que queden mediadas y auditadas.
- **El gate en la ruta de salida final** (post-procesador sobre el texto que
  sale, en el camino de respuesta del `conversation_engine`/`agent_loop`), no
  una tool.
- Política de clasificación por situación de evidencia:

  | Situación | Salida |
  |---|---|
  | Evidencia exógena independiente, sin contra | afirmar + cita el id |
  | Solo inferencia desde premisas con evidencia | afirmar con hedge + cita |
  | Solo cadena endógena | hipótesis explícita, o no emitir |
  | Dos fuentes exógenas en conflicto | presentar ambas, no resolver |
  | Nada, y falta E | "no lo sé" nombrando E |
  | Evidencia compatible con varios mundos | subdeterminación |
  | Falta una condición de la pregunta | repreguntar |

- Reapertura **automática** (D2): al ingerir el resultado de una herramienta, el
  ledger cambia el estado de la hipótesis solo, sin intervención del modelo ni
  del usuario. El automatismo está en el ESTADO, nunca en lo que se dice.
- **Alcance del bloqueo (D8, resuelto):** el bloqueo duro aplica **solo a la
  ruta del planner/agent loop**, que es la única que produce afirmaciones
  factuales sobre el mundo con herramientas. Las rutas trivial,
  `answer_directly` y conversation-engine **no se bloquean** (no afirman nada
  verificable, y bloquear un "hola" sería un bug), pero todas pasan por el
  mismo choke point, para que el gate tenga un solo lugar donde vivir y
  auditarse.
- Bloque "expediente abierto" con **tope de items y de tokens**.
- **Fail-closed duro (D1, resuelta): el gate puede BLOQUEAR la tarea.** Si no
  puede clasificar una afirmación factual, no la emite: corta y dice
  explícitamente qué falta. Degradar la redacción (hedge) es el mínimo
  aceptable, no el default — el default es no afirmar.

**Criterios de aceptación**

- Pregunta imposible (entidad ficticia, evento futuro, dato privado) →
  abstención con el déficit nombrado.
- **La misma pregunta reformulada 3 veces → la misma abstención** (sin
  deriva). Este es el test que mide el valor real del ledger.
- Pregunta conocida y verificable → **no** se abstiene. Test anti-falso
  positivo, presente desde esta fase y no al final.
- El gate no se puede saltear: test que verifica que la ruta de salida pasa
  por el gate aunque el modelo no haya llamado a ninguna tool.
- El gate puede bloquear: test que verifica que una afirmación factual sin
  respaldo **no sale**, y que la tarea termina con el déficit nombrado en vez
  de con una respuesta plausible.
- Reapertura intra-tarea: la tool devuelve el dato que faltaba 5 pasos
  después → la hipótesis pasa a `REACTIVATED` sin intervención del modelo.

**Guardas de alcance:** sin persistencia entre sesiones, sin verificación
externa (F5), sin motor adversarial, sin puntajes numéricos.

---

**Resultado: F2 completa**, en cuatro rebanadas, todas verificadas:

- `agent_core/epistemics/store.py` — `EpistemicStore`: ledger por sesión (con
  desalojo LRU, mismo criterio que `agent_core/sessions.py`), ingestión de
  observaciones, y bloque de "expediente abierto" acotado.
- `tool_integration/adapters/epistemic_tools.py` — `open_question` y
  `record_hypothesis`. **La firma ES la frontera**: no tienen parámetros de
  `origin`, `evidence` ni `status`, ni `**kwargs`. Registrar siempre deja en
  `open`; el estado lo cambia la evidencia.
- `agent_loop.py::_dispatch_tool` — punto ÚNICO de ingestión, con `session_id`
  explícito (nunca estado de instancia: sería el bug A-4 otra vez).

Dos hallazgos reales de esta rebanada, los dos por leer el código de verdad:

1. **El lazo de lavado de origen.** Si `recall`, `remember`, `open_question` o
   `record_hypothesis` fueran ingeribles, el modelo podría: registrar una
   hipótesis → su propia respuesta entra como "observación del kernel" → la
   hipótesis queda sostenida por sí misma. La ingesta es una **allowlist** que
   falla cerrado, y hay un test por el camino real (`_dispatch_tool`) que
   verifica que un eco que devuelve *exactamente* el texto que reabriría la
   hipótesis no ingiere nada.
2. **Las herramientas asíncronas no son observaciones.** `read_workspace_file`,
   `import_resource` y `android_build_and_screenshot` estaban en mi allowlist, y
   no correspondía: devuelven un Artifact "pending" y el contenido real llega
   después por otro camino. Ingerirlas era fabricar un dato a partir de un
   marcador de estado. Ahora hay un test que exige que la allowlist sea disjunta
   de `_VSCODE_ONLY_TOOL_NAMES`, para que una asíncrona nueva obligue a revisar
   la decisión.

Las rebanadas 3 y 4 agregaron el **sanitizador** (`sanitize.py`: tercer eje
independiente — admisión / creencia / **exposición** — reusando
`security_policy.py`, con el marcador de "dato, no instrucciones" viajando con
el dato y con el *claim*, la pregunta y el déficit pasando también por él,
porque el sumidero no es solo `Evidence.content`), y el **choke point** con el
**gate** en `routers/chat.py`: cuatro salidas distintas unificadas en una,
dos reglas mecánicas (déficit declarado y respondido igual → bloquea; turno
sin observaciones y respuesta larga → advierte), fail-closed ante error
interno, y un test que lee el archivo y falla si aparece una quinta puerta.

**Ancla (HECHA).** La evidencia se ancla al `event_hash` de un evento real del
audit log encadenado por HMAC (`anchor_kind="audit_event_hash"`), creado al
ingerir. Es lo que hace que la procedencia no se pueda fabricar: el modelo no
puede escribir en el audit log. Detalle que importa: `AuditEvent` no tiene id
propio — **su identidad ES su hash encadenado**, que depende del evento
anterior y de la clave HMAC. El `content_hash` sigue existiendo, pero solo
para dedup. Y es fail-closed **visible**: si no se puede auditar (disco de solo
lectura, clave inaccesible), la evidencia NO se descarta —perder una
observación real sería peor— pero queda sin ancla y el ledger la marca con
`Flag.UNVERIFIABLE_PROVENANCE`. El `event_type="epistemic_evidence_ingested"`
se agregó en los DOS repos.

---

**Corrección posterior, medida en F6 (2026-09-29):** F2 quedó completa en cuanto
a sus criterios, pero la medición encontró un defecto de cableado en esta
rebanada: `open_question` crea el déficit **sin condición de pago**, así que
`unsatisfied_deficits()` nunca lo suelta y R1 bloquea respuestas correctas
después de que llegó la evidencia que faltaba. El mecanismo para saldarlo existe
y funciona por el camino de código (`add_evidence_request` con condiciones); lo
que falta es que la tool lo use. Medición: `docs/MEDICION-CAPA-EPISTEMICA.md`
(tabla de deuda epistémica); decisión abierta: **D9**.

**Bloqueante medido el 2026-09-29 (y es más grande que el ancla):** kal-in está
**desincronizado de `kal` en 18 archivos de directorios compartidos** (según
`scripts/check_kernel_drift.py --kal-repo`, la herramienta del propio proyecto,
que es la fuente autorizada), con
ambos repos en su `origin/main` y sin commits locales. `audit/audit_log.py`
(127 líneas de diff), `kernel/api/sandbox_api.py` (78),
`kernel/lifecycle/docker_runner.py` (65), `kernel/lifecycle/Dockerfile` (61),
`kernel/api/socket_server.py` (38), `kernel/api/bus.py` (33),
`kernel/registry/registry.py` (30), `kernel/permissions/access_manager.py`
(24), `code_analysis/ast_validator.py` (23),
`kernel/permissions/network_safety.py` (22), `sdk/skill.py` (20),
`code_analysis/denylist.py` (18) — alrededor de 539 líneas. Dos son de
seguridad y uno es el audit log. `scripts/check_kernel_drift.py` existe
exactamente para detectar esto: hoy reporta los dieciocho.

**Triage automático** (comparación de AST ignorando docstrings, 2026-09-29):
**12 de los 18 son solo comentarios/docstrings** — código idéntico, así que la
reconciliación es trivial y sin cambio de comportamiento; **1 es el
`Dockerfile`** (no-Python, mirar a mano); y **5 tienen código distinto de
verdad**: `kernel/api/sandbox_api.py`, `kernel/api/socket_server.py`,
`kernel/lifecycle/docker_runner.py`, `kernel/registry/skill_market.py` y
`audit/audit_log.py`. Esos cinco son los que necesitan decisión humana, y dos
(`docker_runner`, `sandbox_api`) son superficie del sandbox.

> Medido dos veces mal antes de medirlo bien, y vale la pena registrarlo:
> primero reporté **12** archivos porque mi propio `diff` estaba truncado por
> un `head -8`; después armé un triaje que decía que
> `kernel/permissions/network_safety.py` tenía una marca de vulnerabilidad
> real en `kal` y **cero** en kal-in — parecía un fix de seguridad faltante.
> Al leer el diff de verdad, la diferencia era **solo el docstring**. Contar
> marcadores en comentarios no mide código: es la misma lección que el
> detector de ReDoS inerte, aplicada a medir en vez de a controlar.

**Lectura corregida del drift (2026-09-29, tras leer los cinco diffs uno por
uno).** Lo que parecía "kal-in atrás en fixes de seguridad" es, en realidad:

- **Cuatro de los cinco son el MISMO fix escrito distinto.** `skill_market.py`
  (B-10), `socket_server.py` (B-4), `sandbox_api.py` (C-3/M-2/M-6) y
  `docker_runner.py` (M-5/B-5) tienen la misma corrección en ambos lados;
  difieren en el **nombre** de las funciones y excepciones y en la redacción de
  los comentarios. `kal` renombró `InvalidLineEncodingError` →
  `InvalidEncodingError`, `require_sandbox_secret` → `_verify_token`, y los
  `event_type` `kernel_line_invalid_encoding` → `kernel_invalid_encoding`.
  Comportamiento equivalente.
- **Uno es divergencia DELIBERADA y documentada.** `docker_runner.py`:
  kal-in necesita `_ALLOWED_EXTRA_MOUNT_ROOTS = ("/workspace", "/project")`
  porque tiene `agent_core/self_modification.py`, que monta una copia del
  proyecto en `/project`; `kal` es kernel puro y no tiene ese llamador. El
  comentario de kal-in lo dice explícitamente: *"kal (de donde se portó) es
  kernel puro y no tiene ese segundo caso real"*.
- **`audit_log.py`** implementa el mismo fix de A-10 #2 con otra forma
  (kal registra la línea cruda y hace `continue`; kal-in marca la entrada).

O sea: **no hay evidencia de un fix de seguridad faltante en kal-in** — que es
lo contrario de lo que alarmaba mi primera lectura. Y aparece un problema de
fondo del propio chequeo: la regla "byte a byte idénticos" **no puede
expresar "esta divergencia es intencional y está documentada"**, así que
`docker_runner.py` va a figurar como drift para siempre. Opciones: una
allowlist explícita en el checker con la justificación de cada entrada, o
mover la diferencia a un parámetro.

**Paso 0 de F3:**
reconciliar el drift (decidir qué versión es la canónica en cada caso), y
recién después agregar el `event_type` nuevo en `kal` y portarlo. Agregar el
event type arriba de un archivo ya divergente habría empeorado la
reconciliación.

**RESUELTO (2026-09-29).** Los 5 archivos con código real reconciliados a
mano, uno por uno, los 4 renombres verificados con `git log -S` en los dos
repos (kal escribió su versión ~17h antes que kal-in, citando la auditoría de
kal-in — no se renombró nada para unificar: el costo de tocar
`SANDBOX_RUNNER_SECRET`/`X-Sandbox-Secret`, ya acoplados a
`docker-compose.yml`/`.env.example`, no se justifica por una limpieza
cosmética) y un fix real que sí faltaba (`socket_server.py`:
`_audit_invalid_encoding()` descartaba el detalle de `UnicodeDecodeError`,
kal lo pasaba y kal-in no — portado). kal-in: `f3bc10f`, `bf05eb7`, `78921a5`.
kal: `0d1723f` (más una corrección real encontrada portando M-5 acá: el fix
de `_validate_extra_mounts` de kal comparaba
`PurePosixPath(container_path).is_relative_to("/workspace")` sin normalizar
— `/workspace/../etc/passwd` lo evadía — corregido con `posixpath.normpath()`
en los dos repos). `check_kernel_drift.py` va a seguir reportando 18 para
siempre (comparación byte a byte, no puede expresar "revisado y aceptado")
— **eso ya no es una señal de trabajo pendiente**, es la limitación del
checker ya identificada arriba. El "Paso 0" de F3 está cerrado; **F3 completa**
(ancla + drift).

La allowlist con hash pineado por archivo (para que el checker distinga
"divergencia revisada" de "divergencia nueva sin revisar", sin ocultar un fix
real futuro) sigue sin implementarse — sugerida, no bloqueante.

---

## F3 — Ancla de procedencia verificable en kal

**Objetivo:** que la procedencia no se pueda fabricar. La pieza de kernel, y
la única.

**Entregables**

- API estable: `audit.resolve(event_id) -> registro verificable`
  (evento, timestamp, hash de cadena, si el encadenado es consistente).
- Que toda escritura al ledger desde kal-in quede anclada a un `event_id`
  real de una ejecución real.
- Índice de resolución **solo si hace falta**: ver B-8 en
  `kal/docs/HISTORY.md` (el `_read_last_hash()` cuadrático está documentado y
  diferido a propósito). A los tamaños de log actuales, la lectura lineal
  alcanza; si se agrega índice, usar el enfoque de sidecar ya analizado en ese
  documento, no inventar otro.

**Criterios de aceptación**

- Un `event_id` inventado no resuelve (y el ledger lo marca
  `unverifiable_provenance` de forma automática, sin juicio del modelo).
- El registro resuelve a un hash encadenado consistente.
- Test de manipulación: alterar el log hace que `resolve()` deje de validar.

**Guardas de alcance — importantes:** **no** entra a kal ninguna política de
contenido ("denegar afirmaciones sin respaldo") ni almacenamiento de
hipótesis. kal media a qué recursos accede el código y registra lo que pasó;
opinar sobre contenido rompería la frase que lo define ("microkernel de
seguridad puro, sin LLM, sin agente") y su modelo de amenazas. El aporte del
kernel es exactamente uno: procedencia infalsificable.

---

## F4 — Ledger durable

**Objetivo:** que el "no lo sé" y las hipótesis sobrevivan al proceso, sin
convertirse en un cementerio ni en una fuente de alucinaciones.

**Entregables**

- Store con `canonical_key` como clave de dedup — la misma pregunta en otra
  sesión cae en el **mismo** registro.
- Append-only + historial; transiciones de estado registradas.
- Compactación por presupuesto: se tira el detalle de las hipótesis largas,
  se conserva **el déficit y la razón de la abstención** (es la parte que
  evita reinventar).
- Política de reapertura: límite de reaperturas por pregunta; nunca reabrir
  sin evidencia exógena nueva (dedup por hash).
- Decisión de backend (ver §7).

**Criterios de aceptación**

- **Test de no contaminación (el más importante del proyecto):** una
  hipótesis del ledger **nunca** aparece en el camino de conocimiento
  (`recall()`); el ledger no es un `MemoryBackend`.
- Promover/compactar no pierde `missing[]`.
- La misma pregunta en otra sesión no crea un registro nuevo.
- Una hipótesis nunca se promueve a conocimiento por antigüedad, por
  repetición, ni por haber sobrevivido sin ser refutada.

### Resultado: F4 completa (2026-09-29)

**Decisiones tomadas** (el usuario las resolvió al cerrar el bloqueo):

- **D4: SQLite propio**, en `kal_epistemics/sqlite_store.py` y
  `data/epistemics/ledger.db`. El argumento que decidió: el port tiene una
  operación que **reemplaza el estado completo de un ámbito**, no escritura
  incremental por evento, así que con JSONL habría que inventar el protocolo de
  concurrencia (reescritura total con `tmp` + `os.replace` y un lock propio) que
  el port dice explícitamente que no quiere inventar. SQLite ya lo trae probado
  (WAL, locking, transacciones), es stdlib, y el precedente es del repo:
  `agent_core/memory/mid_term.py` usa `sqlite3` crudo, sin ORM, archivo propio.
  La auditabilidad no se pierde: la fila es el snapshot en JSON y se lee con
  `sqlite3 ... "select snapshot from ledger where ambito=..."` sin la librería.
- **D5: ámbito por instalación**, con la clave explícita
  (`AMBITO_POR_DEFECTO`). El aislamiento no desaparece: cambia de eje. Era por
  sesión (F2), ahora es **por ámbito**, y la lección de A-4 se mantiene — el bug
  era que el aislamiento dependiera del objeto que uno tuviera a mano, no que
  existiera. Con una sola persona por instalación, que las sesiones compartan el
  registro es la continuidad que se busca; el día que haya multiusuario se cambia
  el valor de la clave sin migrar la forma de nada.

Sobre **"append-only"**: lo que es append-only es el HISTORIAL del ledger
(cada transición de estado agrega una entrada y no existe API para mutar una
pasada — invariante de F0, con test). El archivo del backend no es un log de
eventos: `guardar()` reemplaza la fila del ámbito, que es lo que el port define.
Un log append-only era la propiedad que ofrecía la opción JSONL y no fue la
elegida; el historial append-only se conserva igual, adentro del snapshot.

**Implementado, en la librería portable (`kal_epistemics/`):**

- **`snapshot()` / `from_snapshot()`** — estado COMPLETO y determinista (dos
  ledgers en el mismo estado dan el mismo diccionario y el mismo hash),
  incluyendo lo que no se ve en los objetos públicos: qué evidencia se aplicó
  **por condición**, qué déficits se saldaron, y el presupuesto de reaperturas
  consumido. Sin eso, recargar reabriría lo ya reabierto (R8) y el mismo dato
  contaría dos veces por haber habido un reinicio.
- **Fail-closed al recargar**: esquema distinto, evidencia o dependencia
  desconocida, clave canónica que no corresponde a su texto, hipótesis que apunta
  a una pregunta o un déficit inexistente, déficit huérfano, o contador de
  reaperturas de una hipótesis que no está. Los ciclos inyectados a mano
  sobreviven sin colgarse (`level()` = `None`, `Flag.CIRCULAR`).
- **`durable.py`** — el port `AlmacenDurable` (cuatro operaciones: cargar,
  guardar, descartar, ámbitos), `LedgerDurable` (abrir/guardar/guardar_si_cambio/
  compactar) y `AlmacenEnMemoria` como doble fiel, que pasa por JSON en las dos
  direcciones.
- **`sqlite_store.py`** — el backend: una tabla, una fila por ámbito, WAL,
  transacciones, lock propio (el endpoint corre en el threadpool de FastAPI) y
  `EstadoIlegible` cuando la fila no se puede leer.
- **`compact()` + `compaction.py`** — la política de qué se tira: nunca una
  hipótesis viva, nunca nada estructural (estado, vínculos, dependencias,
  condiciones de reapertura), nunca un déficit; solo el detalle de las terminales
  más viejas de cada pregunta. Idempotente, con marca de auditoría que conserva
  el tamaño ORIGINAL, y con `conserva_lo_irrenunciable()` verificándose antes de
  escribir.

**Implementado, en el host (`agent_core/epistemics/store.py`):**

- `EpistemicStore` pasa a ser del ámbito: `almacen=` (SQLite en el runtime vía
  `almacen_persistente()`) y `ambito=`. Guarda después de cada mutación
  (`_persistir_si_mutó`, con `guardar_si_cambio()` para no reescribir en los
  turnos que no tocaron nada) y compacta en el mismo paso.
- `ingested_count()` sigue siendo **por sesión** (contador en memoria): R2
  pregunta "¿este turno consultó una fuente?", y contar el ámbito haría que una
  sesión silenciara el aviso de otra.
- `tests/conftest.py` aísla el ledger por test (store en memoria): el runtime
  persiste en `data/`, y sin ese fixture el estado de un test bloquearía al
  siguiente.

**D9, cerrado con el mismo backend** (era el otro hallazgo de F6): `open_question`
acepta `resolved_by` (marcadores por el camino no confiable, `from_untrusted`) y
los adjunta al déficit, así que la deuda se salda cuando llega la evidencia. El
caso 3 de la tabla de deuda epistémica —el único camino que el modelo puede
tomar— pasó de "1 déficit pendiente y gate `bloquear`" a "0 pendientes y gate
`permitir`". Sin condición de pago la deuda sigue siendo para siempre, y eso
queda como test: el ledger no adivina qué evidencia la cubre.

**Criterios de aceptación: los cuatro, verificados.** No pierde `missing[]`,
nunca promueve, no reabre al recargar, y la misma pregunta en otra sesión no crea
un registro nuevo — el número que F6 medía en **0.00 ahora mide 1.00**, y el test
se invirtió con él. El de no contaminación se extendió al almacén, al registro
durable y al backend: ninguno expone `recall`/`store` ni es un `MemoryBackend`.

**Un límite declarado, con test, y una corrección honesta.** `guardar()` escribe
el snapshot completo, así que dos escritores vivos del mismo ámbito siguen siendo
last-write-wins. SQLite aporta atomicidad y evita la corrupción y el "database is
locked", que es la parte que resuelve; **no aporta merge**, porque eso es contrato
del port, no del motor — creer que elegir una biblioteca resuelve un problema de
diseño es exactamente el error que este proyecto evita. Hoy no ocurre (un único
ledger vivo por ámbito). Hay un test sobre el port y otro sobre SQLite que lo
dicen.

**R8 (límite de reaperturas por pregunta): implementado, con el número medido.**
El tope por hipótesis no acota el total de una pregunta, porque la misma
observación se evalúa contra TODAS las hipótesis: con 5 hipótesis mirando el mismo
marcador, **una sola observación produce 5 reaperturas**, y el techo teórico de
una pregunta era 10 × 50 = **500**. Ahora hay un presupuesto por pregunta
(`MAX_REOPEN_APPLICATIONS_PER_QUESTION = 30`, tres veces el de una hipótesis), y
la medición verifica el techo **efectivo** dejando una pregunta en churn: da 30,
no 500. Se **deriva** de los contadores por hipótesis, así que viaja en el
snapshot sin agregar estado; una hipótesis sin pregunta solo tiene su propio
presupuesto. El valor es un inicial revisable, y su costo queda declarado: un
tope así también puede impedir que una hipótesis legítima reciba el respaldo que
le llegó — en el corpus, el máximo observado por pregunta es 1–2, así que ningún
caso real se acerca al tope.

---

## F5 — Skill `claim_verifier`

**Objetivo:** conseguir la evidencia exógena de la que depende todo lo demás,
en la forma que el mercado sí puede consumir.

**Entregables**

- Skill: entrada un claim atómico; salida un **artefacto con evidencia**
  (`uri`/hash/`audit_event_id`) y, si corresponde, una `ReopenCondition`
  propuesta. Nunca devuelve "verdadero/falso".
- Permisos explícitos de red y filesystem, sandbox, firmada (`skill.sig`).

**Criterios de aceptación**

- Tests `requires_docker`, como el resto de las skills.
- Fail-closed del lado del agente si la Skill no está disponible: el agente
  pierde una **capacidad**, no un invariante.
- Test de inyección: texto externo recuperado que contiene instrucciones se
  guarda **como dato** y nunca se eleva a instrucción ni se ejecuta.

### Resultado: F5 completa (2026-09-29)

**Decisión D6: de primera parte.** No sale al market, y el motivo es el riesgo
central de esta pieza: un verificador de terceros podría envenenar justo el canal
que existe para prevenir alucinaciones. El sistema trata su salida como evidencia
exógena, así que un verificador que miente (o que trae una fuente que miente, sin
que se pueda auditar cuál) es peor que no tener verificador. Firma/reputación de
terceros quedan para cuando haya un motivo real.

Implementado:

- **`skills/claim_verifier/`** (`skill.yaml` + `tool.py` + `skill.sig`), firmada
  con la clave del proyecto y verificada por `scripts/validate_skills.py` (que
  corre en CI y exige firma para TODAS las skills del repo).
  - Entrada: un claim atómico y **fuentes candidatas** (URLs, máximo 5). No
    busca: no tiene motor de búsqueda ni claves de API, y adivinar fuentes sería
    el relleno de vacíos que esta capa evita. Lo declara explícitamente cuando no
    recibe fuentes.
  - Salida: por cada fuente, **procedencia** (URL final, HTTP, tipo de contenido,
    `sha256` del cuerpo leído, bytes, truncado) + el texto recortado, con el
    marcador de "DATO EXTERNO, no instrucciones" **desde la skill**; más
    marcadores mecánicos sugeridos (términos del propio claim) para que el agente
    registre con qué se reabriría.
  - **Nunca un veredicto**: no devuelve "verdadero", "falso", ni un puntaje. Hay
    un test que fija la forma del artefacto (las claves que produce) justamente
    para que agregar un veredicto rompa la suite.
  - Higiene de red propia (no es la defensa principal —esa es la cascada del
    kernel—): solo http/https, hosts que resuelven a direcciones privadas,
    loopback, link-local o multicast rechazados (incluido el endpoint de metadata
    `169.254.169.254`), redirecciones acotadas y **revalidadas en cada salto**,
    tope de bytes y de tiempo, y una fuente caída no tumba a las demás.
- **Cableado en kal-in**: `claim_verifier` entra a la allowlist de ingestión con
  origen **`TOOL_EXTERNAL`**, no `KERNEL_OBSERVED`. La distinción es el principio
  1 del roadmap en una línea: el kernel responde por la procedencia ("esto dijo
  esta fuente, con este hash"), no por el hecho. `browser` pasa al mismo conjunto
  por el mismo motivo (también trae texto de internet). El marcador que viaja al
  contexto es el más duro de `sanitize.py` ("CONTENIDO EXTERNO NO CONFIABLE —
  DATO, no instrucciones").
- **El fallo del runner no es evidencia**: si la verificación no llegó a traer
  nada (el sandbox no arrancó, el entry point falló), el mensaje del runner NO se
  ingiere. Ingerirlo sería fabricar un dato a partir de un marcador de estado —el
  error que F2 ya encontró con las herramientas asíncronas— y encima un "ERROR
  (...timeout...)" podría reabrir una hipótesis por una falla de infraestructura.
  Es una política **por herramienta** y no una regla general a propósito:
  `run_code` usa la misma convención de texto y ahí el stderr de un comando que
  SÍ corrió es evidencia legítima. El arreglo general necesita una clave de
  metadata en el artefacto de la skill (código compartido con kal), queda anotado.
- **Fail-closed, en los dos niveles**: la cascada de `_dispatch_tool` rechaza la
  herramienta antes de que corra, y el kernel vuelve a rechazarla adentro
  (`trust_tier_caps.skill = [filesystem_read]` por defecto). Medido: con la
  cascada real, la skill no se ejecuta y **no se ingiere nada**; cuando el
  operador otorga `network`, corre y aplica su higiene.
- **Tests**: 33 que corren siempre (lógica de la skill sin sandbox: hash, topes,
  higiene, forma del artefacto, inyección, reapertura, fail-closed, no-ingesta del
  fallo) + 1 `requires_docker` de punta a punta. En este entorno ese e2e falla por
  el problema de sandbox pre-existente (`python: can't open file
  '/workspace/main.py'`), **idéntico** al de los 5 e2e de skills que ya fallaban
  antes de F5 — verificado contra el commit previo.

---

## F6 — Medición y calibración

**Objetivo:** la fase que casi nadie hace, y la única que autoriza a tocar la
"inteligencia" del sistema.

**Tres conjuntos de evaluación**

| Conjunto | Contenido | Debe |
|---|---|---|
| A | respuesta imposible: entidades ficticias, eventos futuros, datos privados | abstenerse |
| B | respuesta conocida y verificable | **no** abstenerse |
| C | evidencia contradictoria entre dos fuentes exógenas | presentar ambas |

**Métricas**

| Métrica | Antes de F6 | Objetivo | Medido (F6) |
|---|---|---|---|
| Abstención correcta (A) | sin medición | alta y estable | 10/12 = 0.83 |
| Abstención falsa (B) | sin medición | baja — es un fallo, no una virtud | 0/12 = 0.00 |
| Deriva entre sesiones (misma pregunta) | sin medición | ~0 | **1.00 de deriva** (0.00 de dedup): brecha de F4, medida |
| Reaperturas disparadas por evidencia exógena | sin medición | ~100% | 3/3 = 1.00 |
| Reabiertas que terminan `REFUTED` | sin medición | alto (el registro filtra basura) | 1/1 = 1.00 |
| Reabiertas que se auto-sostienen | sin medición | ~0 (bucle endógeno) | 0/6 = 0.00 |

**Gate de salida:** redacción de hedges, umbrales y motor adversarial se
tocan **solo** con estos números sobre la mesa.

### Resultado: F6 completa (2026-09-29)

Implementado, con el detalle de qué es real y qué está scripteado en
`docs/MEDICION-CAPA-EPISTEMICA.md` (generado, no escrito a mano) y el relato en
`HISTORY.md`:

```
kal-in/agent_core/epistemics/measurement/
  corpus.py        corpus como DATOS + validación fail-closed + huella
  harness.py       ejecución por el camino real, métricas, estructurales, calibración
  report.py        render a markdown desde el mismo reporte que el JSON
  corpus/*.json    A · B · C · R · D (evaluación) + K (calibración, disjunto)
kal-in/scripts/measure_epistemics.py     --escribir / --verificar
kal-in/tests/test_epistemic_measurement.py   19 tests: pisos, reproducibilidad,
                                             huecos conocidos, gate de salida,
                                             y el gate por la ruta REAL (/chat
                                             con AgentLoop real y LLM scripteado)
kal-in/docs/MEDICION-CAPA-EPISTEMICA.md  reporte generado
kal-in/docs/medicion-capa-epistemica.json  registro canónico (lo compara la suite)
```

Lo que la fase cambió en el sistema, y con qué número:

1. **Marcadores de hedge: 12 → 24.** Con los originales, 7 de 18 hedges del
   corpus NO se reconocían; con un déficit declarado, esos 7 se **bloqueaban**
   (castigar al modelo por ser honesto). Regla mecánica, no criterio: se agrega
   todo candidato que elimine un falso negativo y no agregue ninguno. Matriz:
   11/7/1/17 → **18/0/1/17** (exactitud 0.78 → 0.97). El falso positivo que
   queda (`podría` sobre una afirmación disfrazada de duda) sigue medido y a la
   vista en K y en A12.
2. **Umbral de respuesta larga: 240 → 64.** Con 240, R2 marcaba 1 de cada 10
   afirmaciones sin respaldo del corpus: en la práctica no existía.
3. **R2 exige que la respuesta NO admita incertidumbre.** La calibración
   destapó que, con el umbral bajo, la regla le ponía el cartel de "sin
   verificación" a 8 de 8 respuestas que ya decían "no lo sé". Precio medido:
   A12 deja de marcarse.
4. **El gate de salida es mecánico:** un test exige que los marcadores y el
   umbral de `gate.py` coincidan con lo que el reporte dice haber medido, y que
   el JSON commiteado siga coincidiendo con el código y el corpus.

**Dos hallazgos, los dos cerrados** (D10 el mismo día, D9 al resolver el backend
de F4):

- **CERRADO (D9) — el déficit declarado por la tool no tiene condición de pago.**
  El mecanismo existe y funciona por el camino de código, pero `open_question`
  crea el déficit sin condiciones → `unsatisfied_deficits()` nunca lo suelta →
  R1 queda enclavado y **bloquea la respuesta correcta** después de que llegó la
  evidencia (medido: 1 déficit pendiente y gate `bloquear` con la evidencia que
  matchea). Era abstención falsa causada por el cableado, no por el gate: con
  `resolved_by` en `open_question` la deuda se salda y el caso 3 mide 0
  pendientes y `permitir`.
- **CERRADO (D10) — el orden de las condiciones dejó de importar.** Eran dos
  problemas apilados: el dedup de "evidencia ya aplicada" era por hipótesis (así
  que la segunda condición se descartaba como si fuera una repetición) y
  `_aplicar()` pisaba el estado sin regla de precedencia (así que ganaba la
  última condición de la tupla). Ahora el dedup es por (condición, evidencia) —el
  invariante de F1 intacto— y la precedencia es explícita: **`REFUTE > WEAKEN >
  REOPEN`**, los vínculos de todas se registran igual, y el presupuesto de
  reaperturas se consume una vez por evidencia. El reporte lo mide en los dos
  órdenes de declaración: `orden_independiente: true`, mismos dos vínculos,
  mismo `Flag.CONTRADICTED`, mismo estado (`refuted`).

---

## F7 — Costura seguridad ↔ epistemología ✅ *hecho*

**Objetivo:** probar las propiedades que existen **porque las dos capas se
tocan**, y nada más que esas. Nace de una observación de revisión: *"la
arquitectura epistemológica ya está empezando a comportarse como una arquitectura
de seguridad… una alucinación puede ser completamente válida desde el punto de
vista del software"*. Es correcto, y este proyecto ya tiene el dato: los dos
huecos medidos de F6 (A10, A12) son exactamente eso — software-válido y
epistémicamente insostenible.

**Encuadre, porque de él depende qué se puede prometer.** No es una arquitectura
de *seguridad* (esa protege al host de un adversario: confidencialidad,
autorización, aislamiento) sino de **integridad**: protege al usuario de las
afirmaciones que el sistema no puede sostener, y —en otro eje— del contenido
adversarial que entra por un canal legítimo. Comparte la gramática (fail-closed,
capacidad restringida, defensa en profundidad) porque la integridad se construye
igual, pero el modelo de amenazas no es el mismo. Y la frase que mejor la
describe no es "guardar lo que el modelo piensa sin confiar en el modelo" sino
**guardarlo sin necesitar confiar**: no hay detector de confianza ni lo habrá —
lo que hay es procedencia obligatoria y ninguna forma de promover creencia por
repetición, antigüedad o supervivencia.

**El puente, en una pieza de código:** el ancla de auditoría. `anchor_kind:
audit_event_hash` prueba a la vez "esto corrió bajo mediación del kernel"
(seguridad) y "esta procedencia no se puede fabricar" (epistémico), porque el
modelo no puede escribir en el log encadenado por HMAC. No es una analogía entre
las dos arquitecturas: es un solo token con dos lecturas.

**Qué se probó** (`tests/test_epistemic_seam.py`, 9 sin Docker + 1 con Docker):

| Propiedad | Qué la hace de la costura |
|---|---|
| Una observación que pasó el contenedor entra como `TOOL_EXTERNAL` y con ancla real | "Limpio de seguridad ≠ creíble", en código |
| Un ancla escrita en el texto NO se convierte en ancla | El contenedor no puede fabricar procedencia (principio 2) |
| Contenido hostil se guarda como DATO y no mueve el estado salvo por una condición declarada | Une inyección (seguridad) con reapertura (epistémico) |
| Una invocación que no produjo contenido no produce evidencia | El bug de composición que encontró F5, como test permanente |
| El contenido externo no puede llegar al matcher como patrón | Une ReDoS (seguridad) con el matcher (epistémico) |
| El ancla encadena con la entrada anterior del log | El mismo token prueba las dos cosas |

**El arreglo general que salió de acá.** F5 había parcheado el bug con una lista
de nombres de herramientas (`TOOLS_DONDE_EL_FALLO_NO_ES_EVIDENCIA`). F7 la
eliminó: la decisión se mudó al camino que sabe qué pasó (`_dispatch_tool`), y
usa el **tier** del wrapper (que la cascada calcula por TIPO, nunca leyendo el
manifiesto autodeclarado) y el **`status` del artefacto** (que es lo que el
runner sabe de verdad). Sin listas que se pudren y sin heurística sobre el texto
del mensaje: un `run_code` que corre y falla sigue ingresando su stderr como
observación, porque ahí el error ES el dato.

**Cautelas, escritas en la suite y no solo acá:**

- **No se duplica la suite del kernel.** Escape de sandbox, hardening de imagen y
  aislamiento de red ya están probados del otro lado; repetirlos acá sería
  duplicación que se pudre (la lección del drift entre kal-in y kal). Solo se
  prueban las seis propiedades de arriba.
- **No vuelve detectable la alucinación.** A10 y A12 siguen medidos y abiertos: el
  gate no juzga verdad, y F7 no cambia eso.
- **La verificación de punta a punta necesita sandbox sano.** El e2e está marcado
  `requires_docker`; en el entorno actual falla por el `/tmp` privado del harness
  hasta que se corre con `TMPDIR` compartido — causa identificada y arreglada en la
  "Nota de entorno" al final de F8, donde además ese e2e pasa con contenedor real. Con dobles fieles del lado del kernel —wrapper real, sandbox
  guionado— las propiedades quedan verificadas; lo que falta cuando el sandbox no
  corre es la confirmación con contenedor de verdad, y eso está dicho en el
  docstring de la suite en vez de disimulado.

**Dónde iría el arreglo ideal, y por qué no está acá.** La señal de "esto no
llegó a ejecutarse" la conoce el kernel: lo correcto a futuro es que el artefacto
lo declare desde `SandboxedSkillTool` (una clave en la metadata, junto a
`status`). Eso es un archivo COMPARTIDO con kal
(`kernel/registry/sandboxed_skill.py`), así que va por el proceso de
sincronización documentado y no de prepo: mientras tanto la regla vive en el
host, que es donde ya vive la política de ingestión.

---

## F8 — Introspección, alternativas y validez ✅ *completa*

**De dónde sale.** De una revisión de literatura que buscaba qué existe ya
construido en esta dirección. Se verificaron las nueve referencias: siete existen
con el título y el venue citados, dos no se pudieron confirmar (ver
"Antecedentes" abajo). De todo eso se rescataron dos ideas implementables y se
descartó lo demás con motivo. El criterio de selección fue uno solo: **¿se puede
hacer sin inventar números que nadie puede computar?** Tres de los trabajos son
bayesianos (posteriors, Noisy-OR, grados de creencia) y quedaron afuera por eso,
no por novedad: nuestro §0 ya prohíbe `confidence: float`, y F6 midió qué pasa
cuando un umbral no está calibrado con datos reales.

### A. Monitores introspectivos ✅ *hecho*

De Herd, Kelly y Stolle, *Temporal Monitoring of Agent Beliefs Under Uncertainty
with Subjective LTL* (AAAI-SS 2026): la idea de monitores que supervisan la
evolución de las creencias propias. Se trajo la pregunta, no el formalismo.

`kal_epistemics/diagnosis.py::diagnosticar()` chequea **lo que la carga de un
snapshot NO chequea**. `from_snapshot` valida estructura (referencias, esquema,
claves canónicas) y `validate()` impide los estados sin respaldo exógeno, pero un
estado estructuralmente impecable puede afirmar que una hipótesis se reabrió siete
veces sin historial que lo explique — y el contador de reaperturas es justamente
lo que acota el ruido (R8). Cinco invariantes: `reaperturas_vs_historial`,
`estado_declarado`, `aplicado_coherente`, `deficit_saldado`,
`techo_de_reaperturas`.

Tres decisiones de diseño que importan más que la lista:

- **No re-chequea las reglas de escritura.** Si lo hiciera, habría dos dueños de
  la misma regla y el día que cambie una, la otra miente.
- **"No verificable" no es "roto".** Con el historial podado por compactación, los
  invariantes que dependen del detalle se reportan como no verificables **con su
  motivo**, y el ledger sigue siendo sano. Confundir "no lo sé" con "está roto" es
  el error que esta capa existe para no cometer.
- **Reporta, no rechaza.** Un ledger 99% sano sirve; lo que no se hace es
  gobernar en silencio. El ledger durable diagnostica al abrir, el host lo loguea
  como `WARNING`, y la fachada lo devuelve junto con `reanudar`.

**Un bug real que apareció escribiendo los tests, y del mismo tipo que D10.** Una
primera versión derivaba el estado leyendo las entradas del historial en orden.
Cuando la misma evidencia dispara dos condiciones, `_aplicar()` deja **una entrada
por lectura** y el estado lo decide la precedencia, así que con las condiciones
declaradas en el orden inverso el monitor derivaba `reactivated` donde el ledger
dice `refuted` y **acusaba al ledger de un problema del monitor**. La derivación
ahora agrupa por aplicación y usa `PRECEDENCIA_DE_ACCIONES` (que pasó a ser
pública: es una regla del contrato, y quien la lee tiene que leer la misma fuente
que quien la aplica). Hay un test que fija que las dos declaraciones dan el mismo
resultado.

### B. Trayectoria ✅ *hecho*

`ledger.trayectoria_de(hid)` deriva la biografía de una hipótesis del historial ya
grabado: transiciones con `desde`/`hacia`, motivo, evidencia, condición y las
**lecturas** de una misma evidencia. Una hipótesis `reactivated` puede haber
pasado por `refuted` y vuelto, y esconderlo detrás del estado actual es la forma
más fácil de que parezca más firme de lo que es. Si el historial fue podado, la
trayectoria lo dice (`historial_podado`, `entradas_omitidas`): una trayectoria
incompleta no puede leerse como si fuera completa. No toca el expediente del host,
así que la medición de F6 no se mueve.

### C. Exclusión mutua entre alternativas ✅ *hecho*

De BeliefMem (arXiv 2605.05583), cuyo mecanismo central es que las candidatas
sigan visibles juntas. Nosotros ya mostramos todas las hipótesis de una pregunta,
pero **no tenemos la relación "H1 y H2 son alternativas excluyentes"**, y sin ella
no podemos nombrar el estado que más debería alarmarnos: dos hipótesis
incompatibles **ambas respaldadas por evidencia exógena**. Hoy cada una se ve bien
por separado, y `UNDETERMINABLE` no lo cubre (eso es falta de evidencia, no
contradicción entre dos respaldos).

- Qué se agrega: `Hypothesis.excludes` (simétrico) o un "como mucho una" por
  pregunta, `Flag.CONFLICTING_SUPPORT`, y el conflicto visible en el expediente.
- **El consumidor tiene que venir incluido.** Sin la superficie en el gate y el
  expediente, esto sería infraestructura especulativa, que es lo que el proyecto
  prohíbe. O sea: el costo real no es el campo, es el camino completo más un ítem
  de corpus que ejercite el conflicto, más volver a correr la medición para
  mostrar que los umbrales no se mueven en un corpus sin conflictos.

### D. Validez temporal evaluable (D1) ✅ *hecho* · D2 sigue esperando medición

De ARTEM (AAAI-40) y del monitoreo temporal. **Auditando el código, acá estamos
más atrás de lo que una tabla comparativa sugiere**: `validity_conditions` es una
tupla de strings que se registra, se persiste, se compara al compactar y **nadie
evalúa** (grep: cero evaluadores, un solo fixture de test). O sea que hoy no
tenemos condiciones temporales: tenemos un gancho.

- Qué se agrega: validez estructurada (`valido_desde`, `valido_hasta`) + un flag
  derivado (`EXPIRED`) visible donde ya se ven los flags.
- **D1 (recomendado): el vencimiento MARCA, no cambia el estado.** El ledger solo
  cambia estado por las reglas declaradas; que un dato envejezca no es una
  observación nueva. El flag se ve, el gate decide, y no hay movimiento silencioso.
- **D2: el vencimiento dispara una acción declarada** (como una condición más).
  Más potente y más caro: hay que integrarlo al presupuesto de reaperturas (R8) y
  a la precedencia, y decidir si el tiempo puede consumir presupuesto.
- **Alcance espacial: no todavía.** Mapear "alcance" a los ámbitos de D5
  confundiría dos cosas distintas (dónde se guarda el estado vs. dónde vale una
  proposición). Primero el tiempo, que es mecánico y testeable; el alcance
  necesita su propio concepto, no un reuso.

### El costo compartido: el bump de `schema_version` ✅ *resuelto: opción (c)*

C y D1 agregaron campos al snapshot, así que `SCHEMA_VERSION` subió 1 → 2. Y como
`from_snapshot` falla cerrado ante una versión distinta, el bump necesitaba plan de
lectura:

| Opción | Decisión |
|---|---|
| (a) Script de migración `v1 → v2` | Descartada: mantenimiento fantasma para algo que el loader resuelve leyendo |
| (b) Aceptar perder los ledgers viejos | Descartada: el ledger ES el activo que esta capa existe para no perder |
| (c) **Leer v1 y v2, escribir v2** | **Elegida.** `VERSIONES_LEGIBLES = (1, 2)`; v3+ sigue rechazándose fail-closed |

La promesa de fallar cerrado no se debilitó: sigue significando lo mismo —**no
reinterpretar un esquema que NO sabemos leer**— y deja de significar "tirar el
estado que sí sabemos leer y es de ayer". El test que lo fija carga un v1 real (el
mismo snapshot sin las claves que v1 no emitía), verifica que se re-guarda como v2
y —lo importante— que el resultado es **idéntico** al que produce el código de hoy,
o sea que el round-trip no perdió nada.

### C y D1, lo que quedó medido

- **C**: dos alternativas incompatibles declaradas, las dos respaldadas por
  evidencia exógena → `conflictos_de_sostén()` devuelve el par, las DOS quedan
  marcadas `conflicting_support`, el gate responde ADVERTIR
  (`alternativas_respaldadas_en_conflicto`) y una respuesta que ya muestra las dos
  NO se marca. El control —dos respaldadas SIN exclusión declarada— no produce
  conflicto: es una pregunta abierta, y para eso ya está la abstención.
- **D1**: una observación con ventana cerrada marca `expired_support` y el estado
  no se mueve; sin ventana declarada, nada se marca. El vencimiento del *sostén* es
  lo que se marca: que una refutación caduque implicaría volver a "no lo sé", que es
  un cambio de estado y por lo tanto D2.
- **Re-medición**: el ítem C05 entra al corpus y `C.conflicto_registrado` sigue en
  1.0 sobre 5 ítems; `C.no_bloquea` baja de 1.0 a 0.8, que es el resultado buscado
  (ese ítem es exactamente el que el gate tiene que marcar). Ningún otro ítem del
  corpus cambió de resultado: R3 no toca nada donde no hay conflicto. La huella del
  corpus cambió, y con ella el reporte.

### Un hallazgo que vale como lección (y por qué C casi nace muerta)

La primera versión de C definía el conflicto como "las dos hipótesis en estado
`SUPPORTED`". Escribiendo el ítem de corpus apareció que **ningún camino del host
pone `SUPPORTED`** (el modelo no puede autodeclararse respaldado, y el estado lo
cambian la evidencia o un humano): la marca habría sido inalcanzable en una
conversación real — la misma clase de trampa que un detector de ReDoS que no
detecta nada, y que un `validity_conditions` que nadie evalúa. La noción que se usa
es la alcanzable y ya existente: `has_exogenous_support()` (un vínculo de sostén con
evidencia exógena usable), que es exactamente lo que marca `Flag.GROUNDED_IN_EXOGENOUS`.

La lección general: **un flag cuya condición no se puede alcanzar por el camino real
es decoración**, y la forma de detectarlo no es leer el código del flag sino escribir
el caso que debería dispararlo.

### Antecedentes verificados (2026-09-29)

Verificados (título y venue coinciden): [Belief Memory](https://huggingface.co/papers/2605.05583)
(arXiv 2605.05583 — memoria probabilística, Noisy-OR, candidatas múltiples),
[Belief-State Engine](https://arxiv.org/abs/2609.10036) (arXiv 2609.10036 —
posterior bayesiano externo, **solo el posterior al LLM, nunca el historial**),
[Temporal Monitoring… Subjective LTL](https://ojs.aaai.org/index.php/AAAI-SS/article/view/42574)
(AAAI-SS 2026), [ARTEM](https://ojs.aaai.org/index.php/AAAI/article/view/39773)
(AAAI-40, memoria episódica espacio-temporal),
[Bayesian-Agent](https://huggingface.co/papers/2606.08348) (arXiv 2606.08348 —
skills como hipótesis; su motivación es que reusar éxitos y fracasos "as if counts
alone were reliable belief" es un error: es nuestro principio 4, con benchmark), y
*Towards a Belief-Based World Model* (arXiv 2609.00455). AgentCore Episodic Memory
es una feature real de AWS.

**No verificados** (marcados como tales para que nadie los cite de oídas): `SAVeR`
no existe con ese nombre —lo que existe y coincide con la descripción es
[*Verify Before You Commit*](https://aclanthology.org/2026.acl-long.1440/) (ACL
2026)— y la encuesta *From Memory to Belief* (sep 2026) no apareció en ninguna
búsqueda con ese título. Buscando esa encuesta apareció algo **más cercano
nuestro** que varios de los citados:
[*Graph-Native Cognitive Memory for AI Agents: Formal Belief Revision Semantics for
Versioned Memory Architectures*](https://ar5iv.labs.arxiv.org/html/2603.17244)
(arXiv 2603.17244), que es la zona de F1/F4 y merece lectura.

Dos correcciones de encuadre que dejó la revisión:

- **Somos una arquitectura de integridad, no de seguridad** (protege al usuario de
  las afirmaciones que el sistema no puede sostener, no al host de un adversario),
  y la frase correcta no es "sin confiar en el modelo" sino **"sin necesitar
  confiar"**: no hay detector de confianza ni lo habrá.
- **Las tablas comparativas hechas por quien diseñó un lado inflan ese lado.** En
  la que motivó esta fase, kal-in aparecía con ✅ en "condiciones
  espacio-temporales" (son strings sin evaluador), "actualización bayesiana" (no
  existe, y no debe existir) y "motor adversarial" (hay precedencia y falsadores,
  no un motor). La columna honesta tiene tres valores: existe / existe parcial /
  **no existe a propósito**.

---

### Nota de entorno: la causa de los e2e que "no se podían verificar" (2026-09-29)

Desde F5, varias fases cerraban con la misma salvedad: los e2e `requires_docker`
fallaban en este entorno con `python: can't open file '/workspace/main.py'`, el
mismo error que arrastraban los tests de sandbox pre-existentes, y por eso las
propiedades de la costura quedaban "verificadas con dobles fieles" y no con
contenedor de verdad. **La causa está identificada y no es el sandbox.**

El runner escribe el código en un directorio temporal del host y lo bind-montea en
`/workspace`. Este entorno le da a cada shell un **`/tmp` privado** —el demonio de
Docker es un proceso de afuera y no lo ve—, así que el bind montaba un directorio
**vacío** y el contenedor arrancaba sin `main.py`. La prueba: un bind desde `/tmp`
llega vacío y el mismo bind desde el repo funciona.

Consecuencia práctica: con `TMPDIR` apuntando a una ruta compartida (por ejemplo
`data/tmp`, que está en `.gitignore`), los e2e corren de verdad. Verificado: los 3
e2e `requires_docker` de F5/F2b/F7 pasan con contenedor real, y el test que ya
existía para `workdir_root` (que era parte de los que fallaban) también. Los
"49 fallos pre-existentes" del entorno eran, en su gran mayoría, esta única causa.

**Y correr el contenedor de verdad encontró un bug real de F2b**, que ningún doble
podía encontrar: el runner carga la skill **por ruta de archivo**
(`spec_from_file_location("/workspace/skill/tool.py")`) y **no** agrega su carpeta a
`sys.path`. La librería vendorizada vive al lado de `tool.py`, así que dentro del
contenedor `import kal_epistemics` fallaba con `ModuleNotFoundError`: la fachada no
habría arrancado nunca. Los tests en proceso no lo veían porque ahí el repo ya tiene
el paquete importable, y el test de "proceso limpio" tampoco porque **le regalaba a
la skill la condición que en el contenedor no tiene** (`sys.path.insert(0, skill_dir)`
+ `from tool import ...`). Las dos cosas están arregladas: la skill se asegura de que
su propia carpeta sea importable, el test ahora imita al runner, y **el runner mismo
agrega la carpeta de la skill a `sys.path`** (el fix en la raíz, que cubre a cualquier
skill que vendorice un módulo hermano). Ojo con ese archivo: es compartido con kal, así
que el cambio va por el proceso de sincronización y queda pendiente de portar. Y una
observación sobre la forma: conviene `sys.path.append()` y no `insert(0, ...)`, para
que la carpeta de la skill no quede por delante de la stdlib y del `sdk/`. Lección de método:
un test que arma las condiciones más favorable que el entorno real no prueba la
portabilidad — prueba que el test sabe armar el entorno.

## 3. Lo que NO se hace (y en qué fase)

- **F0–F2:** nada de persistencia, nada de red, nada de puntajes numéricos
  multidimensionales, nada de UI.
- **F3:** nada de política de contenido en el kernel, nada de hipótesis en
  `kal`.
- **F4:** nada de grafo de hipótesis como base de datos de grafos — las
  relaciones se **derivan** de los ids, no se almacenan y mantienen aparte.
- **F5:** nada de "el verificador opina"; solo devuelve evidencia con
  procedencia.
- **F6 en adelante:** nada de motor adversarial separado hasta tener la
  medición; si es el mismo modelo preguntando lo mismo, es una segunda
  opinión correlacionada, no independiente. **La medición ya existe (F6) y no
  la habilita:** los números que quedan abiertos son de cableado (D9, D10), no
  de falta de una segunda opinión.
- **Siempre:** ninguna hipótesis del ledger como evidencia de otra
  hipótesis. Ahí `level` sube y la confianza **no**.

---

## 4. Riesgos y mitigaciones

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | El ledger se vuelve un cementerio que nadie consulta | F2 (reapertura intra-tarea) **antes** de F4 (persistencia): el valor se prueba donde la reapertura realmente ocurre |
| R2 | Las hipótesis se contaminan al camino de conocimiento y el sistema cita alucinaciones | Store y accessor separados desde el día uno; test de no contaminación en F4 |
| R3 | Teatro epistémico: campos inventados con apariencia de rigor | Todo campo endógeno marcado; procedencia anclada a `event_id` (F3); `SUPPORTED` imposible sin evidencia exógena (F0) |
| R4 | Abstención excesiva: el sistema se vuelve inútil | Conjunto B de F6 medido desde F2, no al final |
| R5 | El gate se vuelve salteable con un refactor | Test de ruta de salida en F2 y en CI |
| R6 | Costo y latencia por turno | Gate barato por defecto; tope de items y de tokens en el bloque de expediente; verificación externa solo cuando aporta |
| R7 | Deriva del esquema al aparecer datos reales | `schema_version` desde F0 + migraciones |
| R8 | Bucles de reapertura | Límite por pregunta + "nunca reabrir sin evidencia exógena nueva" (hash) |

---

## 5. Orden y por qué

1. **F0** congela contratos: es lo más caro de cambiar después, y la doctrina
   del proyecto ya dice que los contratos se preparan ahora.
2. **F1** valida la hipótesis central del diseño (¿se puede decidir la
   reapertura sin el modelo?) al menor costo posible.
3. **F2** pone el mecanismo a producir valor en el loop real, sin persistencia
   — evita construir un cementerio (R1).
4. **F3** en paralelo desde F0: no bloquea a nadie y habilita la procedencia
   infalsificable de todo lo demás.
5. **F4** recién cuando F2 demuestre que el mecanismo se usa.
6. **F5 antes que F2b** (decisión del 2026-09-29): F2b es una fachada *advisory*
   para terceros — útil, pero no cambia nada de lo que kal-in puede hacer hoy,
   porque el gate y las tools ya existen. F5, en cambio, es la pieza que hace que
   el resto del mecanismo tenga algo que reabrir: hasta acá el ledger solo ingería
   lo que pasaba de rebote por las herramientas existentes, y la reapertura
   automática no tenía de dónde sacar evidencia nueva.
7. **F5/F6** en paralelo al final de F4. **Excepción real: F6 se adelantó a F4**
   (decisión del 2026-09-29). El motivo es que F4 persiste lo que F2 produce, y
   persistir un gate sin medir es congelar un heurístico sin saber cuánto falla:
   la fase barata era medir primero, y encontró tres cosas que F4 habría
   heredado sin verlas (D9, D10 y el hueco de A10/A12).

---

## 6. Criterio de terminado

El sistema está terminado cuando se cumplen las tres a la vez:

1. La misma pregunta formulada de tres maneras distintas produce la misma
   abstención, en sesiones distintas, sin intervención del modelo.
2. Ninguna afirmación con procedencia exclusivamente endógena llega al
   usuario como hecho, y el gate no se puede saltear.
3. Una hipótesis abierta hoy se reabre automáticamente cuando llega la
   evidencia que faltaba, sin que el modelo lo decida — y esa evidencia es
   verificable en el audit log.

**Estado medido de cada criterio (F6, 2026-09-29):**

1. **Cumplido.** Intra-sesión: dedup cosmética 1.00 y abstención estable 1.00
   (los tres tipos de abstención son función de (tipo, déficit), no del texto de
   la pregunta). Entre sesiones: la métrica pasó de **0.00 a 1.00** con F4 (D4 +
   D5): la misma pregunta en otra sesión cae en el mismo registro.
2. **Parcial, con dos huecos conocidos y listados**: A10 (afirmación corta sin
   déficit declarado) y A12 (afirmación disfrazada de duda). La parte que sí se
   cumple está medida: 9/10 de los casos con déficit declarado se bloquean, 0
   auto-sostén endógeno, y el gate corre en la ruta real del planner (verificado
   por HTTP con el loop real).
3. **Cumplido.** Reapertura por evidencia exógena 3/3, refutación 1/1,
   repetición 1/1, y las 27 evidencias del corpus ancladas al `event_hash` del
   audit log. La excepción que quedaba era D9 (un déficit declarado sin condición
   de pago no se saldaba nunca) y quedó cerrada con `resolved_by`: el caso 3 de la
   tabla de deuda epistémica mide ahora 0 déficits pendientes y gate `permitir`.

---

## 7. Decisiones abiertas (requieren al usuario)

| # | Decisión | Impacto |
|---|---|---|
| D1 | ~~¿Bloquear o solo degradar la respuesta?~~ **Resuelto: el gate puede bloquear la tarea** (fail-closed duro). Degradar la redacción es el mínimo, no el default | F2: corta y nombra el déficit |
| D2 | ~~¿Reapertura automática o propuesta al usuario?~~ **Resuelto: automática.** El ledger cambia el estado solo, sin intervención del modelo ni del usuario | El automatismo está en el ESTADO, nunca en lo que se dice: la afirmación resultante sigue pasando por el gate |
| D3 | ~~¿Repo nuevo o subpaquete de `kal-in`?~~ **Resuelto:** subpaquete `kal-in/kal_epistemics/`, escrito para poder extraerse sin tocar una línea | — |
| D4 | ~~¿Backend del ledger?~~ **Resuelto: SQLite propio** en `kal_epistemics/sqlite_store.py` + `data/epistemics/ledger.db` (stdlib, WAL, transacciones, archivo propio; el precedente es `mid_term.py`). JSONL quedaba inventando el protocolo de concurrencia que el port no quiere inventar, porque `guardar()` reemplaza el snapshot completo | Cerrado en F4 |
| D5 | ~~¿Por sesión, por usuario, o global?~~ **Resuelto: ámbito por instalación** con la clave explícita (`AMBITO_POR_DEFECTO`). Las sesiones de una instalación comparten el registro; el aislamiento pasa a ser por ámbito (dos ámbitos no se ven) y con multiusuario se cambia el valor de la clave sin migrar la forma | Cerrado en F4 |
| D6 | ~~¿La skill `claim_verifier` sale al market público o queda de primera parte?~~ **Resuelto: de primera parte, no sale al market.** Un verificador de terceros podría envenenar justo el canal que existe para prevenir alucinaciones: el sistema trataría su salida como evidencia exógena y con un autor ajeno no hay forma de auditar qué trajo. La firma y la reputación de terceros quedan para cuando haya un motivo real | Cerrado en F5 |
| D7 | ~~¿Se acepta algún `confidence: float`?~~ **Resuelto: no.** Se implementaron flags derivados (`grounded_in_exogenous`, `endogenous_only`, `contradicted`, `circular`, `unrooted`, `no_falsifier`, `dependency_refuted`, `unverifiable_provenance`), con un test que falla si alguien agrega los campos | — |
| D11 | ¿Cómo se hace un bump de `schema_version` sin perder los ledgers guardados? | **Resuelto: leer v1 y v2, escribir v2** (`VERSIONES_LEGIBLES`); v3+ sigue fail-closed. Un script de migración es mantenimiento fantasma para algo que el loader resuelve leyendo, y tirar el estado es perder el activo | F8 |
| D8 | ~~¿Qué rutas cubre el bloqueo duro?~~ **Resuelto: solo la del planner/agent loop.** Las demás pasan por el choke point pero no se bloquean | F2b |
| D9 | ~~¿El déficit de `open_question` tiene condición de pago?~~ **Resuelto: sí, con `resolved_by`** (marcadores, camino no confiable con `from_untrusted`), simétrico a `watch_for` de `record_hypothesis`. Medido: el caso 3 de la tabla de deuda epistémica pasó de 1 déficit pendiente y gate `bloquear` a 0 pendientes y gate `permitir`. Sin condición de pago la deuda sigue siendo para siempre, y el ledger no la adivina | Cerrado en F4 |
| D10 | ~~¿El dedup de evidencia aplicada es por hipótesis o por condición?~~ **Resuelto: por (condición, evidencia), con precedencia explícita `REFUTE > WEAKEN > REOPEN`.** Una observación que dice dos cosas deja los dos vínculos y el estado más cauto, sin depender del orden de declaración (medido: `orden_independiente: true`); el invariante de F1 no se toca | Cerrado en F6 |

**F2 desbloqueada:** D1 y D2 quedaron resueltas. Las salvaguardas del
automatismo de D2 son estructurales, no de política: la reapertura solo se
dispara con evidencia exógena NUEVA (dedup por hash), nunca con texto del
propio modelo. Falta igual el límite por pregunta de R8, para que un flujo
conversacional de evidencia no reabra la misma hipótesis cincuenta veces.

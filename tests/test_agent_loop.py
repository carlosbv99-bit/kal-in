"""
Tests de agent_core/llm/agent_loop.py.

Usan un OllamaClient falso (respuestas guionadas) y un TaskExecutor /
MemoryManager falsos — lo que se prueba aquí es la LÓGICA del loop
ReAct (cuándo se detiene, cómo despacha herramientas, cómo maneja
errores), no la integración real con Ollama ni con Docker. Esa
integración real vive en test_agent_loop_integration.py y requiere
Ollama corriendo de verdad.
"""
from __future__ import annotations

from agent_core.llm.agent_loop import SYSTEM_PROMPT, AgentLoop, AgentTool, _agent_tool_from_tool
from agent_core.llm.ollama_client import OllamaError
from agent_core.llm.provider import ChatResponse, ToolCall
from sdk.artifacts import Artifact
from sdk.permissions import Permission


class FakeOllamaClient:
    """Devuelve una secuencia guionada de respuestas, una por cada llamada a .chat()."""

    def __init__(self, responses: list[ChatResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, model=None, tools=None, temperature=None):
        self.calls.append({
            "messages": [dict(m) for m in messages], "model": model, "tools": tools, "temperature": temperature,
        })
        if not self.responses:
            raise AssertionError("FakeOllamaClient se quedó sin respuestas guionadas")
        return self.responses.pop(0)


class FakeTask:
    def __init__(self, status, result=None, error=None):
        self.status = status
        self.result = result
        self.error = error


class FakeTaskExecutor:
    def __init__(self, results: list[FakeTask] | None = None):
        self.results = results or []
        self.submitted = []
        self.run_calls = []

    def submit(self, description):
        self.submitted.append(description)
        return object()

    def run_sandboxed(self, task, code, **kwargs):
        self.run_calls.append(code)
        if self.results:
            return self.results.pop(0)
        from task_execution.task import TaskStatus
        return FakeTask(status=TaskStatus.SUCCESS, result="ok")


class FakeMemoryManager:
    def __init__(self):
        self.remembered = []

    def remember(self, content, metadata=None):
        self.remembered.append(content)

        class Item:
            id = "fake-id"

        return Item()

    def recall(self, query, top_k=3):
        return {"short_term": [], "mid_term": [], "long_term": []}


def _loop(llm_responses, task_executor=None, memory=None, tools=None) -> tuple[AgentLoop, FakeOllamaClient]:
    fake_llm = FakeOllamaClient(llm_responses)
    loop = AgentLoop(
        llm_client=fake_llm,
        task_executor=task_executor or FakeTaskExecutor(),
        memory=memory or FakeMemoryManager(),
        tools=tools,
    )
    return loop, fake_llm


def test_direct_answer_without_tool_calls():
    loop, fake_llm = _loop([ChatResponse(content="La respuesta es 4.")])

    result = loop.run("¿Cuánto es 2+2?")

    assert result.status == "success"
    assert result.final_answer == "La respuesta es 4."
    assert result.steps == []
    assert len(fake_llm.calls) == 1


def test_a_failed_tool_call_attempt_is_never_shown_as_the_final_answer():
    # BUG REAL ENCONTRADO EN USO (2026-07-21): un saludo simple hizo que
    # el modelo devolviera '{"name": null, "arguments": {}}' como
    # respuesta final — _extract_fallback_tool_call ya lo rechaza
    # correctamente (name=null no es ninguna herramienta), pero antes de
    # este fix ese texto rechazado se mostraba tal cual al usuario. Ahora
    # se le devuelve como error al modelo y el loop sigue.
    responses = [
        ChatResponse(content='{"name": null, "arguments": {}}'),
        ChatResponse(content="¡Hola! Estoy bien, ¿y vos?"),
    ]
    loop, fake_llm = _loop(responses)

    result = loop.run("Hola, ¿cómo estás?")

    assert result.status == "success"
    assert result.final_answer == "¡Hola! Estoy bien, ¿y vos?"
    assert len(fake_llm.calls) == 2
    # El segundo llamado al LLM debe incluir el error explicándole al
    # modelo que su intento anterior no era válido.
    second_call_messages = fake_llm.calls[1]["messages"]
    assert "ERROR" in second_call_messages[-1]["content"]


def test_a_malformed_pseudo_tool_call_with_a_real_tool_name_is_never_shown_as_the_final_answer():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-25): "el tiburón en esta imagen
    tiene dos colas y no tiene cabeza, corregilo" hizo que el modelo
    devolviera prosa + un bloque ```json``` con "name": "image_editing"
    (una herramienta REAL) pero JSON de verdad inválido — un comentario
    "//" estilo JS y un placeholder [x1, y1, x2, y2] sin comillas en vez
    de números reales. Ni _extract_fallback_tool_call (JSON no parsea)
    ni la versión angosta previa de _looks_like_a_failed_tool_call_attempt
    (requiere que el JSON SÍ parsee para mirar "name") detectaban esto
    — el texto crudo (prosa + JSON roto) se mostraba tal cual como si
    fuera la respuesta final real, sin ejecutar nada.
    """
    image_editing_tool = AgentTool(
        name="image_editing", description="d", parameters_schema={"type": "object", "properties": {}},
        handler=lambda **kw: "editado",
    )
    malformed = (
        "Entiendo. Para corregir la imagen, usaré la herramienta correspondiente.\n\n"
        "```json\n"
        "{\n"
        '  "name": "image_editing",\n'
        '  "arguments": {\n'
        '    "image_path": "data/artifacts/uploads/x.png",\n'
        '    "operation": "inpaint",\n'
        "    \"box\": [x1, y1, x2, y2],  // Estos valores deben ser estimados a ciegas.\n"
        '    "prompt": "agregar una cabeza de tiburón"\n'
        "  }\n"
        "}\n"
        "```"
    )
    responses = [
        ChatResponse(content=malformed),
        ChatResponse(content="No tengo forma de estimar las coordenadas exactas — ¿podés indicarme la posición del tiburón?"),
    ]
    loop, fake_llm = _loop(responses, tools=[image_editing_tool])

    result = loop.run("el tiburón en esta imagen tiene dos colas y no tiene cabeza, corregilo")

    assert result.status == "success"
    assert result.final_answer == "No tengo forma de estimar las coordenadas exactas — ¿podés indicarme la posición del tiburón?"
    assert len(fake_llm.calls) == 2
    assert "ERROR" in fake_llm.calls[1]["messages"][-1]["content"]


def test_a_completely_empty_response_is_never_shown_as_the_final_answer():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-28, probando qwen3.5:9b-q4_K_M):
    tras un run_code exitoso, la siguiente llamada del modelo devolvió
    content='' y tool_calls=[] — algunos modelos separan su razonamiento
    en un campo "thinking" aparte y a veces no llegan a producir ningún
    content real. Sin este fix, esa respuesta vacía se aceptaba tal cual
    como "respuesta final", dejando al usuario sin absolutamente nada
    pese a que la tarea ya se había completado.
    """
    responses = [
        ChatResponse(content=""),
        ChatResponse(content="El resultado es 55."),
    ]
    loop, fake_llm = _loop(responses)

    result = loop.run("ejecutá esto: print(sum(range(1,11)))")

    assert result.status == "success"
    assert result.final_answer == "El resultado es 55."
    assert len(fake_llm.calls) == 2
    assert "ERROR" in fake_llm.calls[1]["messages"][-1]["content"]


def test_an_empty_response_that_never_recovers_exhausts_max_steps_instead_of_looping_forever():
    responses = [ChatResponse(content="")] * 3
    loop, fake_llm = _loop(responses)

    result = loop.run("algo", max_steps=3)

    assert result.status == "max_steps_exceeded"
    assert len(fake_llm.calls) == 3


def test_a_normal_plain_text_answer_is_not_mistaken_for_a_failed_tool_call():
    loop, fake_llm = _loop([ChatResponse(content="La respuesta es 4.")])

    result = loop.run("¿Cuánto es 2+2?")

    assert result.status == "success"
    assert result.final_answer == "La respuesta es 4."
    assert len(fake_llm.calls) == 1


def test_single_tool_call_then_final_answer():
    task_executor = FakeTaskExecutor()
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "print(4)"})]),
        ChatResponse(content="El resultado es 4."),
    ]
    loop, fake_llm = _loop(responses, task_executor=task_executor)

    result = loop.run("Calcula 2+2 con código")

    assert result.status == "success"
    assert result.final_answer == "El resultado es 4."
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "run_code"
    assert task_executor.run_calls == ["print(4)"]
    # La segunda llamada a Ollama debe incluir el resultado de la herramienta
    assert fake_llm.calls[1]["messages"][-1]["role"] == "tool"


def test_reconstructed_tool_call_arguments_stay_as_a_dict_ollama_native_format():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-19): esto antes serializaba
    `arguments` a un string JSON acá mismo, para satisfacer a Groq (que
    sí lo exige así, ver test_openai_compatible_client.py). Pero Ollama
    NATIVO rechaza ese formato con 400 ("Value looks like object, but
    can't find closing '}' symbol") en cualquier turno posterior a una
    llamada a herramienta — confirmado en vivo contra un Ollama real
    (qwen2.5-coder:14b), rompía hasta con un solo tool call de por
    medio ("hola" -> audio_generation -> segundo turno ya fallaba).
    agent_loop.py arma el formato CANÓNICO (dict) — cada proveedor
    concreto adapta a su propio wire format en su propio cliente
    (ver OpenAICompatibleClient._with_stringified_tool_call_arguments).
    """
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "print(4)"})]),
        ChatResponse(content="El resultado es 4."),
    ]
    loop, fake_llm = _loop(responses, task_executor=FakeTaskExecutor())

    loop.run("Calcula 2+2 con código")

    assistant_message = fake_llm.calls[1]["messages"][-2]
    assert assistant_message["role"] == "assistant"
    sent_arguments = assistant_message["tool_calls"][0]["function"]["arguments"]
    assert sent_arguments == {"code": "print(4)"}


def test_a_missing_tool_call_id_is_generated_and_correlated_with_the_tool_message():
    """
    BUG REAL ENCONTRADO EN USO: Groq exige 'id' en cada tool_call del
    mensaje assistant Y el 'tool_call_id' correspondiente en el mensaje
    role="tool" que le responde — sin esto rechazaba con 400
    ("property 'id' is missing") cualquier turno posterior a una
    llamada a herramienta. Ollama no siempre manda un id (ToolCall.id
    queda None) — agent_loop.py debe generar uno y usarlo consistente
    en ambos mensajes.
    """
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "print(4)"})]),
        ChatResponse(content="El resultado es 4."),
    ]
    loop, fake_llm = _loop(responses, task_executor=FakeTaskExecutor())

    loop.run("Calcula 2+2 con código")

    sent_messages = fake_llm.calls[1]["messages"]
    assistant_message = sent_messages[-2]
    tool_message = sent_messages[-1]

    generated_id = assistant_message["tool_calls"][0]["id"]
    assert generated_id  # no vacío, no None
    assert assistant_message["tool_calls"][0]["type"] == "function"
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == generated_id


def test_multiple_sequential_tool_calls():
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="remember", arguments={"content": "dato importante"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="recall", arguments={"query": "dato"})]),
        ChatResponse(content="Listo, ya lo recordé y lo busqué."),
    ]
    memory = FakeMemoryManager()
    loop, _ = _loop(responses, memory=memory)

    result = loop.run("Recuerda esto y luego búscalo")

    assert result.status == "success"
    assert len(result.steps) == 2
    assert memory.remembered == ["dato importante"]


def test_unknown_tool_call_returns_error_observation_not_crash():
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="herramienta_inventada", arguments={})]),
        ChatResponse(content="No pude usar esa herramienta."),
    ]
    loop, _ = _loop(responses)

    result = loop.run("usa una herramienta que no existe")

    assert result.status == "success"  # el loop no crashea
    assert "no existe" in result.steps[0].observation.lower()


def test_tool_with_wrong_arguments_returns_error_observation():
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"argumento_incorrecto": "x"})]),
        ChatResponse(content="Ajusto mi enfoque."),
    ]
    loop, _ = _loop(responses)

    result = loop.run("prueba con argumentos incorrectos")

    assert result.status == "success"
    assert "argumentos inválidos" in result.steps[0].observation.lower()


def test_tool_handler_exception_is_caught_not_propagated():
    def broken_handler(**kwargs):
        raise RuntimeError("boom interno")

    tools = [
        AgentTool(
            name="rota",
            description="una herramienta que siempre falla",
            parameters_schema={"type": "object", "properties": {}},
            handler=broken_handler,
        )
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="rota", arguments={})]),
        ChatResponse(content="Reporto el fallo."),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("usa la herramienta rota")

    assert result.status == "success"
    assert "boom interno" in result.steps[0].observation


# --- Cascada de permisos (sdk/permissions.py::PermissionCascade) ---


def test_tool_requiring_permission_outside_its_trust_tier_is_never_called():
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return "no debería llegar acá"

    tools = [
        AgentTool(
            name="skill_con_red", description="una skill que pide red",
            parameters_schema={"type": "object", "properties": {}}, handler=handler,
            permissions=frozenset({Permission.NETWORK}), trust_tier="skill",  # tier "skill" no cubre network por default
        )
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="skill_con_red", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("usa la skill con red")

    assert calls == []  # el handler NUNCA se llamó
    assert "ERROR" in result.steps[0].observation
    assert "network" in result.steps[0].observation


def test_permission_denial_is_audited(tmp_path, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    tools = [
        AgentTool(
            name="skill_con_red", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: "x", permissions=frozenset({Permission.NETWORK}), trust_tier="skill",
        )
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="skill_con_red", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    loop.run("usa la skill con red")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "permission_denied"
    assert entries[0]["outcome"] == "failure"


def test_session_denied_permissions_blocks_even_when_tier_would_allow():
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return "ejecutada"

    tools = [
        AgentTool(
            name="adaptador_con_red", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=handler, permissions=frozenset({Permission.NETWORK}), trust_tier="system",  # system SÍ cubre network
        )
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="adaptador_con_red", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("usa el adaptador", denied_permissions=frozenset({Permission.NETWORK}))

    assert calls == []
    assert "ERROR" in result.steps[0].observation


def test_tool_without_special_permissions_runs_normally_through_cascade():
    """La cascada no debe romper el caso normal: una herramienta sin
    permisos declarados (default de la mayoría de los AgentTool de test)
    sigue funcionando exactamente igual que antes de que existiera."""
    tools = [
        AgentTool(
            name="simple", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: "ejecutada",
        )
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="simple", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("usa la herramienta simple")

    assert result.steps[0].observation == "ejecutada"


def test_max_steps_exceeded_stops_the_loop():
    # El modelo pide una herramienta indefinidamente, nunca da respuesta final
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "print(1)"})])
        for _ in range(10)
    ]
    loop, fake_llm = _loop(responses)

    result = loop.run("nunca termines", max_steps=3)

    assert result.status == "max_steps_exceeded"
    assert len(result.steps) == 3
    assert len(fake_llm.calls) == 3  # se detuvo exactamente en el límite, no siguió de más


# --- max_tool_repeats: BUG REAL ENCONTRADO EN USO (2026-07-10) ---
# "genera una raqueta de tenis" generó la imagen correcta una vez y
# después 3 imágenes más de paisajes sin relación en el mismo turno,
# sin llegar nunca a una respuesta final. La instrucción de prompt
# sola no alcanzó — este es el tope estructural que no depende de que
# el modelo la respete.


def _counting_tool(calls: list):
    return AgentTool(
        name="image_generation", description="d", parameters_schema={"type": "object", "properties": {}},
        handler=lambda **kw: calls.append(kw) or "imagen generada",
    )


def test_on_step_is_called_for_every_step_including_a_rejected_repeat():
    """
    Recorrido en vivo (2026-07-24, ver agent_core/routers/chat.py::
    GET /chat/progress/{id}): on_step debe enterarse de CUALQUIER paso
    — exitoso, o rechazado por el tope de repeticiones — para que el
    panel en vivo del frontend pueda mostrar incluso un intento
    bloqueado, no solo los que sí se ejecutaron.
    """
    calls: list = []
    tools = [_counting_tool(calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={})])
        for _ in range(2)
    ] + [ChatResponse(content="listo")]
    loop, _ = _loop(responses, tools=tools)
    seen_steps: list = []

    result = loop.run("generá una imagen", max_steps=10, max_tool_repeats=1, on_step=lambda step: seen_steps.append(step))

    assert len(seen_steps) == 2  # el exitoso Y el rechazado
    assert seen_steps[0].observation == "imagen generada"
    assert "ERROR" in seen_steps[1].observation
    assert result.status == "success"


def test_tool_call_beyond_max_tool_repeats_is_rejected_without_executing():
    calls: list = []
    tools = [_counting_tool(calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": f"v{i}"})])
        for i in range(5)
    ] + [ChatResponse(content="listo")]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("generá una imagen", max_steps=10, max_tool_repeats=2)

    # Se ejecutó de verdad solo 2 veces (el límite) — nunca 3, 4 o 5,
    # aunque el modelo (guionado acá) haya insistido esa cantidad de veces.
    assert len(calls) == 2
    assert "ERROR" in result.steps[2].observation
    assert "image_generation" in result.steps[2].observation
    assert result.status == "success"  # el modelo igual pudo terminar bien tras el rechazo


def test_tool_calls_up_to_the_limit_all_execute_normally():
    calls: list = []
    tools = [_counting_tool(calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": f"v{i}"})])
        for i in range(3)
    ] + [ChatResponse(content="listo")]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("generá 3 variantes", max_steps=10, max_tool_repeats=3)

    assert len(calls) == 3
    assert all("ERROR" not in s.observation for s in result.steps)


def test_max_tool_repeats_counts_independently_per_tool_name():
    image_calls: list = []
    other_calls: list = []
    tools = [
        _counting_tool(image_calls),
        AgentTool(
            name="run_code", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: other_calls.append(kw) or "ok",
        ),
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("hacé varias cosas", max_steps=10, max_tool_repeats=2)

    # Las 2 llamadas a image_generation no consumen el cupo de run_code.
    assert len(image_calls) == 2
    assert len(other_calls) == 1
    assert all("ERROR" not in s.observation for s in result.steps)


def test_tool_schema_is_recomputed_each_step_and_excludes_a_tool_once_blocked():
    """
    GAP ESTRUCTURAL IDENTIFICADO (revisión de diseño, 2026-07-30): antes,
    `tool_schemas` se calculaba UNA sola vez antes del loop — una
    herramienta sobre su tope de repeticiones seguía apareciendo en el
    `tools=` que se le manda al LLM en TODOS los pasos siguientes, y el
    único freno era el mensaje de ERROR después del rechazo (barrera de
    honor). Ahora el schema se recalcula en cada paso: una vez que
    'image_generation' llega a su tope, deja de estar presente en las
    llamadas siguientes a self.llm.chat() — barrera física, no un pedido
    de texto.
    """
    calls: list = []
    tools = [_counting_tool(calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={})])
        for _ in range(2)
    ] + [ChatResponse(content="listo")]
    loop, fake_llm = _loop(responses, tools=tools)

    loop.run("generá una imagen", max_steps=10, max_tool_repeats=1)

    def _tool_names(call_index: int) -> set[str]:
        return {t["function"]["name"] for t in fake_llm.calls[call_index]["tools"]}

    # Paso 1: todavía no se llamó a nada, la herramienta está disponible.
    assert "image_generation" in _tool_names(0)
    # Paso 2: la primera llamada ya alcanzó max_tool_repeats=1 — el
    # schema del paso SIGUIENTE ya no la incluye, aunque el modelo
    # (guionado acá) insista en pedirla de nuevo.
    assert "image_generation" not in _tool_names(1)


def test_tool_schema_excludes_a_vscode_only_tool_after_it_already_succeeded_once():
    calls: list = []
    tools = [
        AgentTool(
            name="propose_project_files", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: calls.append(kw) or "Se prepararon 2 archivo(s)",
        ),
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="propose_project_files", arguments={"files": []})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="propose_project_files", arguments={"files": []})]),
        ChatResponse(content="listo"),
    ]
    loop, fake_llm = _loop(responses, tools=tools)

    loop.run("armá los archivos", max_steps=10, max_tool_repeats=5)

    tool_names_after_success = {t["function"]["name"] for t in fake_llm.calls[1]["tools"]}
    assert "propose_project_files" not in tool_names_after_success


def _generative_tool(name: str, calls: list, artifact_dir: str = "data/artifacts/images") -> AgentTool:
    """Simula una herramienta que genera un Artifact real (con .uri) —
    a diferencia de _counting_tool(), necesario para probar el tope más
    estricto por autochequeo (que rastrea artifact.uri -> tool_name)."""
    agent_tool = AgentTool(name=name, description="d", parameters_schema={"type": "object", "properties": {}}, handler=None)

    def handler(**kwargs):
        calls.append(kwargs)
        artifact = Artifact(modality="image", uri=f"{artifact_dir}/{len(calls)}.png", metadata={})
        agent_tool.last_artifact = artifact
        return f"imagen generada en {artifact.uri}"

    agent_tool.handler = handler
    return agent_tool


def _analyze_image_tool(calls: list) -> AgentTool:
    agent_tool = AgentTool(name="analyze_image", description="d", parameters_schema={"type": "object", "properties": {}}, handler=None)
    agent_tool.handler = lambda **kwargs: calls.append(kwargs) or "descripción de la imagen"
    return agent_tool


def test_self_check_via_analyze_image_lowers_the_repeat_limit_to_one_retry():
    """
    BUG REAL ENCONTRADO EN USO: "crea una naranja (solo una)" generó
    bien la primera vez, pero analyze_image sobre esa MISMA imagen
    (SDXL-turbo no respeta cantidades exactas de forma confiable)
    empujó al modelo a regenerar sin ningún límite hasta agotar
    max_steps, sin darle nunca una respuesta al usuario. Tope
    estructural: una vez que analyze_image se llama sobre un artefacto
    generado EN ESTE TURNO, esa herramienta generativa queda limitada a
    2 llamadas totales (la original + un solo reintento) — sin importar
    que max_tool_repeats configurado sea mayor.
    """
    gen_calls: list = []
    analyze_calls: list = []
    tools = [_generative_tool("image_generation", gen_calls), _analyze_image_tool(analyze_calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una naranja"})]),
        ChatResponse(
            content="",
            tool_calls=[ToolCall(name="analyze_image", arguments={"image_path": "data/artifacts/images/1.png", "question": "¿es una sola?"})],
        ),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una naranja de nuevo"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una naranja, tercera vez"})]),
        ChatResponse(content="no logré exactamente una, pero acá está la última versión"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("crea una naranja (solo una)", max_steps=10, max_tool_repeats=5)

    # Solo 2 generaciones reales (la original + 1 reintento) — nunca 3,
    # aunque max_tool_repeats configurado (5) lo permitiría de no ser
    # por el autochequeo detectado.
    assert len(gen_calls) == 2
    assert len(analyze_calls) == 1
    assert "ERROR" in result.steps[-1].observation
    assert "image_generation" in result.steps[-1].observation
    assert result.status == "success"
    # agent_core/routers/chat.py usa esto para deduplicar la imagen
    # ANTERIOR al reintento (ver test_orchestrator_chat_repeated_image_generation.py) —
    # tiene que quedar expuesto en el resultado, no solo local a run().
    assert "image_generation" in result.self_checked_tools


def test_ignoring_the_self_check_rejection_twice_cuts_the_turn_short_instead_of_exhausting_steps():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-25): "crea una calle concurrida
    de moscú" — a diferencia del test de arriba (donde el modelo, tras
    UN rechazo, respondió en texto por su cuenta), acá el modelo
    insistió con 'image_generation' una y otra vez pese al rechazo
    explícito, hasta agotar max_steps SIN darle nunca una respuesta al
    usuario — ni siquiera mencionar que la imagen YA estaba generada.
    Fix: si el modelo ignora el rechazo una SEGUNDA vez (insiste con la
    MISMA herramienta ya tapada), se corta el turno con una respuesta
    sintetizada en vez de seguir gastando pasos reales de cómputo.
    """
    gen_calls: list = []
    analyze_calls: list = []
    tools = [_generative_tool("image_generation", gen_calls), _analyze_image_tool(analyze_calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una calle"})]),
        ChatResponse(
            content="",
            tool_calls=[ToolCall(name="analyze_image", arguments={"image_path": "data/artifacts/images/1.png", "question": "¿es correcta?"})],
        ),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una calle, de nuevo"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una calle, tercera vez"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "una calle, cuarta vez"})]),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("crea una calle concurrida", max_steps=10, max_tool_repeats=5)

    # Nunca se llegó a la 4ta respuesta guionada (el 5to elemento de
    # `responses` de arriba ni existe) — el loop cortó ANTES, en el
    # segundo rechazo, no agotó max_steps=10.
    assert len(gen_calls) == 2  # la original + el único reintento permitido
    assert result.status == "success"  # nunca "max_steps_exceeded"
    assert "image_generation" in result.final_answer
    assert "arriba" in result.final_answer  # le avisa que el resultado real ya está visible


def test_analyze_image_on_an_unrelated_path_does_not_tighten_the_limit():
    """Solo cuenta como autochequeo si analyze_image mira un artefacto
    generado EN ESTE MISMO turno — una imagen subida por el usuario (u
    otra ruta cualquiera) no debe activar el tope más estricto."""
    gen_calls: list = []
    analyze_calls: list = []
    tools = [_generative_tool("image_generation", gen_calls), _analyze_image_tool(analyze_calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "algo"})]),
        ChatResponse(
            content="",
            tool_calls=[ToolCall(name="analyze_image", arguments={"image_path": "data/artifacts/uploads/otra.png", "question": "describí"})],
        ),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "algo más"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "algo más, otra vez"})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("hacé algo", max_steps=10, max_tool_repeats=3)

    # Las 3 llamadas a image_generation corren normal (tope configurado
    # de 3, no el más estricto de 2) — analyze_image miró una ruta que
    # NO es de este turno, no cuenta como autochequeo.
    assert len(gen_calls) == 3
    assert all("ERROR" not in s.observation for s in result.steps)


def test_propose_project_files_is_capped_at_one_call_per_turn():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-20, VS Code): pedido "elaborá un
    menú con fotos y descripción" llamó a propose_project_files 3 veces
    en el mismo turno (revisando su propio intento anterior sin ninguna
    señal real de que hiciera falta — el usuario recién ve/decide sobre
    la propuesta DESPUÉS de este turno, nunca durante). Tope estructural
    a 1 sola llamada — nunca más permisivo que max_tool_repeats, igual
    criterio que el autochequeo de imágenes.
    """
    calls: list = []
    tools = [
        AgentTool(
            name="propose_project_files", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: calls.append(kw) or "propuesta enviada",
        ),
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="propose_project_files", arguments={"files": [{"path": "menu.html", "content": "v1"}]})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="propose_project_files", arguments={"files": [{"path": "menu.html", "content": "v2"}]})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("elaborá un menú con fotos y descripción", max_steps=10, max_tool_repeats=5)

    # Una sola llamada real, aunque max_tool_repeats configurado (5) lo
    # permitiría de no ser por el tope específico de esta herramienta.
    assert len(calls) == 1
    assert "ERROR" in result.steps[-1].observation
    assert "propose_project_files" in result.steps[-1].observation
    assert result.status == "success"


def test_a_failed_attempt_at_a_vscode_only_tool_can_be_retried():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-24, VS Code): "generame un logo
    simple... con un tomate y una hoja de albahaca" hizo que el modelo
    llamara a import_resource con una ruta ABSOLUTA (mal formada) — el
    primer intento falló (ProjectFilesRejectedError) sin proponer nada
    al usuario. El tope de 1 llamada por turno para
    _VSCODE_ONLY_TOOL_NAMES (pensado para bloquear una SEGUNDA
    propuesta después de una YA exitosa) bloqueó también ese segundo
    intento — la única chance real del modelo de corregir su propio
    error — y terminó llamando a una herramienta totalmente distinta y
    equivocada (qr_code) en vez de la correcta (image_generation). El
    tope de 1 ahora solo aplica DESPUÉS de un intento que tuvo éxito de
    verdad.
    """
    calls: list = []

    def handler(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return "ERROR inesperado ejecutando 'import_resource': ruta absoluta"
        return "recurso importado"

    tools = [
        AgentTool(
            name="import_resource", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=handler,
        ),
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="import_resource", arguments={"destination_path": "/tmp/tomato.jpg"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="import_resource", arguments={"destination_path": "assets/tomato.jpg"})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("logo con un tomate", max_steps=10, max_tool_repeats=5)

    # El reintento (con la ruta corregida) SÍ se ejecutó — no se rechazó
    # de entrada solo porque ya se había "intentado" antes.
    assert len(calls) == 2
    assert result.status == "success"


def test_a_vscode_only_tool_still_caps_at_one_call_after_it_already_succeeded():
    """Regresión: el fix de arriba no debe reabrir el bug original
    (propose_project_files/import_resource llamado varias veces DESPUÉS
    de ya haber propuesto algo con éxito)."""
    calls: list = []

    tools = [
        AgentTool(
            name="import_resource", description="d", parameters_schema={"type": "object", "properties": {}},
            handler=lambda **kw: calls.append(kw) or "recurso importado",
        ),
    ]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="import_resource", arguments={"destination_path": "assets/tomato.jpg"})]),
        ChatResponse(content="", tool_calls=[ToolCall(name="import_resource", arguments={"destination_path": "assets/tomato2.jpg"})]),
        ChatResponse(content="listo"),
    ]
    loop, _ = _loop(responses, tools=tools)

    result = loop.run("logo con un tomate", max_steps=10, max_tool_repeats=5)

    assert len(calls) == 1
    assert "ERROR" in result.steps[-1].observation


def test_default_max_tool_repeats_comes_from_settings(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings.llm, "max_tool_repeats", 1)
    calls: list = []
    tools = [_counting_tool(calls)]
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={})])
        for _ in range(3)
    ] + [ChatResponse(content="listo")]
    loop, _ = _loop(responses, tools=tools)

    loop.run("generá una imagen", max_steps=10)  # sin max_tool_repeats explícito

    assert len(calls) == 1


def test_ollama_error_stops_loop_cleanly():
    class FailingClient:
        def chat(self, messages, model=None, tools=None, temperature=None):
            raise OllamaError("no se pudo conectar")

    loop = AgentLoop(llm_client=FailingClient(), task_executor=FakeTaskExecutor(), memory=FakeMemoryManager())

    result = loop.run("cualquier cosa")

    assert result.status == "llm_error"
    assert "no se pudo conectar" in result.final_answer


def test_failed_code_execution_reports_error_in_observation():
    from task_execution.task import TaskStatus

    task_executor = FakeTaskExecutor(results=[FakeTask(status=TaskStatus.FAILED, error="ValueError: boom")])
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "raise ValueError('boom')"})]),
        ChatResponse(content="El código falló."),
    ]
    loop, _ = _loop(responses, task_executor=task_executor)

    result = loop.run("ejecuta código que falla")

    assert "ERROR" in result.steps[0].observation
    assert "boom" in result.steps[0].observation


def test_custom_tools_override_defaults():
    custom_tool = AgentTool(
        name="solo_esta",
        description="la única herramienta disponible",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda: "ejecutada",
    )
    loop, fake_llm = _loop(
        [ChatResponse(content="", tool_calls=[ToolCall(name="solo_esta", arguments={})]), ChatResponse(content="listo")],
        tools=[custom_tool],
    )

    loop.run("usa la única herramienta")

    schemas_sent = fake_llm.calls[0]["tools"]
    tool_names_sent = [s["function"]["name"] for s in schemas_sent]
    assert tool_names_sent == ["solo_esta"]  # no se filtraron los tools default


# --- Restricción estructural de herramientas por cliente (client="vscode") ---
#
# BUG REAL ENCONTRADO EN USO, dos veces con reglas de prompt distintas: pedido
# "creá la página web para una panadería" desde la extensión de VS Code generó
# fotos de panadería (herramienta de imágenes) en vez de código — una regla de
# SYSTEM_PROMPT pidiéndole explícitamente que no llamara a imagen/audio/video
# para este tipo de pedido NO evitó que lo hiciera. Fix real: las herramientas
# multimedia ni siquiera se incluyen en el toolset que ve el modelo cuando
# client="vscode" (mismo criterio que max_tool_repeats: estructural, no una
# petición que el modelo puede ignorar).


def test_vscode_client_excludes_multimedia_tools_from_the_registry_build():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)  # tools=None: usa el registry real

    loop.run("hola", client="vscode")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert tool_names_sent.isdisjoint(
        {"image_generation", "audio_generation", "video_composition", "image_editing",
         "image_composition", "speech_to_text", "image_via_kernel", "audio_via_kernel",
         "voice_roundtrip_via_kernel", "image_inpaint_via_kernel"}
    )
    assert "run_code" in tool_names_sent  # las herramientas de código siguen disponibles


def test_web_client_still_gets_multimedia_tools():
    """Sin client (o client="web"), la interfaz web sigue generando
    imagen/audio/video como siempre — esta restricción es solo para la
    faceta de agente IDE."""
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "image_generation" in tool_names_sent


def test_vscode_client_rejects_a_hallucinated_multimedia_tool_call_as_unknown():
    """Defensa en profundidad: si el modelo de todos modos "alucina" un
    tool_call para una herramienta excluida (no debería, ya que ni
    siquiera está en la lista que se le mandó), se rechaza como
    herramienta desconocida — nunca se ejecuta de verdad."""
    loop, fake_llm = _loop(
        [
            ChatResponse(content="", tool_calls=[ToolCall(name="image_generation", arguments={"prompt": "x"})]),
            ChatResponse(content="listo"),
        ],
        tools=None,
    )

    result = loop.run("creá la página web para una panadería", client="vscode")

    assert "no existe" in result.steps[0].observation.lower() or "desconocid" in result.steps[0].observation.lower()


# --- propose_project_files: exclusión inversa (solo VS Code) ---
#
# El backend nunca escribe el archivo real (no conoce el workspace de
# VS Code) — ofrecerle esta herramienta al cliente web solo generaría
# una propuesta que nadie puede aplicar nunca.


def test_web_client_never_gets_the_propose_project_files_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "propose_project_files" not in tool_names_sent


def test_vscode_client_gets_the_propose_project_files_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola", client="vscode")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "propose_project_files" in tool_names_sent


def test_web_client_never_gets_the_import_resource_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "import_resource" not in tool_names_sent


def test_vscode_client_gets_the_import_resource_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola", client="vscode")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "import_resource" in tool_names_sent


def test_web_client_never_gets_the_read_workspace_file_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "read_workspace_file" not in tool_names_sent


def test_vscode_client_gets_the_read_workspace_file_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola", client="vscode")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "read_workspace_file" in tool_names_sent


# --- Capability Broker: desbloqueo dinámico de multimedia en VS Code ---
#
# Ver agent_core/capability_broker.py — pedido explícito del usuario
# (2026-07-24): una tarea de código en VS Code a veces necesita ADEMÁS
# una capacidad multimedia puntual (p.ej. generar una imagen para el
# proyecto). required_capabilities (calculado por el Conversation
# Engine) desbloquea SOLO lo que ese turno señaló como necesario.


def test_vscode_client_without_required_capabilities_still_blocks_multimedia():
    """Sin required_capabilities, el comportamiento es EXACTAMENTE el de siempre."""
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola", client="vscode")

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "image_generation" not in tool_names_sent


def test_vscode_client_with_required_capabilities_unlocks_the_matching_multimedia_tool():
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hacé la web y generame un logo", client="vscode", required_capabilities=["coding", "image-generation"])

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "image_generation" in tool_names_sent
    # No desbloquea OTRAS capacidades multimedia que no se pidieron.
    assert "audio_generation" not in tool_names_sent


def test_web_client_never_gets_vscode_only_tools_even_with_coding_capability():
    """
    Barrera de seguridad clave: required_capabilities=["coding"] mapea
    también a propose_project_files/read_workspace_file (VS-Code-only),
    pero para client="web" NUNCA deben desbloquearse — son
    estructuralmente imposibles para ese cliente (no hay workspace
    real), no una cuestión de capacidad declarada.
    """
    loop, fake_llm = _loop([ChatResponse(content="listo")], tools=None)

    loop.run("hola", required_capabilities=["coding"])

    tool_names_sent = {s["function"]["name"] for s in fake_llm.calls[0]["tools"]}
    assert "propose_project_files" not in tool_names_sent
    assert "read_workspace_file" not in tool_names_sent


def test_a_raw_json_array_of_files_is_detected_as_a_propose_project_files_call():
    """
    BUG REAL ENCONTRADO EN USO: el modelo a veces "imitaba" la llamada a
    propose_project_files como un array JSON crudo de archivos en el
    texto, sin envolverlo en {"name", "arguments"} como el resto de los
    tool calls imitados — se mostraba tal cual al usuario (con los
    saltos de línea escapados como "\\n" literal, pareciendo "todo el
    código en una sola línea") en vez de proponerse de verdad.
    """
    responses = [
        ChatResponse(
            content='Te propongo:\n```json\n[{"path": "index.html", "content": "<html>\\n</html>"}]\n```'
        ),
        ChatResponse(content="Listo, creé el archivo."),
    ]
    loop, fake_llm = _loop(responses, tools=None)

    result = loop.run("creá una página web", client="vscode")

    assert result.status == "success"
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "propose_project_files"
    assert result.steps[0].arguments == {"files": [{"path": "index.html", "content": "<html>\n</html>"}]}


def test_a_raw_json_array_with_a_literal_unescaped_newline_is_still_detected():
    """
    BUG REAL ENCONTRADO EN USO proponiendo un proyecto Android real: el
    modelo escribió código Java multilínea con saltos de línea LITERALES
    dentro del valor de "content" en vez de escaparlos como \\n —
    técnicamente JSON inválido, perdía la propuesta entera antes de este
    fix (ver json_extraction.py::extract_json_array, strict=False).
    """
    raw_content_with_real_newline = '[{"path": "a.java", "content": "linea1\nlinea2"}]'
    responses = [
        ChatResponse(content=f"Te propongo:\n```json\n{raw_content_with_real_newline}\n```"),
        ChatResponse(content="Listo."),
    ]
    loop, _ = _loop(responses, tools=None)

    result = loop.run("creá un proyecto android", client="vscode")

    assert result.status == "success"
    assert result.steps[0].tool_name == "propose_project_files"
    assert result.steps[0].arguments == {"files": [{"path": "a.java", "content": "linea1\nlinea2"}]}


def test_a_raw_json_array_is_ignored_when_propose_project_files_is_not_offered():
    """Sin client="vscode", propose_project_files ni siquiera está en el
    toolset — un array JSON en el texto no debe dispararse como tool call."""
    responses = [ChatResponse(content='[{"path": "index.html", "content": "<html></html>"}]')]
    loop, _ = _loop(responses, tools=None)

    result = loop.run("creá una página web")  # sin client="vscode"

    assert result.status == "success"
    assert result.steps == []


# --- _artifact_to_observation(): rama "project_files" ---


def test_project_files_artifact_summarizes_without_leaking_full_content():
    """
    El modelo no necesita releer el contenido completo de los archivos
    propuestos (eso infla el historial sin necesidad) — la vista previa
    real la ve el USUARIO del lado de la extensión de VS Code.
    """

    class FakeProposeTool:
        class manifest:
            description = "propone archivos"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(
                modality="project_files",
                uri="",
                metadata={"status": "proposed", "request_id": "abc", "files": [{"path": "index.html", "content": "<html>mucho contenido</html>"}]},
            )

    agent_tool = _agent_tool_from_tool("propose_project_files", FakeProposeTool())

    observation = agent_tool.handler()

    assert "index.html" in observation
    assert "mucho contenido" not in observation
    assert "1 archivo" in observation


def test_text_artifact_with_summary_key_is_shown_verbatim_as_observation():
    """
    BUG REAL ENCONTRADO EN USO (analyze_image, 2026-07-19): un Artifact
    modality="text" con "status": "success" cae en la rama pensada para
    run_code (espera "stdout") y devuelve "(sin salida)" — el modelo
    nunca ve la respuesta real de una herramienta de solo-texto como
    analyze_image o speech_to_text. La convención correcta es "summary"
    (sin "status" en absoluto), que se muestra tal cual.
    """

    class FakeTextTool:
        class manifest:
            description = "analiza algo"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(modality="text", uri="", metadata={"summary": "Es una foto de un colibrí."})

    agent_tool = _agent_tool_from_tool("analyze_image", FakeTextTool())

    observation = agent_tool.handler()

    assert observation == "Es una foto de un colibrí."


def test_project_files_artifact_requiring_approval_is_a_clear_error():
    class FakeProposeTool:
        class manifest:
            description = "propone archivos"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(modality="project_files", uri="", metadata={"status": "requires_approval", "files": []})

    agent_tool = _agent_tool_from_tool("propose_project_files", FakeProposeTool())

    observation = agent_tool.handler()

    assert "ERROR" in observation
    assert "aprobación" in observation


def test_workspace_file_request_artifact_tells_the_model_to_wait_not_invent_content():
    class FakeReadTool:
        class manifest:
            description = "lee un archivo del workspace"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(
                modality="workspace_file_request",
                uri="",
                metadata={"status": "pending", "request_id": "req-1", "path": "restaurante-web/menu.html"},
            )

    agent_tool = _agent_tool_from_tool("read_workspace_file", FakeReadTool())

    observation = agent_tool.handler()

    assert "restaurante-web/menu.html" in observation
    assert "no inventes" in observation.lower()


def test_android_build_request_artifact_tells_the_model_it_is_still_in_progress():
    """
    AndroidBuildScreenshotTool (tool_integration/adapters/vscode_android.py):
    a diferencia de workspace_file_request, esto no se encadena de vuelta
    al modelo — el resultado real (captura o error de Gradle) se lo
    muestra la extensión directamente al usuario. La observación debe
    dejar claro que sigue en curso, para que la respuesta final del
    modelo no dé la tarea por terminada ni invente el resultado.
    """
    class FakeAndroidBuildTool:
        class manifest:
            description = "compila e instala una app Android"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(modality="android_build_request", uri="", metadata={"request_id": "req-1"})

    agent_tool = _agent_tool_from_tool("android_build_and_screenshot", FakeAndroidBuildTool())

    observation = agent_tool.handler()

    assert "sigue en curso" in observation
    assert "no inventes" in observation.lower()


# --- Fallback: modelos que no completan tool_calls nativo (bug real de producción) ---


def test_plain_json_in_content_is_detected_and_dispatched():
    """
    Bug real encontrado con qwen2.5-coder:14b vía Ollama: en vez de
    poblar message.tool_calls, el modelo imita el formato como texto
    plano en content. Sin el fallback, esto se mostraba tal cual al
    usuario sin ejecutar nada.
    """
    task_executor = FakeTaskExecutor()
    responses = [
        ChatResponse(content='{"name": "run_code", "arguments": {"code": "print(15 * 23)"}}'),
        ChatResponse(content="El resultado es 345."),
    ]
    loop, _ = _loop(responses, task_executor=task_executor)

    result = loop.run("calcula 15*23")

    assert result.status == "success"
    assert result.final_answer == "El resultado es 345."
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "run_code"
    assert task_executor.run_calls == ["print(15 * 23)"]


def test_json_wrapped_in_markdown_fence_is_detected():
    responses = [
        ChatResponse(content='Voy a usar esto:\n```json\n{"name": "run_code", "arguments": {"code": "print(1)"}}\n```'),
        ChatResponse(content="Listo."),
    ]
    task_executor = FakeTaskExecutor()
    loop, _ = _loop(responses, task_executor=task_executor)

    result = loop.run("prueba con fence")

    assert len(result.steps) == 1
    assert task_executor.run_calls == ["print(1)"]


def test_json_with_unknown_tool_name_is_not_treated_as_tool_call():
    """
    Un JSON en el texto que no coincide con ninguna herramienta real no
    debe dispararse como tool call — se trata como respuesta final
    normal, para no generar falsos positivos.
    """
    responses = [ChatResponse(content='{"name": "herramienta_que_no_existe", "arguments": {}}')]
    loop, _ = _loop(responses)

    result = loop.run("algo")

    assert result.status == "success"
    assert result.steps == []
    assert "herramienta_que_no_existe" in result.final_answer


def test_plain_text_final_answer_without_any_json_still_works():
    """Confirma que el fallback no interfiere con respuestas normales sin JSON."""
    responses = [ChatResponse(content="No necesito ninguna herramienta para responder esto.")]
    loop, _ = _loop(responses)

    result = loop.run("pregunta simple")

    assert result.status == "success"
    assert result.final_answer == "No necesito ninguna herramienta para responder esto."
    assert result.steps == []


def test_native_tool_calls_still_take_priority_over_fallback_parsing():
    """
    Si el modelo SÍ completa tool_calls nativo, no debería ni evaluarse
    el contenido como fallback — evita procesar dos veces si content
    coincidentemente también pareciera JSON.
    """
    task_executor = FakeTaskExecutor()
    responses = [
        ChatResponse(content="", tool_calls=[ToolCall(name="run_code", arguments={"code": "print('nativo')"})]),
        ChatResponse(content="Usé el camino nativo."),
    ]
    loop, _ = _loop(responses, task_executor=task_executor)

    loop.run("algo")

    assert task_executor.run_calls == ["print('nativo')"]


# --- Continuidad conversacional: history y session_context (agent_core/sessions.py) ---


def test_without_history_or_session_context_behavior_is_unchanged():
    loop, fake_llm = _loop([ChatResponse(content="respuesta")])

    loop.run("hola")

    sent_messages = fake_llm.calls[0]["messages"]
    assert len(sent_messages) == 2  # solo system + user, como antes de esta feature


def test_history_messages_are_included_before_the_new_goal():
    loop, fake_llm = _loop([ChatResponse(content="respuesta")])
    history = [
        {"role": "user", "content": "primer mensaje"},
        {"role": "assistant", "content": "primera respuesta"},
    ]

    loop.run("segundo mensaje", history=history)

    sent_messages = fake_llm.calls[0]["messages"]
    assert sent_messages[1] == history[0]
    assert sent_messages[2] == history[1]
    assert sent_messages[3] == {"role": "user", "content": "segundo mensaje"}


def test_session_context_is_merged_into_the_single_system_message():
    """
    Bug real encontrado en uso: mandarlo como un SEGUNDO mensaje
    role="system" separado hacía que qwen3-coder:30b lo ignorara por
    completo (confirmado con Ollama real) — fundirlo en el ÚNICO
    mensaje system es lo que el modelo sí respeta.
    """
    loop, fake_llm = _loop([ChatResponse(content="respuesta")])
    context = {"role": "system", "content": "Contexto de esta sesión: el último artefacto..."}

    loop.run("hazle el fondo azul", session_context=context)

    sent_messages = fake_llm.calls[0]["messages"]
    assert len(sent_messages) == 2  # UN solo system + el user, no tres mensajes
    assert sent_messages[0]["role"] == "system"
    assert context["content"] in sent_messages[0]["content"]
    assert sent_messages[1] == {"role": "user", "content": "hazle el fondo azul"}


def test_session_context_comes_before_history_which_comes_before_the_goal():
    loop, fake_llm = _loop([ChatResponse(content="respuesta")])
    context = {"role": "system", "content": "contexto"}
    history = [{"role": "user", "content": "turno anterior"}, {"role": "assistant", "content": "respuesta anterior"}]

    loop.run("turno nuevo", history=history, session_context=context)

    sent_messages = fake_llm.calls[0]["messages"]
    assert len(sent_messages) == 4  # system (con contexto fundido) + 2 de historial + goal
    assert sent_messages[0]["role"] == "system"
    assert "contexto" in sent_messages[0]["content"]
    assert sent_messages[1]["content"] == "turno anterior"
    assert sent_messages[2]["content"] == "respuesta anterior"
    assert sent_messages[3]["content"] == "turno nuevo"


# --- Artefacto activo: AgentStep.artifact / AgentTool.last_artifact ---


def test_agent_tool_from_tool_captures_the_raw_artifact_on_last_artifact():
    """
    _agent_tool_from_tool() es el único lugar donde una Tool real (no un
    AgentTool de test) se envuelve para el loop — confirma que además
    de aplanar el Artifact a texto (como ya hacía), guarda el Artifact
    crudo en last_artifact para que run() pueda trackear "qué generó
    este paso" sin cambiar la firma de handler.
    """

    class FakeImageTool:
        class manifest:
            description = "genera una imagen"
            parameters_schema = {"type": "object", "properties": {}}
            permissions = frozenset()

        def execute(self, **kwargs):
            return Artifact(modality="image", uri="logo.png")

    agent_tool = _agent_tool_from_tool("generate_image", FakeImageTool())

    assert agent_tool.last_artifact is None  # todavía no se llamó
    observation = agent_tool.handler()

    assert agent_tool.last_artifact == Artifact(modality="image", uri="logo.png")
    assert observation == "image: archivo generado en logo.png"


def test_agent_step_captures_the_artifact_produced_by_its_tool():
    artifact = Artifact(modality="image", uri="logo.png")
    tool = AgentTool(name="generate_image", description="", parameters_schema={"type": "object", "properties": {}}, handler=None)

    def handler(**kwargs):
        tool.last_artifact = artifact
        return "image: archivo generado en logo.png"

    tool.handler = handler
    loop, _ = _loop(
        [
            ChatResponse(content="", tool_calls=[ToolCall(name="generate_image", arguments={})]),
            ChatResponse(content="Listo, generé la imagen."),
        ],
        tools=[tool],
    )

    result = loop.run("hazme un logo")

    assert result.steps[0].artifact == artifact


def test_agent_step_artifact_is_none_for_tools_that_never_set_it():
    """Los AgentTool de test de siempre (handler que devuelve un string
    plano, sin tocar last_artifact) no deben romper nada — el step
    simplemente queda con artifact=None, como antes de esta feature."""
    loop, _ = _loop(
        [
            ChatResponse(content="", tool_calls=[ToolCall(name="solo_esta", arguments={})]),
            ChatResponse(content="listo"),
        ],
        tools=[AgentTool(name="solo_esta", description="d", parameters_schema={"type": "object", "properties": {}}, handler=lambda: "ejecutada")],
    )

    result = loop.run("algo")

    assert result.steps[0].artifact is None


def test_system_prompt_tells_the_model_to_check_its_tools_before_denying_a_capability():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-27, interfaz web): pedido "lee y
    entregame el audio de todo tu ultimo comentario con voz femenina"
    (texto ya presente en el HISTORIAL, como respuesta propia anterior)
    respondió "no tengo la capacidad de leer textos o generar audio" —
    FALSO, audio_generation estaba disponible y funciona para
    exactamente ese caso. El fix equivalente ya existía para
    client="vscode" (ver
    test_context_service.py::test_vscode_client_instruction_tells_the_model_to_ask_for_clarification_instead_of_inventing_a_limitation)
    pero nunca se había agregado al SYSTEM_PROMPT general que usa
    también la interfaz web — este test confirma que ahora sí.
    """
    assert "FÍJATE primero en tu lista real de herramientas disponibles" in SYSTEM_PROMPT
    assert "en vez de inventar una incapacidad" in SYSTEM_PROMPT
    assert "audio_generation" in SYSTEM_PROMPT


def test_run_passes_the_configured_temperature_to_the_llm():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-27): run() nunca pasaba
    temperature — heredaba el default alto de Ollama (pensado para charla
    creativa, no para decidir de forma confiable si hay que llamar una
    herramienta). Mismo prompt exacto, probado dos veces seguidas contra
    el modelo real: una vez negó falsamente tener audio_generation
    disponible (sin ningún intento de tool call), la otra lo llamó
    correctamente — variabilidad de muestreo real causada por la falta
    de un temperature bajo. Corregido con settings.llm.temperature.
    """
    from utils.config import settings

    loop, fake_llm = _loop([ChatResponse(content="listo")])

    loop.run("algo")

    assert fake_llm.calls[0]["temperature"] == settings.llm.temperature


def test_system_prompt_triggers_the_exact_quantity_self_check_without_requiring_the_word_solo():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-30): pedido simple "crea una
    orca" (sin la palabra "solo") generó DOS orcas completas — como el
    autochequeo antes solo se disparaba con "solo una/un X", esa
    duplicación nunca se detectó, y se le entregó al usuario una imagen
    con el doble de lo pedido sin ningún aviso.
    """
    assert 'NO hace falta que diga "SOLO una/un X"' in SYSTEM_PROMPT
    assert "pedir \"una orca\" ya" in SYSTEM_PROMPT


def test_system_prompt_tells_the_model_to_locate_an_existing_defect_with_vision_before_inpainting():
    """
    BUG REAL ENCONTRADO EN USO (2026-07-30): "hay dos orcas en la
    imagen, borra una de ellas" fue directo a inpaint con un 'box'
    adivinado sin haber llamado nunca a analyze_image — el resultado
    terminó siendo una composición completamente distinta a la
    original, sin ninguna orca borrada de verdad.
    """
    assert "llama primero a analyze_image" in SYSTEM_PROMPT
    assert "imagen YA EXISTENTE" in SYSTEM_PROMPT

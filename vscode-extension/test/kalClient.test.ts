import assert from "node:assert/strict";
import { test } from "node:test";
import { KalClient } from "../src/kalClient";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("chat() manda goal y model, y devuelve el resultado parseado", async () => {
  const calls: { url: string; init: any }[] = [];
  const fakeFetch = (async (url: any, init?: any) => {
    calls.push({ url: String(url), init });
    return jsonResponse({
      session_id: "sesion-1",
      goal: "hola",
      final_answer: "respuesta",
      status: "success",
      plan: ["hola"],
      steps: [],
    });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  const result = await client.chat("hola", "qwen2.5-coder:14b");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://localhost:8000/chat");
  const sentBody = JSON.parse(calls[0].init.body);
  assert.equal(sentBody.goal, "hola");
  assert.equal(sentBody.model, "qwen2.5-coder:14b");
  assert.equal(result.final_answer, "respuesta");
  assert.equal(result.session_id, "sesion-1");
});

test("chat() manda model=null, session_id=null y editor_context=null cuando no se especifican", async () => {
  const fakeFetch = (async (_url: any, init: any) => {
    const body = JSON.parse(init.body);
    assert.equal(body.model, null);
    assert.equal(body.session_id, null);
    assert.equal(body.editor_context, null);
    return jsonResponse({ session_id: "s", goal: "x", final_answer: "y", status: "success", plan: [], steps: [] });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  await client.chat("x");
});

test("chat() siempre manda client: \"vscode\" — la interfaz web sigue generando imagen/audio/video, esta faceta no", async () => {
  const fakeFetch = (async (_url: any, init: any) => {
    const body = JSON.parse(init.body);
    assert.equal(body.client, "vscode");
    return jsonResponse({ session_id: "s", goal: "x", final_answer: "y", status: "success", plan: [], steps: [] });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  await client.chat("x");
});

test("chat() manda editor_context como señal cruda, nunca texto ya formateado", async () => {
  const fakeFetch = (async (_url: any, init: any) => {
    const body = JSON.parse(init.body);
    assert.deepEqual(body.editor_context, {
      relative_path: "src/foo.py",
      language_id: "python",
      text: "def foo():\n    pass\n",
      is_selection: true,
      workspace_tree: [],
      open_editors: [],
    });
    return jsonResponse({ session_id: "s", goal: "x", final_answer: "y", status: "success", plan: [], steps: [] });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  await client.chat("x", undefined, undefined, {
    relativePath: "src/foo.py",
    languageId: "python",
    text: "def foo():\n    pass\n",
    isSelection: true,
  });
});

test("chat() manda workspace_tree y open_editors cuando el snapshot los trae (Visible Tree/Open Editors)", async () => {
  const fakeFetch = (async (_url: any, init: any) => {
    const body = JSON.parse(init.body);
    assert.deepEqual(body.editor_context.workspace_tree, ["restaurante-web/index.html", "restaurante-web/menu.html"]);
    assert.deepEqual(body.editor_context.open_editors, ["restaurante-web/menu.html"]);
    return jsonResponse({ session_id: "s", goal: "x", final_answer: "y", status: "success", plan: [], steps: [] });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  await client.chat("x", undefined, undefined, {
    relativePath: "restaurante-web/menu.html",
    languageId: "html",
    text: "",
    isSelection: false,
    workspaceTree: ["restaurante-web/index.html", "restaurante-web/menu.html"],
    openEditors: ["restaurante-web/menu.html"],
  });
});

test("chat() manda el session_id cuando se especifica, para continuar la misma conversación", async () => {
  const fakeFetch = (async (_url: any, init: any) => {
    const body = JSON.parse(init.body);
    assert.equal(body.session_id, "sesion-existente");
    return jsonResponse({
      session_id: "sesion-existente", goal: "x", final_answer: "y", status: "success", plan: [], steps: [],
    });
  }) as typeof fetch;

  const client = new KalClient("http://localhost:8000", fakeFetch);
  await client.chat("x", undefined, "sesion-existente");
});

test("chat() lanza un error claro si la respuesta no es ok", async () => {
  const fakeFetch = (async () => jsonResponse({ detail: "boom" }, 503)) as typeof fetch;
  const client = new KalClient("http://localhost:8000", fakeFetch);

  await assert.rejects(() => client.chat("hola"), /Kal-in respondió 503/);
});

test("chat() lanza un error claro si el servidor no responde", async () => {
  const fakeFetch = (async () => {
    throw new Error("ECONNREFUSED");
  }) as unknown as typeof fetch;
  const client = new KalClient("http://localhost:8000", fakeFetch);

  await assert.rejects(() => client.chat("hola"), /No se pudo conectar con kal/);
});

test("health() devuelve true cuando el servidor responde ok", async () => {
  const fakeFetch = (async () => new Response(null, { status: 200 })) as typeof fetch;
  const client = new KalClient("http://localhost:8000", fakeFetch);

  assert.equal(await client.health(), true);
});

test("health() devuelve false cuando el servidor no responde", async () => {
  const fakeFetch = (async () => {
    throw new Error("ECONNREFUSED");
  }) as unknown as typeof fetch;
  const client = new KalClient("http://localhost:8000", fakeFetch);

  assert.equal(await client.health(), false);
});

test("health() devuelve false cuando el servidor responde con error", async () => {
  const fakeFetch = (async () => new Response(null, { status: 500 })) as typeof fetch;
  const client = new KalClient("http://localhost:8000", fakeFetch);

  assert.equal(await client.health(), false);
});

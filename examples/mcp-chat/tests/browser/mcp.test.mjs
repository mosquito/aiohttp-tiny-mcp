import assert from "node:assert/strict";
import test from "node:test";
import { MCPClient, HttpError, RpcError, ToolError } from "../../mcp_chat/static/mcp.js";

const origin = "https://chat.example";

test("fetch uses the browser global as its receiver", async () => {
  const client = new MCPClient("/mcp?org=example.com", {
    origin,
    fetchImpl: function () {
      assert.equal(this, globalThis);
      return Promise.resolve(new Response(null, { status: 202 }));
    },
  });
  await client.send("notifications/initialized", {}, true);
});

function json(frame, headers = {}) {
  return new Response(JSON.stringify(frame), {
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function harness(respond) {
  const requests = [];
  let handshakes = 0;
  const client = new MCPClient("/mcp?org=example.com&user=alice", {
    origin,
    fetchImpl: async (url, options) => {
      const body = JSON.parse(options.body);
      requests.push({ url: String(url), ...options, body });
      if (body.method === "initialize") {
        handshakes += 1;
        return json({ jsonrpc: "2.0", id: body.id, result: { protocolVersion: "2025-11-25" } },
          { "Mcp-Session-Id": `session-${handshakes}` });
      }
      if (body.method === "notifications/initialized") return new Response(null, { status: 202 });
      return respond(body, options, requests);
    },
  });
  return { client, requests };
}

test("concurrent calls share initialization and preserve context and session", async () => {
  const { client, requests } = harness((body) => json({
    id: body.id, result: { structuredContent: { name: body.params.name } },
  }));
  const results = await Promise.all([client.call("identity"), client.call("inbox", { limit: 10 })]);
  assert.deepEqual(results, [{ name: "identity" }, { name: "inbox" }]);
  assert.equal(requests.filter((r) => r.body.method === "initialize").length, 1);
  for (const request of requests) {
    assert.equal(request.url, `${origin}/mcp?org=example.com&user=alice`);
    assert.equal(request.headers["MCP-Protocol-Version"], "2025-11-25");
    assert.equal(request.redirect, "error");
  }
  assert.equal(requests[1].body.id, undefined);
  assert.equal(requests.at(-1).headers["Mcp-Session-Id"], "session-1");
  assert.deepEqual(requests.at(-1).body.params.arguments, { limit: 10 });
});

test("SSE handles split UTF-8, CRLF, multiline data, and notifications", async () => {
  let cancelled = false;
  const { client } = harness((body) => {
    const text = ': heartbeat\r\n\r\ndata: {"method":"notifications/message"}\r\n\r\n' +
      `data: {"id":${body.id},\r\ndata: "result":{"structuredContent":{"text":"Привет 👋"}}}\r\n\r\n`;
    const bytes = new TextEncoder().encode(text);
    let offset = 0;
    return new Response(new ReadableStream({
      pull(controller) {
        if (offset < bytes.length) controller.enqueue(bytes.slice(offset, ++offset));
      },
      cancel() { cancelled = true; },
    }), { headers: { "Content-Type": "text/event-stream" } });
  });
  assert.deepEqual(await client.call("history", { channel: "general" }), { text: "Привет 👋" });
  assert.equal(cancelled, true);
});

test("an expired session is initialized again before one retry", async () => {
  let calls = 0;
  const { client, requests } = harness((body) => {
    if (++calls === 1) return new Response("Session not found", { status: 404 });
    return json({ id: body.id, result: { structuredContent: { messages: [] } } });
  });
  assert.deepEqual(await client.call("inbox"), { messages: [] });
  assert.equal(calls, 2);
  assert.equal(requests.at(-1).headers["Mcp-Session-Id"], "session-2");
  assert.equal(requests.filter((r) => r.body.method === "initialize").length, 2);
});

test("network and HTTP failures never blindly repeat a write", async () => {
  for (const failure of [new TypeError("Network failure"), new Response("Unavailable", { status: 503 })]) {
    let calls = 0;
    const { client } = harness(() => {
      calls += 1;
      if (failure instanceof Error) throw failure;
      return failure;
    });
    await assert.rejects(client.call("post_message", { channel: "general", text: "Once" }));
    assert.equal(calls, 1);
  }
});

test("tool and JSON-RPC errors preserve the server message", async () => {
  const tool = harness((body) => json({ id: body.id, result: {
    isError: true, content: [{ type: "text", text: "Subscribe to the channel first." }],
  } }));
  await assert.rejects(tool.client.call("post_message"), {
    constructor: ToolError, message: "Subscribe to the channel first.",
  });
  const rpc = harness((body) => json({ id: body.id, error: { code: -32602, message: "Invalid params" } }));
  await assert.rejects(rpc.client.call("inbox"), { constructor: RpcError, code: -32602 });
  const http = harness(() => new Response("Access denied", { status: 403 }));
  await assert.rejects(http.client.call("inbox"), { constructor: HttpError, status: 403 });
});

test("text-only tools and resource reads return their data", async () => {
  const { client } = harness((body) => json({ id: body.id, result:
    body.method === "resources/read"
      ? { contents: [{ uri: body.params.uri, mimeType: "text/markdown", text: "# Chat rules" }] }
      : { content: [{ type: "text", text: '{"messages":[]}' }] },
  }));
  assert.deepEqual(await client.call("inbox"), { messages: [] });
  assert.equal((await client.readResource("chat://rules")).contents[0].text, "# Chat rules");
});

test("rejects wrong reply IDs, truncated streams, and other origins", async () => {
  const mismatch = harness(() => json({ id: -42, result: {} }));
  await assert.rejects(mismatch.client.call("inbox"), /response ID/);
  const truncated = harness(() => new Response('data: {"method":"notifications/message"}\n\n', {
    headers: { "Content-Type": "text/event-stream" },
  }));
  await assert.rejects(truncated.client.call("inbox"), /without a reply/);
  assert.throws(() => new MCPClient("https://other.example/mcp", { origin }), /origin/);
  assert.throws(() => new MCPClient("https://user:pass@chat.example/mcp", { origin }), /origin/);
});

test("closing a client aborts an in-flight request", async () => {
  let started;
  const pending = new Promise((resolve) => { started = resolve; });
  const { client } = harness((body, options) => new Promise((resolve, reject) => {
    started();
    options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
  }));
  const result = client.call("inbox");
  await pending;
  client.close();
  await assert.rejects(result, { name: "AbortError" });
});

function deferred() {
  let resolve;
  const promise = new Promise((finish) => { resolve = finish; });
  return { promise, resolve };
}

function notificationStream(signal, chunks = []) {
  let controller;
  const abort = () => controller.error(signal.reason);
  const body = new ReadableStream({
    start(source) {
      controller = source;
      for (const chunk of chunks) source.enqueue(chunk);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    },
    cancel() { signal.removeEventListener("abort", abort); },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

function watchHarness(respond, { token = "" } = {}) {
  const requests = [];
  let handshakes = 0;
  const client = new MCPClient("/mcp?org=example.com&user=alice", {
    origin, token,
    fetchImpl: async function (url, options) {
      assert.equal(this, globalThis);
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url: String(url), ...options, body });
      if (body?.method === "initialize") {
        handshakes += 1;
        return json({ id: body.id, result: { protocolVersion: "2025-11-25" } },
          { "Mcp-Session-Id": `session-${handshakes}` });
      }
      if (body?.method === "notifications/initialized") return new Response(null, { status: 202 });
      if (body?.method === "resources/subscribe") return json({ id: body.id, result: {} });
      return respond(options, body, requests);
    },
  });
  return { client, requests };
}

test("watch initializes, subscribes, and authenticates its GET stream", { timeout: 3000 }, async () => {
  const live = deferred();
  const statuses = [];
  const { client, requests } = watchHarness((options) => notificationStream(options.signal),
    { token: "signed.jwt.credential" });
  const stop = client.watch(["chat://inbox", "chat://dms"], {
    onStatus(status) { statuses.push(status); if (status === "live") live.resolve(); },
  });
  try {
    await live.promise;
    assert.deepEqual(requests.map((request) => request.body?.method || request.method), [
      "initialize", "notifications/initialized", "resources/subscribe", "resources/subscribe", "GET",
    ]);
    assert.deepEqual(requests.slice(2, 4).map((request) => request.body.params), [
      { uri: "chat://inbox" }, { uri: "chat://dms" },
    ]);
    const stream = requests.at(-1);
    assert.equal(stream.url, `${origin}/mcp?org=example.com&user=alice`);
    assert.equal(stream.headers.Accept, "text/event-stream");
    assert.equal(stream.headers["MCP-Protocol-Version"], "2025-11-25");
    assert.equal(stream.headers["Mcp-Session-Id"], "session-1");
    assert.equal(stream.headers.Authorization, "Bearer signed.jwt.credential");
    assert.equal(stream.credentials, "omit");
    assert.equal(stream.redirect, "error");
    assert.equal(stream.body, null);
    assert.deepEqual(statuses, ["connecting", "live"]);
  } finally {
    stop();
    await client.watching;
    client.close();
  }
  assert.equal(requests.at(-1).signal.aborted, true);
});

test("watch decodes split UTF-8 and multiline notifications without forwarding replies", {
  timeout: 3000,
}, async () => {
  const received = deferred();
  const frames = [];
  const text = ': heartbeat\r\n\r\ndata: {"id":99,"result":{}}\r\n\r\n' +
    'event: message\r\ndata: {"jsonrpc":"2.0","method":"notifications/resources/updated",\r\n' +
    'data: "params":{"uri":"chat://inbox","text":"Привет 👋"}}\r\n\r\n';
  const chunks = [...new TextEncoder().encode(text)].map((byte) => new Uint8Array([byte]));
  const { client } = watchHarness((options) => notificationStream(options.signal, chunks));
  const stop = client.watch(["chat://inbox"], {
    onEvent(frame) { frames.push(frame); received.resolve(); },
  });
  try {
    await received.promise;
    assert.deepEqual(frames, [{ jsonrpc: "2.0", method: "notifications/resources/updated",
      params: { uri: "chat://inbox", text: "Привет 👋" } }]);
  } finally {
    stop();
    await client.watching;
    client.close();
  }
});

test("closing a client aborts an active notification stream without reconnecting", {
  timeout: 3000,
}, async () => {
  const live = deferred();
  const statuses = [];
  const { client, requests } = watchHarness((options) => notificationStream(options.signal));
  client.watch(["chat://inbox"], {
    onStatus(status) { statuses.push(status); if (status === "live") live.resolve(); },
  });
  await live.promise;
  client.close();
  await client.watching;
  assert.equal(requests.at(-1).signal.aborted, true);
  assert.equal(requests.filter((request) => request.method === "GET").length, 1);
  assert.deepEqual(statuses, ["connecting", "live"]);
});

test("watch reinitializes and resubscribes after a missing session", { timeout: 3000 }, async (t) => {
  const schedule = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", (callback, delay, ...args) => schedule(callback, 0, ...args));
  const live = deferred();
  let streams = 0;
  const { client, requests } = watchHarness((options) => {
    if (++streams === 1) return new Response("Session not found", { status: 404 });
    return notificationStream(options.signal);
  });
  const stop = client.watch(["chat://inbox", "chat://dms"], {
    onStatus(status) { if (status === "live") live.resolve(); },
  });
  try {
    await live.promise;
    assert.equal(streams, 2);
    assert.equal(requests.filter((request) => request.body?.method === "initialize").length, 2);
    const subscriptions = requests.filter((request) => request.body?.method === "resources/subscribe");
    assert.deepEqual(subscriptions.map((request) => request.headers["Mcp-Session-Id"]), [
      "session-1", "session-1", "session-2", "session-2",
    ]);
    assert.deepEqual(subscriptions.map((request) => request.body.params.uri), [
      "chat://inbox", "chat://dms", "chat://inbox", "chat://dms",
    ]);
    assert.equal(requests.at(-1).headers["Mcp-Session-Id"], "session-2");
  } finally {
    stop();
    await client.watching;
    client.close();
  }
});

test("watch caps reconnect delays and leaves failed writes unrepeated", { timeout: 3000 }, async (t) => {
  const schedule = globalThis.setTimeout;
  const delays = [];
  t.mock.method(globalThis, "setTimeout", (callback, delay, ...args) => {
    delays.push(delay);
    return schedule(callback, 0, ...args);
  });
  const live = deferred();
  const statuses = [];
  let streams = 0;
  let writes = 0;
  const { client } = watchHarness((options, body) => {
    if (options.method === "GET") {
      if (++streams <= 8) return new Response("Unavailable", { status: 503 });
      return notificationStream(options.signal);
    }
    assert.equal(body.method, "tools/call");
    writes += 1;
    return new Response("Unavailable", { status: 503 });
  });
  const stop = client.watch(["chat://inbox"], {
    onStatus(status) { statuses.push(status); if (status === "live") live.resolve(); },
  });
  try {
    await assert.rejects(client.call("post_message", { channel: "general", text: "Only once" }),
      { constructor: HttpError, status: 503 });
    await live.promise;
    assert.equal(writes, 1);
    assert.equal(streams, 9);
    assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 15000, 15000, 15000]);
    assert.equal(statuses.filter((status) => status === "retrying").length, 8);
  } finally {
    stop();
    await client.watching;
    client.close();
  }
});

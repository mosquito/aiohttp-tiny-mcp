export class HttpError extends Error {
  constructor(response, detail = "") {
    super(detail || `HTTP ${response.status}: ${response.statusText}`);
    this.status = response.status;
  }
}

export class RpcError extends Error {
  constructor(error) {
    super(error.message);
    this.code = error.code;
  }
}

export class ToolError extends Error {}

export async function* readSSE(response) {
  if (!response.body) throw new Error("MCP returned an empty event stream.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data = [];
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let cut;
      while ((cut = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, cut).replace(/\r$/, "");
        buffer = buffer.slice(cut + 1);
        if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        if (line === "" && data.length) {
          const text = data.join("\n");
          data = [];
          if (text.trim()) yield JSON.parse(text);
        }
      }
      if (done) return;
    }
  } finally {
    await reader.cancel();
  }
}

export class MCPClient {
  constructor(endpoint, { fetchImpl = globalThis.fetch, origin = globalThis.location?.origin, token = "" } = {}) {
    this.url = new URL(endpoint, origin);
    if (this.url.origin !== origin || this.url.username || this.url.password) {
      throw new Error("The MCP endpoint must use this origin.");
    }
    if (typeof token !== "string" || /[\x00-\x20\x7f]/.test(token)) {
      throw new Error("Token must not contain whitespace or control characters.");
    }
    this.token = token;
    this.fetch = fetchImpl.bind(globalThis);
    this.session = null;
    this.counter = 0;
    this.ready = null;
    this.version = "2025-11-25";
    this.abort = new AbortController();
  }

  close() {
    this.abort.abort();
    this.session = null;
    this.ready = null;
  }

  watch(resources, { onEvent = () => {}, onStatus = () => {} } = {}) {
    this.watchAbort?.abort();
    const controller = new AbortController();
    this.watchAbort = controller;
    const stop = () => controller.abort();
    this.abort.signal.addEventListener("abort", stop, { once: true });
    if (this.abort.signal.aborted) stop();
    const pause = (milliseconds) => new Promise((resolve) => {
      if (controller.signal.aborted) return resolve();
      const finish = () => {
        clearTimeout(handle);
        controller.signal.removeEventListener("abort", finish);
        resolve();
      };
      const handle = setTimeout(finish, milliseconds);
      controller.signal.addEventListener("abort", finish, { once: true });
    });
    this.watching = (async () => {
      let delay = 500;
      while (!controller.signal.aborted) {
        try {
          onStatus("connecting");
          await this.initialize();
          for (const uri of resources) {
            if (controller.signal.aborted) return;
            await this.request("resources/subscribe", { uri });
          }
          if (controller.signal.aborted) return;
          const headers = { Accept: "text/event-stream", "MCP-Protocol-Version": this.version };
          const previous = this.session;
          if (previous) headers["Mcp-Session-Id"] = previous;
          if (this.token) headers.Authorization = `Bearer ${this.token}`;
          const response = await this.fetch(this.url, {
            method: "GET", headers, credentials: "omit", redirect: "error", signal: controller.signal,
          });
          if (!response.ok) {
            await response.body?.cancel();
            if (response.status === 404 && this.session === previous) {
              this.session = null;
              this.ready = null;
            }
            throw new HttpError(response);
          }
          if (!(response.headers.get("Content-Type") || "").includes("text/event-stream")) {
            await response.body?.cancel();
            throw new Error("MCP notifications require an event stream.");
          }
          if (controller.signal.aborted) {
            await response.body?.cancel();
            return;
          }
          onStatus("live");
          delay = 500;
          for await (const frame of readSSE(response)) {
            if (controller.signal.aborted) return;
            if (frame.method) onEvent(frame);
          }
          throw new Error("MCP notification stream ended.");
        } catch (error) {
          if (controller.signal.aborted) return;
          onStatus("retrying", error);
          await pause(delay);
          delay = Math.min(delay * 2, 15000);
        }
      }
    })().finally(() => this.abort.signal.removeEventListener("abort", stop));
    return stop;
  }

  async send(method, params = {}, notification = false) {
    const id = notification ? undefined : ++this.counter;
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      "MCP-Protocol-Version": this.version,
    };
    if (this.session) headers["Mcp-Session-Id"] = this.session;
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const response = await this.fetch(this.url, {
      method: "POST",
      headers,
      credentials: "omit",
      redirect: "error",
      signal: this.abort.signal,
      body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new HttpError(response, (this.token ? detail.replaceAll(this.token, "[redacted]") : detail).slice(0, 500));
    }
    const session = response.headers.get("Mcp-Session-Id");
    if (session) this.session = session;
    if (notification) {
      await response.body?.cancel();
      return;
    }
    const matches = (frame) => frame.id === id || (frame.id === null && frame.error);
    const unwrap = (frame) => {
      if (frame.error) throw new RpcError(frame.error);
      if (!("result" in frame)) throw new Error("MCP returned no result.");
      return frame.result;
    };
    if (!(response.headers.get("Content-Type") || "").includes("text/event-stream")) {
      const frame = await response.json();
      if (!matches(frame)) throw new Error("MCP returned an unexpected response ID.");
      return unwrap(frame);
    }
    for await (const frame of readSSE(response)) if (matches(frame)) return unwrap(frame);
    throw new Error("MCP stream ended without a reply.");
  }

  async initialize() {
    if (!this.ready) {
      this.ready = (async () => {
        const result = await this.send("initialize", {
          protocolVersion: this.version,
          capabilities: {},
          clientInfo: { name: "mcp-chat-web", version: "0.1.0" },
        });
        if (result.protocolVersion !== "2025-11-25") {
          throw new Error("The server did not accept MCP 2025-11-25.");
        }
        await this.send("notifications/initialized", {}, true);
        return result;
      })().catch((error) => {
        this.ready = null;
        throw error;
      });
    }
    return this.ready;
  }

  async request(method, params = {}) {
    await this.initialize();
    const previous = this.session;
    try {
      return await this.send(method, params);
    } catch (error) {
      if (!(error instanceof HttpError) || error.status !== 404 || !previous) throw error;
      if (this.session === previous) {
        this.session = null;
        this.ready = null;
      }
      await this.initialize();
      return await this.send(method, params);
    }
  }

  async call(name, args = {}) {
    const result = await this.request("tools/call", { name, arguments: args });
    const text = (result.content || [])
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n");
    if (result.isError) throw new ToolError(text || "The server refused this operation.");
    if (result.structuredContent !== undefined) return result.structuredContent;
    if (text) return JSON.parse(text);
    throw new Error("The tool returned no JSON data.");
  }

  async readResource(uri) {
    return await this.request("resources/read", { uri });
  }
}

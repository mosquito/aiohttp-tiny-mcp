/* Browser MCP client; revision differences are defined in REVISIONS. */

"use strict";


const REVISIONS = {
  "2026-07-28": {
    describe: "server/discover",
    handshake: false,       // no initialize, and nothing to complete
    meta: true,             // identity and capabilities on every request
    versionHeader: true,
    methodHeader: true,     // Mcp-Method, and Mcp-Name where it applies
    ask: "result",          // the question comes back as the result
    extensions: true,
  },
  "2025-11-25": {
    describe: "initialize",
    handshake: true,        // stated once, and remembered
    meta: false,
    versionHeader: true,
    methodHeader: false,
    ask: "push",            // pushed on the open stream, answered separately
  },
  "2025-06-18": {
    describe: "initialize",
    handshake: true,
    meta: false,
    versionHeader: true,
    methodHeader: false,
    ask: "push",
  },
  "2025-03-26": {
    describe: "initialize",
    handshake: true,
    meta: false,
    versionHeader: false,   // its absence is what implies this revision
    methodHeader: false,
    ask: "arguments",       // the question rides in the tool call itself
  },
  "2024-11-05": {
    // The first published revision. It predates Streamable HTTP, so a real
    // client of it speaks the two-endpoint HTTP+SSE transport instead; this
    // page reaches a server that accepts the revision over the transport it
    // has, which is what makes it useful for seeing the projection.
    describe: "initialize",
    handshake: true,
    meta: false,
    versionHeader: false,
    methodHeader: false,
    ask: "arguments",
  },
};

const NAME_HEADER_METHODS = new Set(["tools/call", "resources/read", "prompts/get"]);

// Package-specific retry fields for the pre-elicitation revision.
const ASKED_META = "dev.aiohttp-tiny-mcp/inputRequired";
const ANSWERS_KEY = "mcpAnswers";
const STATE_KEY = "mcpState";

const CLIENT = { name: "aiohttp-tiny-mcp-console", version: "0.1.0" };
const SKILLS_EXTENSION = "io.modelcontextprotocol/skills";

const DRIVEN = new Set([ANSWERS_KEY, STATE_KEY]);


class Client {
  constructor(url, version, options) {
    this.url = url;
    this.version = version;
    this.rules = REVISIONS[version];
    this.answerable = options.answerable;
    this.onFrame = options.onFrame;       // every message, either way
    this.onNotify = options.onNotify;     // progress, logging, changes
    this.onQuestion = options.onQuestion; // returns an ElicitResult
    // Return the server-issued session id on subsequent requests.
    this.sessionId = null;
    this.counter = 0;
    this.tools = new Map();
    this.serverCapabilities = {};
  }

  get capabilities() {
    return this.answerable ? { elicitation: {} } : {};
  }

  nextId() {
    this.counter += 1;
    return this.counter;
  }

  decorate(params, method) {
    const shaped = { ...params };
    if (this.rules.meta) {
      shaped._meta = {
        ...(shaped._meta || {}),
        "io.modelcontextprotocol/protocolVersion": this.version,
        "io.modelcontextprotocol/clientInfo": CLIENT,
        "io.modelcontextprotocol/clientCapabilities": this.capabilities,
      };
    }
    return shaped;
  }

  headers(method, name) {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    };
    if (this.rules.versionHeader) headers["MCP-Protocol-Version"] = this.version;
    if (this.rules.methodHeader) {
      headers["Mcp-Method"] = method;
      if (name && NAME_HEADER_METHODS.has(method)) headers["Mcp-Name"] = encodeHeader(name);
    }
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;
    return headers;
  }

  /* Read frames before EOF so pushed questions can be answered while the stream stays open. */
  async *frames(envelope, method, name) {
    const headers = this.headers(method, name);
    this.onFrame("sent", envelope);
    const response = await fetch(this.url, {
      method: "POST",
      headers,
      body: JSON.stringify(envelope),
    });

    const issued = response.headers.get("Mcp-Session-Id");
    if (issued) this.sessionId = issued;

    const kind = (response.headers.get("Content-Type") || "").split(";")[0].trim();
    if (kind !== "text/event-stream") {
      const text = await response.text();
      if (!text) return;
      const body = JSON.parse(text);
      this.onFrame("received", body);
      yield body;
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 1);
        if (!line.startsWith("data:")) continue;
        const text = line.slice(5).trim();
        // A data line with no payload primes or keeps the stream alive.
        if (!text) continue;
        const body = JSON.parse(text);
        this.onFrame("received", body);
        yield body;
      }
    }
  }

  answers(frame, id) {
    if (!("result" in frame) && !("error" in frame)) return false;
    // Pre-decode errors have id: null but still answer this request.
    return frame.id === id || ("error" in frame && frame.id === null);
  }

  async request(method, params, name) {
    const envelope = {
      jsonrpc: "2.0",
      id: this.nextId(),
      method,
      params: this.decorate(params || {}, method),
    };
    let body = null;
    for await (const frame of this.frames(envelope, method, name)) {
      if (this.answers(frame, envelope.id)) {
        body = frame;
        break;
      }
      await this.incoming(frame);
    }
    if (body === null) throw new Error(`${method} ended without a reply`);
    if (body.error) {
      const failure = new Error(body.error.message);
      failure.code = body.error.code;
      failure.data = body.error.data;
      throw failure;
    }
    return body.result;
  }

  async notify(method, params) {
    const envelope = { jsonrpc: "2.0", method, params: this.decorate(params || {}, method) };
    this.onFrame("sent", envelope);
    await fetch(this.url, {
      method: "POST",
      headers: this.headers(method),
      body: JSON.stringify(envelope),
    });
  }

  async incoming(frame) {
    if (frame.id === undefined || frame.id === null) {
      this.onNotify(frame);
      return;
    }
    if (!frame.method) return;
    // Pushed questions receive a separate bare JSON-RPC response.
    const reply = this.answerable
      ? { jsonrpc: "2.0", id: frame.id, result: await this.onQuestion(frame.params || {}) }
      : {
          jsonrpc: "2.0",
          id: frame.id,
          error: { code: -32601, message: `unsupported: ${frame.method}` },
        };
    this.onFrame("sent", reply);
    await fetch(this.url, {
      method: "POST",
      headers: this.headers("elicitation/create"),
      body: JSON.stringify(reply),
    });
  }


  async initialize() {
    const params = this.rules.handshake
      ? { protocolVersion: this.version, clientInfo: CLIENT, capabilities: this.capabilities }
      : {};
    const result = await this.request(this.rules.describe, params);
    this.serverCapabilities = result.capabilities || {};
    if (this.rules.handshake) await this.notify("notifications/initialized", {});
    return result;
  }

  get extensions() {
    return this.rules.extensions ? this.serverCapabilities.extensions || {} : {};
  }

  async pages(method, key) {
    const items = [];
    const seen = new Set();
    let cursor;
    do {
      const result = await this.request(method, cursor === undefined ? {} : { cursor });
      items.push(...(result[key] || []));
      cursor = result.nextCursor;
      if (cursor !== undefined && cursor !== null) {
        if (typeof cursor !== "string" || seen.has(cursor)) {
          throw new Error(`${method} returned an invalid or repeated cursor`);
        }
        seen.add(cursor);
      }
    } while (cursor !== undefined && cursor !== null);
    return items;
  }

  async listTools() {
    const tools = await this.pages("tools/list", "tools");
    this.tools = new Map(tools.map((tool) => [tool.name, tool]));
    return tools;
  }

  async listResources() {
    const resources = await this.pages("resources/list", "resources");
    let templates = [];
    try {
      templates = await this.pages("resources/templates/list", "resourceTemplates");
    } catch (error) {
      /* A server with no templates may not expose the method. */
    }
    return { resources, templates };
  }

  async listPrompts() {
    return this.pages("prompts/list", "prompts");
  }

  async listSkills() {
    if (!Object.hasOwn(this.extensions, SKILLS_EXTENSION)) return [];
    return this.pages("skills/list", "skills");
  }

  async getSkill(uri) {
    if (!Object.hasOwn(this.extensions, SKILLS_EXTENSION)) {
      throw new Error("This server has not declared the Skills extension for this revision.");
    }
    const result = await this.request("skills/get", { uri });
    if (!result.skill || result.skill.uri !== uri) {
      throw new Error("skills/get returned a different skill URI");
    }
    return result.skill;
  }

  async readResource(uri) {
    return this.request("resources/read", { uri }, uri);
  }

  async getPrompt(name, argumentValues) {
    return this.request("prompts/get", { name, arguments: argumentValues }, name);
  }

  async callTool(name, argumentValues) {
    let answers = {};
    let state = null;
    for (let round = 0; round < 8; round += 1) {
      const params = { name, arguments: { ...argumentValues } };
      if (Object.keys(answers).length) {
        // Resend previous answers because the handler restarts on each round.
        if (this.rules.ask === "arguments") {
          params.arguments[ANSWERS_KEY] = answers;
          if (state) params.arguments[STATE_KEY] = state;
        } else {
          params.inputResponses = answers;
          if (state) params.requestState = state;
        }
      }
      const result = await this.request("tools/call", params, name);
      const asked = this.asked(result);
      if (!asked) return result;
      if (!this.answerable) return result;
      state = asked.state;
      for (const [key, request] of Object.entries(asked.requests)) {
        answers[key] = await this.onQuestion(request.params || {});
      }
    }
    throw new Error(`${name} is still asking after 8 rounds`);
  }

  asked(result) {
    if (this.rules.ask === "result" && result.resultType === "input_required") {
      return { requests: result.inputRequests || {}, state: result.requestState || null };
    }
    if (this.rules.ask === "arguments") {
      const carried = (result._meta || {})[ASKED_META];
      if (carried) {
        return { requests: carried.inputRequests || {}, state: carried.requestState || null };
      }
    }
    return null;
  }
}

function encodeHeader(value) {
  const text = String(value);
  const plain = /^[\x20-\x7E]*$/.test(text) && text === text.trim();
  if (plain) return text;
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return `=?base64?${btoa(binary)}?=`;
}


const page = {
  endpoint: document.getElementById("endpoint"),
  revision: document.getElementById("revision"),
  answerable: document.getElementById("answerable"),
  connect: document.getElementById("connect"),
  refresh: document.getElementById("refresh"),
  status: document.getElementById("status"),
  catalogue: document.getElementById("catalogue"),
  subject: document.getElementById("subject"),
  about: document.getElementById("about"),
  args: document.getElementById("arguments"),
  invoke: document.getElementById("invoke"),
  outcome: document.getElementById("outcome"),
  traffic: document.getElementById("traffic"),
  follow: document.getElementById("follow"),
  clear: document.getElementById("clear"),
  greeting: document.getElementById("greeting"),
  trafficPanel: document.getElementById("traffic-panel"),
  trafficToggle: document.getElementById("traffic-toggle"),
  cataloguePanel: document.getElementById("catalogue-panel"),
  catalogueToggle: document.getElementById("catalogue-toggle"),
  question: document.getElementById("question"),
  questionText: document.getElementById("question-text"),
  questionFields: document.getElementById("question-fields"),
  answerForm: document.getElementById("answer"),
};

let client = null;
let chosen = null;
let endpointUrl = null;
//: Where the page was told the endpoint is. The field starts from it and may add a query.
let configuredEndpoint = null;

// The endpoint field: the path, plus any query string a person adds, such as
// `?mcp=2025-06-18` to pin a revision or whatever the server reads from the URL.
// The value is kept for the next visit while its path is still the configured one,
// so a server moved elsewhere is not chased at a stale address.
function endpointKey() {
  return `mcp-console-endpoint:${location.pathname}`;
}

function rememberedEndpoint() {
  const kept = localStorage.getItem(endpointKey());
  if (!kept) return null;
  const url = new URL(kept, location.origin);
  return url.pathname === configuredEndpoint.pathname ? kept : null;
}

function chosenEndpoint() {
  const typed = page.endpoint.value.trim() || configuredEndpoint.pathname;
  const url = new URL(typed, location.origin);
  if (url.origin !== location.origin) {
    throw new Error(`The endpoint must be on this server, not ${url.origin}.`);
  }
  page.endpoint.value = url.pathname + url.search;
  localStorage.setItem(endpointKey(), page.endpoint.value);
  return url;
}

function element(tag, className, text) {
  const made = document.createElement(tag);
  if (className) made.className = className;
  if (text !== undefined) made.textContent = text;
  return made;
}

function say(text, state) {
  page.status.textContent = text;
  page.status.className = `status ${state || ""}`;
}


function record(direction, body) {
  if (page.traffic.firstElementChild && page.traffic.firstElementChild.className === "empty") {
    page.traffic.textContent = "";
  }
  const frame = element("details", `frame ${direction}`);
  const label = body.method || (body.error ? `error ${body.error.code}` : "result");
  const summary = element("summary", null, `${label}${body.id !== undefined ? ` #${body.id}` : ""}`);
  frame.append(summary, valueViews(body));
  page.traffic.append(frame);
  if (page.follow.checked) page.traffic.scrollTop = page.traffic.scrollHeight;
  markTraffic();
}

function noticed(frame) {
  const params = frame.params || {};
  if (frame.method === "notifications/progress") {
    let bar = page.outcome.querySelector("progress");
    if (!bar) {
      bar = element("progress");
      page.outcome.prepend(bar);
    }
    bar.max = params.total || 1;
    bar.value = params.progress || 0;
    bar.title = params.message || "";
    return;
  }
  if (frame.method === "notifications/message") {
    const line = element("div", "note-line");
    line.append(element("span", "level", `${params.level} `), document.createTextNode(
      typeof params.data === "string" ? params.data : JSON.stringify(params.data)
    ));
    page.outcome.append(line);
    return;
  }
  const line = element("div", "note-line", `${frame.method} ${summarize(params)}`);
  page.outcome.append(line);
}


function askPerson(request) {
  const schema = request.requestedSchema || { type: "object", properties: {} };
  prose(request.message || "The server needs an answer.", page.questionText);
  page.questionFields.textContent = "";
  const fields = buildFields(schema, page.questionFields);
  page.question.showModal();

  return new Promise((resolve) => {
    page.answerForm.onsubmit = null;
    page.question.addEventListener(
      "close",
      () => {
        const action = page.question.returnValue || "cancel";
        if (action !== "accept") {
          resolve({ action });
          return;
        }
        resolve({ action: "accept", content: readFields(fields) });
      },
      { once: true }
    );
  });
}


function buildFields(schema, into, omit) {
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  const skip = omit || new Set();
  const fields = [];
  for (const [name, property] of Object.entries(properties)) {
    if (skip.has(name)) continue;
    fields.push(buildField(name, property, required.has(name), into));
  }
  if (!fields.length) {
    into.append(element("p", "empty", "Nothing to fill in."));
  }
  return fields;
}

function buildField(name, property, isRequired, into) {
  const kinds = [].concat(property.type || inferType(property));
  const kind = kinds.find((one) => one !== "null") || "string";
  const row = element("div", kind === "boolean" ? "argument flag" : "argument");
  const label = element("label", null, property.title || name);
  if (isRequired) label.append(element("span", "required", "*"));
  let input;

  if (property.enum) {
    input = element("select");
    if (!isRequired) input.append(element("option", null, ""));
    property.enum.forEach((value) => {
      const option = element("option", null, String(value));
      option.value = String(value);
      input.append(option);
    });
  } else if (kind === "boolean") {
    input = element("input");
    input.type = "checkbox";
    if (property.default === true) input.checked = true;
  } else if (kind === "integer" || kind === "number") {
    input = element("input");
    input.type = "number";
    if (kind === "integer") input.step = "1";
    if (property.minimum !== undefined) input.min = property.minimum;
    if (property.maximum !== undefined) input.max = property.maximum;
    if (property.default !== undefined) input.value = property.default;
  } else if (kind === "object" || kind === "array") {
    input = element("textarea");
    input.placeholder = kind === "array" ? "[]" : "{}";
    if (property.default !== undefined) input.value = JSON.stringify(property.default, null, 2);
  } else {
    input = element("input");
    input.type = "text";
    if (property.default !== undefined) input.value = property.default;
  }

  input.dataset.name = name;
  input.dataset.kind = kind;
  if (isRequired) input.dataset.required = "1";
  row.append(label, input);
  if (property.description) row.append(element("div", "hint", property.description));
  into.append(row);
  return input;
}

function inferType(property) {
  const branches = property.anyOf || property.oneOf || [];
  const named = branches.map((branch) => branch.type).filter((type) => type && type !== "null");
  return named.length ? named : "string";
}

/* Empty inputs represent absent arguments, including for required-field validation. */
function missing(fields) {
  return fields.filter(
    (input) => input.dataset.required && input.dataset.kind !== "boolean" && !input.value.trim()
  );
}

function readFields(fields) {
  const values = {};
  fields.forEach((input) => {
    const name = input.dataset.name;
    const kind = input.dataset.kind;
    if (kind === "boolean") {
      values[name] = input.checked;
      return;
    }
    const raw = input.value.trim();
    if (raw === "") return; // absent, which is not the same as empty
    if (kind === "integer") values[name] = parseInt(raw, 10);
    else if (kind === "number") values[name] = parseFloat(raw);
    else if (kind === "object" || kind === "array") {
      try {
        values[name] = JSON.parse(raw);
      } catch (error) {
        throw new Error(`${name}: ${error.message}`);
      }
    } else values[name] = raw;
  });
  return values;
}


function show(items, title, into, describe, pick) {
  if (!items.length) return;
  const group = element("div", "group");
  group.append(element("h3", null, title));
  items.forEach((item) => {
    const button = element("button", "item");
    button.type = "button";
    const name = element("span", "name", describe.label(item));
    (describe.tags ? describe.tags(item) : []).forEach((tag) => {
      name.append(element("span", `tag ${tag}`, tag));
    });
    button.append(name);
    const note = describe.note(item);
    if (note) button.append(element("span", "note", note));
    button.onclick = () => {
      page.catalogue.querySelectorAll(".item").forEach((other) => other.classList.remove("chosen"));
      button.classList.add("chosen");
      pick(item);
    };
    group.append(button);
  });
  into.append(group);
}

function firstLine(text) {
  return plain((text || "").split("\n")[0]);
}

/* Escape server text before adding markdown tags; descriptions cannot supply HTML. */

const SAFE_LINK = /^(https?:|mailto:|#|\/)/i;

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Protect code spans from inline markup using placeholders absent from escaped input. */
function inline(text) {
  const spans = [];
  let marked = text.replace(/`([^`]+)`/g, (whole, code) => {
    spans.push(code);
    return `<c${spans.length - 1}>`;
  });

  marked = marked
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, href) =>
      SAFE_LINK.test(href)
        ? `<a href="${href}" target="_blank" rel="noreferrer noopener">${label}</a>`
        : label
    )
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>");

  return marked.replace(/<c(\d+)>/g, (whole, index) => `<code>${spans[index]}</code>`);
}

function listItem(line) {
  const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
  if (bullet) return { ordered: false, text: bullet[1] };
  const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
  if (numbered) return { ordered: true, text: numbered[1] };
  return null;
}

function markdownHtml(text) {
  const lines = String(text || "").split("\n");
  const out = [];
  let paragraph = [];
  let list = null;

  const endParagraph = () => {
    if (!paragraph.length) return;
    // Reflow source-wrapped paragraphs to the panel width.
    out.push(`<p>${inline(escapeHtml(paragraph.join(" ").trim()))}</p>`);
    paragraph = [];
  };
  const endList = () => {
    if (!list) return;
    const tag = list.ordered ? "ol" : "ul";
    out.push(`<${tag}>${list.items.map((item) => `<li>${item}</li>`).join("")}</${tag}>`);
    list = null;
  };
  const endBoth = () => {
    endParagraph();
    endList();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (/^\s*```/.test(line)) {
      endBoth();
      const code = [];
      for (index += 1; index < lines.length && !/^\s*```/.test(lines[index]); index += 1) {
        code.push(lines[index]);
      }
      out.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    if (!line.trim()) {
      endBoth();
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      endBoth();
      const level = Math.min(heading[1].length + 2, 6); // the panel owns h1 and h2
      out.push(`<h${level}>${inline(escapeHtml(heading[2]))}</h${level}>`);
      continue;
    }

    const item = listItem(line);
    if (item) {
      endParagraph();
      if (!list || list.ordered !== item.ordered) {
        endList();
        list = { ordered: item.ordered, items: [] };
      }
      list.items.push(inline(escapeHtml(item.text)));
      continue;
    }

    // Indentation continues a preceding list or paragraph; otherwise it starts code.
    if (/^ {4,}\S/.test(line) && !list && !paragraph.length) {
      const code = [];
      while (index < lines.length && (/^ {4,}/.test(lines[index]) || !lines[index].trim())) {
        code.push(lines[index].replace(/^ {4}/, ""));
        index += 1;
      }
      index -= 1;
      out.push(`<pre><code>${escapeHtml(code.join("\n").replace(/\s+$/, ""))}</code></pre>`);
      continue;
    }

    if (list) {
      list.items[list.items.length - 1] += ` ${inline(escapeHtml(line.trim()))}`;
      continue;
    }
    paragraph.push(line.trim());
  }

  endBoth();
  return out.join("");
}

function prose(text, into) {
  into.innerHTML = markdownHtml(text);
}

/* Strip markdown for single-line button labels. */
function plain(text) {
  return String(text || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/, "")
    .replace(/^\s*[-*+]\s+/, "");
}


function show(items, title, into, describe, pick) {
  if (!items.length) return;
  const group = element("div", "group");
  group.append(element("h3", null, title));
  items.forEach((item) => {
    const button = element("button", "item");
    button.type = "button";
    const name = element("span", "name", describe.label(item));
    (describe.tags ? describe.tags(item) : []).forEach((tag) => {
      name.append(element("span", `tag ${tag}`, tag));
    });
    button.append(name);
    const note = describe.note(item);
    if (note) button.append(element("span", "note", note));
    button.onclick = () => {
      page.catalogue.querySelectorAll(".item").forEach((other) => other.classList.remove("chosen"));
      button.classList.add("chosen");
      pick(item);
    };
    group.append(button);
  });
  into.append(group);
}

function hints(tool) {
  const annotations = tool.annotations || {};
  const tags = [];
  if (annotations.destructiveHint) tags.push("destructive");
  if (annotations.readOnlyHint) tags.push("readonly");
  return tags;
}

async function loadCatalogue() {
  page.catalogue.textContent = "";
  const into = document.createDocumentFragment();

  const tools = await client.listTools();
  show(tools, `Tools (${tools.length})`, into, {
    label: (tool) => tool.name,
    note: (tool) => firstLine(tool.description),
    tags: hints,
  }, (tool) => choose({ kind: "tool", item: tool }));

  const { resources, templates } = await client.listResources();
  show(resources, `Resources (${resources.length})`, into, {
    label: (resource) => resource.uri,
    note: (resource) => firstLine(resource.description) || resource.name,
  }, (resource) => choose({ kind: "resource", item: resource }));

  show(templates, `Resource templates (${templates.length})`, into, {
    label: (template) => template.uriTemplate,
    note: (template) => firstLine(template.description) || template.name,
  }, (template) => choose({ kind: "template", item: template }));

  const prompts = await client.listPrompts();
  show(prompts, `Prompts (${prompts.length})`, into, {
    label: (prompt) => prompt.name,
    note: (prompt) => firstLine(prompt.description),
  }, (prompt) => choose({ kind: "prompt", item: prompt }));

  const extensions = Object.entries(client.extensions).map(([name, capabilities]) => ({ name, capabilities }));
  show(extensions, `Extensions (${extensions.length})`, into, {
    label: (extension) => extension.name,
    note: () => "Capabilities and custom requests",
  }, (extension) => choose({ kind: "extension", item: extension }));

  if (Object.hasOwn(client.extensions, SKILLS_EXTENSION)) {
    try {
      const skills = await client.listSkills();
      show(skills, `Skills (${skills.length})`, into, {
        label: (skill) => (skill.frontmatter || {}).name || skill.uri,
        note: (skill) => `${skill.uri} — ${firstLine((skill.frontmatter || {}).description)}`,
      }, (skill) => choose({ kind: "skill", item: skill }));
      if (!skills.length) into.append(element("p", "empty", "No skills listed. Use skills/get with a known URI."));
    } catch (error) {
      into.append(element("p", "empty", `Could not list skills: ${error.message}`));
    }
  }

  if (!into.childNodes.length) {
    page.catalogue.append(element("p", "empty", "This server offers nothing."));
  } else {
    page.catalogue.append(into);
  }
}


function choose(what) {
  chosen = what;
  if (catalogue) catalogue.fold();
  page.outcome.textContent = "";
  page.args.textContent = "";
  page.invoke.disabled = false;

  if (what.kind === "tool") {
    page.subject.textContent = what.item.name;
    prose(what.item.description, page.about);
    // Hide retry arguments managed by this client.
    chosen.fields = buildFields(what.item.inputSchema || {}, page.args, DRIVEN);
    page.invoke.textContent = "Call";
  } else if (what.kind === "prompt") {
    page.subject.textContent = what.item.name;
    prose(what.item.description, page.about);
    const schema = {
      type: "object",
      properties: Object.fromEntries(
        (what.item.arguments || []).map((argument) => [
          argument.name,
          { type: "string", description: argument.description },
        ])
      ),
      required: (what.item.arguments || []).filter((one) => one.required).map((one) => one.name),
    };
    chosen.fields = buildFields(schema, page.args);
    page.invoke.textContent = "Get";
  } else if (what.kind === "extension") {
    page.subject.textContent = what.item.name;
    page.about.textContent = "Enter a method and its JSON parameters from the extension documentation.";
    reportValue("Capabilities", what.item.capabilities);
    if ((what.item.capabilities || {}).notifications) {
      // Method to the params field that carries its topic; null for a plain broadcast.
      reportValue("Broadcasts", what.item.capabilities.notifications);
    }
    chosen.fields = buildFields({
      properties: {
        method: { type: "string", default: what.item.name === SKILLS_EXTENSION ? "skills/list" : "" },
        params: { type: "object", default: {} },
      },
      required: ["method"],
    }, page.args);
    page.invoke.textContent = "Send";
  } else if (what.kind === "skill") {
    page.subject.textContent = (what.item.frontmatter || {}).name || what.item.uri;
    prose((what.item.frontmatter || {}).description, page.about);
    page.about.append(element("p", "hint", what.item.uri));
    chosen.fields = [];
    page.invoke.textContent = "Inspect";
    reportValue("Frontmatter", what.item.frontmatter);
  } else if (what.kind === "resource") {
    page.subject.textContent = what.item.uri;
    prose(what.item.description, page.about);
    chosen.fields = buildFields({}, page.args);
    page.invoke.textContent = "Read";
  } else {
    page.subject.textContent = what.item.uriTemplate;
    prose(what.item.description, page.about);
    const variables = [...what.item.uriTemplate.matchAll(/\{([^}]+)\}/g)].map((one) => one[1]);
    const schema = {
      type: "object",
      properties: Object.fromEntries(variables.map((name) => [name, { type: "string" }])),
      required: variables,
    };
    chosen.fields = buildFields(schema, page.args);
    page.invoke.textContent = "Read";
  }
}


const COPY_ICON =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">' +
  '<rect x="5.5" y="5.5" width="8" height="9" rx="1.5" fill="none" ' +
  'stroke="currentColor" stroke-width="1.3"/>' +
  '<path d="M10.5 3.5h-7a1.5 1.5 0 0 0-1.5 1.5v7" fill="none" ' +
  'stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';

const DONE_ICON =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">' +
  '<path d="M3 8.5l3.5 3.5L13 5" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

/* Fall back to execCommand when HTTP lacks the secure context required by Clipboard API. */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const carrier = document.createElement("textarea");
  carrier.value = text;
  carrier.setAttribute("readonly", "");
  carrier.style.position = "fixed";
  carrier.style.opacity = "0";
  document.body.append(carrier);
  carrier.select();
  document.execCommand("copy");
  carrier.remove();
}

function copyButton(text, what) {
  const button = element("button", "copy");
  button.type = "button";
  button.title = `Copy ${what}`;
  button.setAttribute("aria-label", `Copy ${what}`);
  button.innerHTML = COPY_ICON;
  button.onclick = async (event) => {
    event.stopPropagation();
    try {
      await copyText(text);
      button.innerHTML = DONE_ICON;
      button.classList.add("done");
      setTimeout(() => {
        button.innerHTML = COPY_ICON;
        button.classList.remove("done");
      }, 1200);
    } catch (error) {
      button.title = `Could not copy: ${error.message}`;
    }
  };
  return button;
}

function copyable(node, text, what) {
  const holder = element("span", "copyable");
  holder.append(node, copyButton(text, what || "this value"));
  return holder;
}

function copyTextOf(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

/* Preserve JSON types; do not infer dates or other meanings from values. */

const LONG_TEXT = 70;

function scalar(value) {
  if (value === null || value === undefined) {
    return element("span", "value absent", "null");
  }
  if (typeof value === "boolean") {
    return element("span", `value boolean ${value}`, value ? "true" : "false");
  }
  if (typeof value === "number") {
    return element("span", "value number", String(value));
  }
  const text = String(value);
  if (text.includes("\n") || text.length > LONG_TEXT) {
    return element("pre", "value text", text);
  }
  return element("span", "value string", text || "—");
}

function isScalar(value) {
  return value === null || typeof value !== "object";
}

function renderValue(value, depth) {
  const level = depth || 0;
  if (isScalar(value)) {
    return value === null || value === undefined
      ? scalar(value)
      : copyable(scalar(value), copyTextOf(value), "this value");
  }

  if (Array.isArray(value)) {
    if (!value.length) return element("span", "value absent", "empty");
    const list = element("ol", "items");
    value.forEach((item) => {
      const row = element("li");
      row.append(renderValue(item, level + 1));
      list.append(row);
    });
    return list;
  }

  const names = Object.keys(value);
  if (!names.length) return element("span", "value absent", "empty");

  const table = element("div", "fields");
  names.forEach((name) => {
    const row = element("div", "field-row");
    const held = value[name];
    const label = element("span", "field-name");
    label.append(document.createTextNode(humanName(name)));
    const shown = element("div", "field-value");
    shown.append(renderValue(held, level + 1));
    if (!isScalar(held)) {
      shown.classList.add("nested");
      label.classList.add("labels-branch");
      label.append(copyButton(copyTextOf(held), humanName(name)));
    }
    row.append(label, shown);
    table.append(row);
  });
  return table;
}

/* Humanize field names, preserving dotted or slashed protocol keys. */
function humanName(name) {
  const text = String(name);
  if (/[./]/.test(text)) return text;
  return text
    .replace(/[_-]+/g, " ")
    .replace(/([a-z\d])([A-Z])/g, "$1 $2")
    .trim()
    .replace(/^./, (first) => first.toUpperCase());
}

function parsed(text) {
  try {
    return JSON.parse(text);
  } catch (error) {
    return undefined;
  }
}

/* Compare rendered values without treating object key order as a difference. */
function same(left, right) {
  if (left === undefined || right === undefined) return false;
  return JSON.stringify(sorted(left)) === JSON.stringify(sorted(right));
}

function sorted(value) {
  if (Array.isArray(value)) return value.map(sorted);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((name) => [name, sorted(value[name])])
  );
}

function summarize(params) {
  return Object.entries(params || {})
    .filter(([name]) => name !== "_meta")
    .map(([name, value]) => `${name}=${isScalar(value) ? value : JSON.stringify(value)}`)
    .join(" ");
}

function valueViews(value) {
  const holder = element("div", "views");
  const bar = element("div", "views-bar");
  bar.append(copyButton(copyTextOf(value), "the whole message"));
  const toggle = element("button", "quiet tiny", "raw");
  toggle.type = "button";
  bar.append(toggle);

  const tree = element("div", "value-tree");
  tree.append(renderValue(value, 0));
  const raw = element("pre", "value-raw", JSON.stringify(value, null, 2));
  raw.hidden = true;

  toggle.onclick = (event) => {
    event.preventDefault();
    const showRaw = raw.hidden;
    raw.hidden = !showRaw;
    tree.hidden = showRaw;
    toggle.textContent = showRaw ? "tree" : "raw";
  };

  holder.append(bar, tree, raw);
  return holder;
}

function reportValue(title, value, failed) {
  const card = element("div", `result${failed ? " failed" : ""}`);
  card.append(element("h4", null, title));
  card.append(valueViews(value));
  page.outcome.append(card);
}

/* What to show for one tool result: text, a value, or both.
 *
 * A value is shown wherever there is one. `structuredContent` is one, and so
 * is text that parses as an object or an array -- a server states JSON
 * compactly because it is going over a wire, and the two revisions older than
 * 2025-06-18 have no `structuredContent` to put it in at all. Anything that
 * does not parse is not JSON and stays the text it is.
 *
 * `"5"`, `"true"` and `"null"` do parse, but to a number, a boolean and
 * nothing. None of those is a tree, and a tool that answers in one word means
 * the word.
 */
function resultViews(result, text) {
  const structured = result.structuredContent;
  const held = structured !== undefined && structured !== null;
  const fromText = parsed(text);
  const structural = fromText !== null && fromText !== undefined && typeof fromText === "object";
  const value = held ? structured : structural ? fromText : undefined;
  // Text that says the same thing as the value beside it says nothing the
  // second time.
  const repeats = value !== undefined && same(fromText, value);
  return {
    text: text && !repeats ? text : null,
    value,
    label: held && !repeats ? "Structured content" : "Result",
  };
}

function report(title, body, failed) {
  const card = element("div", `result${failed ? " failed" : ""}`);
  card.append(element("h4", null, title));
  const text = String(body);
  const held = parsed(text);
  card.append(element("pre", null, held === undefined ? text : JSON.stringify(held, null, 2)));
  page.outcome.append(card);
}

function resourceUris(value, found = new Set()) {
  if (!value || typeof value !== "object") return found;
  for (const [key, child] of Object.entries(value)) {
    if (key === "uri" && typeof child === "string" && /^[a-z][a-z0-9+.-]*:/i.test(child)) {
      found.add(child);
    } else if (child && typeof child === "object") resourceUris(child, found);
  }
  return found;
}

function reportContents(result) {
  const links = new Set();
  (result.contents || []).forEach((part) => {
    if (part.text === undefined) {
      reportValue(part.mimeType || "contents", part);
      return;
    }
    const held = parsed(part.text);
    if (held === undefined || typeof held !== "object") {
      report(part.mimeType || "contents", part.text);
    } else {
      reportValue(part.mimeType || "contents", held);
      resourceUris(held, links);
    }
  });
  if (links.size) {
    const group = element("div", "group");
    group.append(element("h4", null, "Read a resource"));
    links.forEach((uri) => {
      const button = element("button", "item", uri);
      button.type = "button";
      button.onclick = () => {
        choose({ kind: "resource", item: { uri } });
        invoke();
      };
      group.append(button);
    });
    page.outcome.append(group);
  }
}

async function verifiedSkillFile(result, file) {
  const parts = result.contents || [];
  if (parts.length !== 1 || parts[0].uri !== file.uri) {
    throw new Error("The resource response does not match the requested file.");
  }
  const part = parts[0];
  let bytes;
  if (typeof part.text === "string") bytes = new TextEncoder().encode(part.text);
  else if (typeof part.blob === "string") bytes = Uint8Array.from(atob(part.blob), (char) => char.charCodeAt(0));
  else throw new Error("The resource response has no file content.");
  if (bytes.length !== file.size) throw new Error("The file size differs from its manifest. Inspect the skill again.");
  if (!globalThis.crypto || !globalThis.crypto.subtle) {
    throw new Error("File verification requires HTTPS or localhost.");
  }
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
  const hex = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (`sha256:${hex}` !== file.digest) throw new Error("The file digest differs from its manifest. Inspect the skill again.");
  return result;
}

function reportSkill(skill) {
  reportValue("Frontmatter", skill.frontmatter);
  const dynamic = skill.resources === "dynamic";
  if (!dynamic && !Array.isArray(skill.resources)) throw new Error("The skill has no file manifest.");
  if (dynamic) report("Files", "Read the current instructions below. This dynamic manifest provides no file digests.");
  else reportValue("File manifest", skill.resources);
  const files = element("div", "group");
  files.append(element("h4", null, "Read a file"));
  const selected = chosen;
  const source = client;
  (dynamic ? [{ uri: skill.uri }] : skill.resources).forEach((file) => {
    const button = element("button", "item", dynamic ? file.uri : `${file.uri} (${file.size} bytes)`);
    button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      try {
        const content = await source.readResource(file.uri);
        const result = dynamic ? content : await verifiedSkillFile(content, file);
        if (chosen === selected && client === source) reportContents(result);
      } catch (error) {
        if (chosen === selected && client === source) report("Could not read file", error.message, true);
      } finally {
        button.disabled = false;
      }
    };
    files.append(button);
  });
  page.outcome.append(files);
}

async function invoke() {
  if (!chosen || !client) return;
  page.invoke.disabled = true;
  page.outcome.textContent = "";
  const empty = missing(chosen.fields);
  chosen.fields.forEach((input) => input.classList.remove("wanting"));
  if (empty.length) {
    empty.forEach((input) => input.classList.add("wanting"));
    const names = empty.map((input) => input.dataset.name).join(", ");
    report("Not sent", `${names} ${empty.length > 1 ? "are" : "is"} required.`, true);
    empty[0].focus();
    page.invoke.disabled = false;
    return;
  }
  try {
    const values = readFields(chosen.fields);
    let result;
    if (chosen.kind === "tool") {
      result = await client.callTool(chosen.item.name, values);
      const failed = result.isError === true;
      const text = (result.content || [])
        .map((block) => (block.type === "text" ? block.text : JSON.stringify(block)))
        .join("\n");
      const views = resultViews(result, text);
      if (views.text !== null) {
        report(failed ? "Result (isError)" : "Result", views.text, failed);
      }
      if (views.value !== undefined) reportValue(views.label, views.value, failed);
      if (views.text === null && views.value === undefined) {
        reportValue("Result", result, failed);
      }
    } else if (chosen.kind === "skill") {
      reportSkill(await client.getSkill(chosen.item.uri));
    } else if (chosen.kind === "extension") {
      const params = values.params === undefined ? {} : values.params;
      if (params === null || typeof params !== "object" || Array.isArray(params)) {
        throw new Error("params must be a JSON object");
      }
      result = await client.request(values.method, params);
      reportValue("Result", result);
    } else if (chosen.kind === "prompt") {
      result = await client.getPrompt(chosen.item.name, values);
      (result.messages || []).forEach((message) => {
        const content = message.content || {};
        report(message.role, content.text !== undefined ? content.text : content);
      });
    } else {
      const uri =
        chosen.kind === "resource"
          ? chosen.item.uri
          : chosen.item.uriTemplate.replace(/\{([^}]+)\}/g, (whole, name) =>
              values[name] === undefined ? whole : encodeURIComponent(values[name])
            );
      result = await client.readResource(uri);
      reportContents(result);
    }
  } catch (error) {
    const detail = error.code !== undefined ? ` (${error.code})` : "";
    report(`Failed${detail}`, error.message, true);
    if (error.data !== undefined && error.data !== null) {
      reportValue("Error data", error.data, true);
    }
  } finally {
    page.invoke.disabled = false;
  }
}


function disconnect() {
  // Sessions expire server-side; there is no close request.
  client = null;
  chosen = null;
  page.catalogue.textContent = "";
  page.catalogue.append(element("p", "empty", "Connect to see what this server offers."));
  page.subject.textContent = "Nothing selected";
  page.about.textContent = "";
  page.args.textContent = "";
  page.outcome.textContent = "";
  page.invoke.disabled = true;
  page.refresh.disabled = true;
  page.revision.disabled = false;
  page.answerable.disabled = false;
  page.endpoint.disabled = false;
  page.connect.textContent = "Connect";
  say("not connected", "");
}

async function connect() {
  page.connect.disabled = true;
  page.revision.disabled = true;
  page.answerable.disabled = true;
  page.endpoint.disabled = true;
  say("connecting", "");
  page.outcome.textContent = "";
  try {
    endpointUrl = chosenEndpoint();
    client = new Client(endpointUrl.href, page.revision.value, {
      answerable: page.answerable.checked,
      onFrame: record,
      onNotify: noticed,
      onQuestion: askPerson,
    });
    const described = await client.initialize();
    await loadCatalogue();
    page.refresh.disabled = false;
    const name = (described.serverInfo || (described._meta || {})["io.modelcontextprotocol/serverInfo"] || {}).name;
    say(`${name || "connected"} · ${client.version}`, "on");
    page.connect.textContent = "Disconnect";
    if (described.instructions) {
      page.subject.textContent = "Instructions";
      prose(described.instructions, page.about);
      page.invoke.disabled = true;
    }
  } catch (error) {
    client = null;
    page.refresh.disabled = true;
    page.revision.disabled = false;
    page.answerable.disabled = false;
    page.endpoint.disabled = false;
    page.connect.textContent = "Connect";
    say(error.message, "off");
    report("Could not connect", connectionHelp(error), true);
  } finally {
    page.connect.disabled = false;
  }
}

function connectionHelp(error) {
  const lines = [error.message];
  if (endpointUrl && (error instanceof TypeError || /fetch/i.test(error.message))) {
    lines.push("", `The request to ${endpointUrl.href} did not reach a server.`);
  }
  return lines.join("\n");
}


const NARROW = "(max-width: 900px)";

function folding(panel, toggle, shutWhenNarrow) {
  const narrow = window.matchMedia(NARROW);
  let chosenByHand = false;

  const show = (open) => {
    panel.classList.toggle("shut", !open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) panel.classList.remove("waiting");
  };

  const settle = (isNarrow) => {
    chosenByHand = false;
    show(!isNarrow || !shutWhenNarrow);
  };

  settle(narrow.matches);

  // Fold on header clicks without intercepting its action buttons.
  panel.querySelector(".panel-head").onclick = (event) => {
    if (!narrow.matches) return;
    const control = event.target.closest("button, input, label, select");
    if (control && control !== toggle) return;
    chosenByHand = true;
    show(panel.classList.contains("shut"));
  };
  // Reset layout defaults when the viewport changes.
  narrow.addEventListener("change", (event) => settle(event.matches));

  return {
    fold: () => {
      if (narrow.matches) show(false);
    },
    isShut: () => panel.classList.contains("shut"),
    chosenByHand: () => chosenByHand,
  };
}

let traffic = null;
let catalogue = null;

function foldPanels() {
  traffic = folding(page.trafficPanel, page.trafficToggle, true);
  catalogue = folding(page.cataloguePanel, page.catalogueToggle, false);
}

function markTraffic() {
  if (!traffic || !traffic.isShut() || traffic.chosenByHand()) return;
  page.trafficPanel.classList.add("waiting");
}


function start() {
  Object.keys(REVISIONS).forEach((version) => {
    const option = element("option", null, version);
    option.value = version;
    page.revision.append(option);
  });
  const remembered = localStorage.getItem("mcp-console-revision");
  page.revision.value = REVISIONS[remembered] ? remembered : Object.keys(REVISIONS)[0];

  configuredEndpoint = new URL(document.documentElement.dataset.endpoint || "/mcp", location.origin);
  page.endpoint.value = rememberedEndpoint() || configuredEndpoint.pathname + configuredEndpoint.search;
  page.endpoint.onkeydown = (event) => {
    if (event.key === "Enter" && !client) {
      event.preventDefault();
      connect();
    }
  };

  page.connect.onclick = () => (client ? disconnect() : connect());
  page.refresh.onclick = async () => {
    try {
      if (client.rules.extensions) await client.initialize();
      await loadCatalogue();
    } catch (error) {
      say(error.message, "off");
    }
  };
  // Call is the form's submit button, though it sits outside the form -- the
  // `form` attribute says which form it belongs to. That is what makes Enter
  // in a field call the tool: a form with several fields and no submit button
  // ignores Enter, which is the rule rather than a quirk. A textarea keeps
  // Enter for what Enter does in a textarea.
  page.args.onsubmit = (event) => {
    event.preventDefault();
    invoke();
  };
  foldPanels();
  page.clear.onclick = () => {
    page.traffic.textContent = "";
    page.traffic.append(element("p", "empty", "Every message, as it goes over the wire."));
  };
  page.revision.onchange = () => {
    localStorage.setItem("mcp-console-revision", page.revision.value);
  };

  connect();
}

start();

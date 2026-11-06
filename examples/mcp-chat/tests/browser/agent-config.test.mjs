import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { agentConfig, shellQuote } from "../../mcp_chat/static/agent-config.js";

const endpoint = "https://chat.example/mcp?org=example.com&user=alice";
const token = "header.payload.signature";
const byId = (setup, id) => setup.variants.find((variant) => variant.id === id);

test("one-time Codex and persistent Codex use different commands", () => {
  const setup = agentConfig({ endpoint, user: "agent" });
  assert.match(byId(setup, "codex-once").code, /^codex -c /);
  assert.match(byId(setup, "codex-once").code, /mcp_servers\.mcp_chat\.url=/);
  assert.doesNotMatch(byId(setup, "codex-once").code, /mcp add/);
  assert.match(byId(setup, "codex-persistent").code, /^codex mcp add mcp_chat --url /);
  assert.match(byId(setup, "codex-persistent").extra.code, /\[mcp_servers\.mcp_chat\]/);
  for (const variant of setup.variants) assert.ok(variant.code.includes("user=agent"));
  assert.equal(new URL(setup.endpoint).searchParams.get("org"), "example.com");
  assert.match(setup.prompt, /chat:\/\/rules/);
  assert.match(setup.prompt, /mark_read/);
});

test("each JSON format uses its client's schema", () => {
  const setup = agentConfig({ endpoint });
  const claude = JSON.parse(byId(setup, "claude-project").code);
  const cursor = JSON.parse(byId(setup, "cursor").code);
  const vscode = JSON.parse(byId(setup, "vscode").code);
  assert.deepEqual(claude.mcpServers.mcp_chat, { type: "http", url: endpoint });
  assert.deepEqual(cursor.mcpServers.mcp_chat, { url: endpoint });
  assert.deepEqual(vscode.servers.mcp_chat, { type: "http", url: endpoint });
  assert.match(byId(setup, "claude-code").code, /--transport http --scope user/);
});

test("Bearer mode keeps the token out of the URL and sets client headers", () => {
  const setup = agentConfig({ endpoint, token, auth: "bearer" });
  assert.equal(new URL(setup.endpoint).searchParams.has("token"), false);
  assert.equal(new URL(setup.endpoint).searchParams.has("org"), false);
  assert.equal(new URL(setup.endpoint).searchParams.has("user"), false);
  assert.match(byId(setup, "codex-once").code, /http_headers/);
  assert.ok(byId(setup, "codex-once").code.includes(`Bearer ${token}`));
  assert.match(byId(setup, "codex-persistent").code, /--bearer-token-env-var MCP_CHAT_TOKEN/);
  assert.match(byId(setup, "codex-persistent").extra.code, /bearer_token_env_var/);
  assert.ok(!byId(setup, "codex-persistent").extra.code.includes(token));
  assert.ok(byId(setup, "claude-code").code.includes(`Authorization: Bearer ${token}`));
  for (const id of ["claude-project", "pi", "cursor", "vscode"]) {
    const config = JSON.parse(byId(setup, id).code);
    const server = (config.mcpServers || config.servers).mcp_chat;
    assert.equal(server.headers.Authorization, `Bearer ${token}`);
    assert.equal(server.url, "https://chat.example/mcp");
  }
});

test("URL mode embeds exactly one token and does not send duplicate credentials", () => {
  const setup = agentConfig({ endpoint: endpoint + "&token=old", token, auth: "url" });
  assert.deepEqual(new URL(setup.endpoint).searchParams.getAll("token"), [token]);
  assert.equal(new URL(setup.endpoint).searchParams.has("org"), false);
  assert.equal(new URL(setup.endpoint).searchParams.has("user"), false);
  for (const variant of setup.variants) {
    assert.ok(variant.code.includes(`token=${token}`));
    assert.doesNotMatch(variant.code, /Authorization|bearer-token-env-var/);
  }
  const clean = agentConfig({ endpoint: endpoint + "&token=old", token, auth: "bearer" });
  assert.equal(clean.endpoint, "https://chat.example/mcp");
});

test("Pi uses native MCP configuration for demo, Bearer, and URL authentication", () => {
  const demo = byId(agentConfig({ endpoint, user: "pi-agent" }), "pi");
  assert.match(demo.target, /~\/\.pi\/agent\/mcp\.json/);
  assert.match(demo.instructions, /npm install -g --ignore-scripts @earendil-works\/pi-coding-agent/);
  assert.equal(demo.link, "https://pi.dev/docs/latest/mcp");
  assert.match(demo.verify, /pi mcp list/);
  assert.match(demo.verify, /\/reload/);
  assert.deepEqual(JSON.parse(demo.code), {
    mcpServers: { mcp_chat: { url: "https://chat.example/mcp?org=example.com&user=pi-agent" } },
  });
  for (const auth of ["bearer", "url"]) {
    const variant = byId(agentConfig({ endpoint, user: "pi-agent", token, auth }), "pi");
    const server = JSON.parse(variant.code).mcpServers.mcp_chat;
    const url = new URL(server.url);
    assert.equal(url.searchParams.has("org"), false);
    assert.equal(url.searchParams.has("user"), false);
    if (auth === "bearer") {
      assert.equal(server.url, "https://chat.example/mcp");
      assert.deepEqual(server.headers, { Authorization: `Bearer ${token}` });
    } else {
      assert.deepEqual(url.searchParams.getAll("token"), [token]);
      assert.equal(server.headers, undefined);
    }
  }
});

test("POSIX shell quoting preserves spaces, quotes, ampersands, and substitutions", () => {
  const value = "https://host/mcp?org=a&user=o'brien $(printf wrong) `printf wrong`";
  const output = execFileSync("/bin/sh", ["-c", `printf '%s' ${shellQuote(value)}`], { encoding: "utf8" });
  assert.equal(output, value);
});

test("invalid identity, protocol, token controls, and auth modes are rejected", () => {
  assert.throws(() => agentConfig({ endpoint, user: "a\nb" }));
  assert.throws(() => agentConfig({ endpoint: "file:///etc/passwd" }));
  assert.throws(() => agentConfig({ endpoint: "https://name:secret@chat.example/mcp" }));
  assert.throws(() => agentConfig({ endpoint, token: "token\r\nInjected: yes" }));
  assert.throws(() => agentConfig({ endpoint, token, auth: "invalid" }));
});

"""Console route exemptions and browser authentication over each protocol revision."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aiohttp_tiny_mcp import Endpoint, Registry, StaticBasicAuth
from aiohttp_tiny_mcp.console import Console

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/console.js"


@pytest.mark.asyncio
@pytest.mark.parametrize("mount,subapp", [("/", False), ("/console", False), ("/try", True)])
async def test_only_mounted_console_assets_bypass_application_authentication(mount, subapp):
    @web.middleware
    async def protect(request, handler):
        if not Console.is_public(request):
            raise web.HTTPUnauthorized()
        return await handler(request)

    root = web.Application(middlewares=[protect])
    app = web.Application() if subapp else root
    Console().setup(app, mount)

    async def private(request):
        return web.Response()

    app.router.add_get("/private/console.js", private)
    if subapp:
        root.add_subapp("/reports", app)
    prefix = "/reports" if subapp else ""
    base = prefix + mount.rstrip("/")
    async with TestClient(TestServer(root)) as client:
        for method in ("GET", "HEAD"):
            for path in {base or "/", base + "/", base + "/console.js", base + "/console.css"}:
                response = await client.request(method, path)
                assert response.status == 200, (method, path)
        for path in (
            base + "/console.js/extra",
            base + "/console.css.bak",
            base + "/secrets",
            prefix + "/private/console.js",
            prefix + "/mcp",
        ):
            assert (await client.get(path)).status == 401, path
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            for path in {base or "/", base + "/console.js"}:
                assert (await client.request(method, path)).status == 401


@pytest.mark.asyncio
async def test_disabled_console_does_not_exempt_routes():
    @web.middleware
    async def protect(request, handler):
        assert not Console.is_public(request)
        raise web.HTTPUnauthorized()

    app = web.Application(middlewares=[protect])
    async with TestClient(TestServer(app)) as client:
        for path in ("/", "/console", "/console.js", "/console/console.css"):
            assert (await client.get(path)).status == 401


@pytest.mark.asyncio
async def test_registry_auth_leaves_assets_public_and_protects_mcp():
    app = Endpoint(Registry("private", "1", auth=StaticBasicAuth(("alice", "secret")))).app()
    Console().setup(app)
    async with TestClient(TestServer(app)) as client:
        for method in ("GET", "HEAD"):
            for path in ("/console", "/console/console.js", "/console/console.css"):
                assert (await client.request(method, path)).status == 200
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json"},
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"].startswith("Basic ")


def run_js(body):
    source = SCRIPT.read_text()
    source = source[: source.index("const page =")]
    driver = (
        'const assert = require("node:assert/strict");\n'
        + source
        + "\n(async () => {\n"
        + body
        + "\n})().catch(error => { console.error(error); process.exit(1); });"
    )
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_challenges_and_scoped_credentials():
    run_js(r"""
    assert.deepEqual(authenticationSchemes(
      'Basic realm="one, Bearer two\\\"", charset="UTF-8", bEaReR error="invalid_token", ' +
      'error_description="try, again", resource_metadata="https://host/mcp"'), ["basic", "bearer"]);
    assert.deepEqual(authenticationSchemes('Basic, Bearer, ApiKey realm = "x"'),
      ["basic", "bearer", "apikey"]);
    assert.deepEqual(authenticationSchemes(""), []);
    const credentials = new ConsoleCredentials();
    const url = "http://localhost/mcp?mcp=2025-11-25";
    credentials.set(url, "basic", "алиса", "päss:word", "");
    assert.equal(credentials.forEndpoint(url).Authorization,
      "Basic " + Buffer.from("алиса:päss:word").toString("base64"));
    assert.equal(credentials.forEndpoint("http://localhost/mcp?resources=tool").Authorization,
      credentials.forEndpoint(url).Authorization);
    const others = ["http://localhost/other", "https://elsewhere/mcp", "https://localhost/mcp"];
    for (const other of others) {
      assert.deepEqual(credentials.forEndpoint(other), {});
    }
    const headers = credentials.forEndpoint(url);
    headers.Authorization = "changed";
    assert.notEqual(credentials.forEndpoint(url).Authorization, "changed");
    credentials.set(url, "bearer", "", "token.test", "");
    assert.equal(credentials.forEndpoint(url).Authorization, "Bearer token.test");
    credentials.set(url, "custom", "", "secret", "X-API-Key");
    assert.deepEqual(credentials.forEndpoint(url), {"X-API-Key": "secret"});
    for (const header of ["Cookie", "Content-Type", "Mcp-Method", "X-Test\r\nEvil"]) {
      assert.throws(() => credentials.set(url, "custom", "", "secret", header));
    }
    assert.throws(() => credentials.set(url, "basic", "a:b", "secret", ""));
    assert.throws(() => credentials.set(url, "basic", "alice", "secret\n", ""));
    assert.throws(() => credentials.set(url, "bearer", "", "bad\r\nvalue", ""));
    credentials.clear();
    assert.deepEqual(credentials.forEndpoint(url), {});
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_credentials_restore_raw_fields_and_headers_without_storage_writes():
    run_js(r"""
    globalThis.location = {pathname: "/console"};
    const saved = new Map();
    let writes = 0;
    globalThis.localStorage = {
      getItem(key) { return saved.get(key) ?? null; },
      setItem(key, value) { writes++; saved.set(key, value); },
      removeItem(key) { saved.delete(key); },
    };
    const url = "http://localhost/mcp?team=one";
    const cases = [
      ["basic", "алиса", "päss:word", ""],
      ["bearer", "", "token.test", ""],
      ["custom", "", "api-secret", "X-API-Key"],
      ["none", "", "", ""],
      ["query", "", "", ""],
    ];
    for (const [method, username, secret, header] of cases) {
      const original = new ConsoleCredentials();
      original.set(url, method, username, secret, header);
      const headers = original.forEndpoint(url);
      const count = writes;
      original.clear();
      assert.equal(original.record, null);
      const restored = new ConsoleCredentials();
      assert.equal(restored.restore(url), true);
      assert.equal(writes, count);
      assert.deepEqual(restored.forEndpoint(url), headers);
      assert.deepEqual(restored.record, {
        endpoint: 'http://localhost/mcp', method, username, secret, header,
      });
      if (["none", "query"].includes(method)) assert.deepEqual(headers, {});
    }
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_saved_credentials_are_scoped_and_forgotten_independently():
    run_js("""
    globalThis.location = {pathname: "/console"};
    const saved = new Map();
    globalThis.localStorage = {
      getItem(key) { return saved.get(key) ?? null; },
      setItem(key, value) { saved.set(key, value); },
      removeItem(key) { saved.delete(key); },
    };
    const credentials = new ConsoleCredentials();
    const first = "http://localhost/mcp?team=one";
    const second = "http://localhost/other?team=two";
    credentials.set(first, "bearer", "", "first-token", "");
    credentials.set(second, "custom", "", "second-secret", "X-API-Key");
    credentials.clear();
    assert.equal(saved.size, 2);
    assert.equal(credentials.restore(first), true);
    assert.equal(credentials.forEndpoint(first).Authorization, "Bearer first-token");
    assert.deepEqual(credentials.forEndpoint(second), {});
    location.pathname = "/other-console";
    assert.equal(credentials.restore(first), false);
    assert.deepEqual(credentials.forEndpoint(first), {});
    location.pathname = "/console";
    assert.equal(credentials.restore(second), true);
    credentials.forget(first);
    assert.deepEqual(credentials.forEndpoint(second), {"X-API-Key": "second-secret"});
    assert.equal(saved.size, 1);
    credentials.forget(second);
    assert.equal(credentials.record, null);
    assert.deepEqual(credentials.forEndpoint(second), {});
    assert.equal(saved.size, 0);
    assert.equal(credentials.restore(first), false);
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_query_changes_share_saved_credentials_and_clear_them():
    run_js("""
    globalThis.location = {pathname: '/console'};
    const saved = new Map();
    globalThis.localStorage = {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key),
    };
    const original = 'https://example.com/mcp?team=one&token=query-token';
    const changed = 'https://example.com/mcp?team=two&resources=tool&mode=other';
    const credentials = new ConsoleCredentials();
    credentials.set(original, 'bearer', '', 'access-token', '');
    assert.equal(credentials.forEndpoint(changed).Authorization, 'Bearer access-token');
    const reloaded = new ConsoleCredentials();
    assert.equal(reloaded.restore(changed), true);
    assert.equal(reloaded.forEndpoint(original).Authorization, 'Bearer access-token');
    assert.equal(saved.size, 1);
    assert.ok(!JSON.stringify([...saved]).includes('query-token'));
    for (const other of ['https://other.example/mcp', 'http://example.com/mcp',
      'https://example.com/other', 'https://example.com/mcp/']) {
      assert.deepEqual(reloaded.forEndpoint(other), {});
    }
    reloaded.forget(changed);
    assert.equal(reloaded.restore(original), false);
    assert.equal(saved.size, 0);
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_exact_legacy_credentials_migrate_without_scanning_other_records():
    run_js("""
    globalThis.location = {pathname: '/console'};
    const saved = new Map();
    globalThis.localStorage = {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key),
    };
    const endpoint = 'https://example.com/mcp?team=one';
    const credentials = new ConsoleCredentials();
    const legacyKey = credentials.legacyStorageKey(endpoint);
    saved.set(legacyKey, JSON.stringify({version: 1, endpoint,
      method: 'bearer', username: '', secret: 'token', header: ''}));
    assert.equal(credentials.restore(endpoint + '&other=1'), false);
    assert.equal(credentials.restore(endpoint), true);
    assert.equal(saved.has(legacyKey), false);
    assert.equal(saved.size, 1);
    const changed = 'https://example.com/mcp?resources=tool';
    assert.equal(credentials.restore(changed), true);
    assert.equal(credentials.forEndpoint(changed).Authorization, 'Bearer token');
    credentials.forget(changed);
    assert.equal(credentials.restore(endpoint), false);
    assert.equal(saved.size, 0);
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dialog_clear_only_removes_selected_endpoint_credentials():
    source = SCRIPT.read_text()
    setup = source[
        source.index("function setupAuthentication()") : source.index("function endpointChanged()")
    ]
    run_js(
        setup
        + """
    globalThis.location = {pathname: '/console'};
    const saved = new Map();
    globalThis.localStorage = {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key),
    };
    const page = new Proxy({}, {get(target, key) {
      return target[key] ||= {value: '', addEventListener() {}, close() {}};
    }});
    const credentials = new ConsoleCredentials();
    credentials.set('http://localhost/current', 'bearer', '', 'current-token', '');
    credentials.set('http://localhost/selected?old=1', 'bearer', '', 'selected-token', '');
    function chosenEndpoint() { return new URL('http://localhost/current'); }
    function authenticationEndpoint() { return new URL('http://localhost/selected?token=query'); }
    function endpointKey() { return 'last-endpoint'; }
    function clearCredentials() { credentials.clear(); }
    function disconnect() {}
    function resetOAuth() {}
    function signInOAuth() {}
    function authFields() {}
    function endpointChanged() {}
    setupAuthentication();
    page.authClear.onclick();
    assert.equal(credentials.restore('http://localhost/selected?different=1'), false);
    assert.equal(credentials.restore('http://localhost/current'), true);
    assert.equal(page.endpoint.value, '/selected');
    """
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_restored_credentials_reject_invalid_or_mismatched_records():
    run_js(r"""
    globalThis.location = {pathname: "/console"};
    const url = "http://localhost/mcp";
    const valid = {
      version: 1, endpoint: url, method: "bearer", username: "", secret: "token", header: "",
    };
    let stored;
    globalThis.localStorage = {getItem() { return stored; }};
    const credentials = new ConsoleCredentials();
    const invalid = [
      null, [], {}, {...valid, version: 2}, {...valid, endpoint: url + "/different"},
      {...valid, method: "unknown"}, {...valid, secret: "bad\r\nvalue"},
      {...valid, secret: 42}, {...valid, username: null}, {...valid, header: false},
      {...valid, method: "custom", header: "Cookie"},
      {...valid, method: "basic", username: "a:b"},
    ];
    for (const record of invalid) {
      credentials.set(url, "bearer", "", "old-token", "");
      stored = JSON.stringify(record);
      assert.equal(credentials.restore(url), false);
      assert.deepEqual(credentials.forEndpoint(url), {});
      assert.equal(credentials.record, null);
    }
    stored = "invalid JSON";
    assert.equal(credentials.restore(url), false);
    stored = JSON.stringify({...valid, headers: {Cookie: "injected"}});
    assert.equal(credentials.restore(url), true);
    assert.deepEqual(credentials.forEndpoint(url), {Authorization: "Bearer token"});
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_unavailable_storage_does_not_break_credential_operations():
    run_js("""
    const credentials = new ConsoleCredentials();
    const url = "http://localhost/mcp";
    credentials.set(url, "bearer", "", "token", "");
    assert.equal(credentials.forEndpoint(url).Authorization, "Bearer token");
    assert.equal(credentials.restore(url), false);
    globalThis.location = {pathname: "/console"};
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new Error("Storage blocked"); },
    });
    credentials.set(url, "bearer", "", "token", "");
    assert.equal(credentials.forEndpoint(url).Authorization, "Bearer token");
    assert.equal(credentials.restore(url), false);
    credentials.set(url, "bearer", "", "token", "");
    credentials.forget(url);
    assert.deepEqual(credentials.forEndpoint(url), {});
    delete globalThis.localStorage;
    globalThis.localStorage = {
      getItem() { return null; },
      setItem() { throw new Error("Quota exceeded"); },
      removeItem() { throw new Error("Storage blocked"); },
    };
    credentials.set(url, "basic", "alice", "secret", "");
    assert.ok(credentials.forEndpoint(url).Authorization.startsWith("Basic "));
    credentials.forget(url);
    assert.equal(credentials.record, null);
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_all_requests_carry_credentials_without_logging_them():
    run_js("""
    for (const version of Object.keys(REVISIONS)) {
      const traffic = [], sent = [];
      globalThis.fetch = async (url, options) => {
        assert.equal(url, "http://localhost/mcp");
        assert.equal(options.headers.Authorization, "Bearer secret");
        assert.equal(options.redirect, "error");
        assert.equal(options.credentials, "omit");
        const envelope = JSON.parse(options.body);
        sent.push(envelope);
        if (envelope.id && envelope.method) return new Response(JSON.stringify({
          jsonrpc: "2.0", id: envelope.id, result: {capabilities: {}}
        }), {headers: {"Content-Type": "application/json", "Mcp-Session-Id": "session"}});
        assert.equal(options.headers["Mcp-Session-Id"], "session");
        return new Response(null, {status: 202});
      };
      const client = new Client("http://localhost/mcp", version, {
        authHeaders: {Authorization: "Bearer secret"},
        onFrame: (...frame) => traffic.push(frame), answerable: true,
        onQuestion: async () => ({action: "decline"}),
      });
      await client.initialize();
      await client.notify("notifications/test", {});
      await client.incoming({id: "question", method: "elicitation/create"});
      assert.equal(sent.at(-1).result.action, "decline");
      assert.ok(!JSON.stringify(traffic).includes("secret"));
      client.abort.abort();
      assert.ok(client.abort.signal.aborted);
    }
    """)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_http_authentication_failures_are_not_retried_or_parsed_as_rpc():
    run_js("""
    for (const status of [401, 403, 500]) {
      let sent = 0, challenged = 0;
      globalThis.fetch = async () => {
        sent++;
        return new Response("<html>denied</html>", {status,
          headers: {"WWW-Authenticate": 'Basic realm="test", Bearer'}});
      };
      const client = new Client("http://localhost/mcp", "2026-07-28", {
        onFrame() {}, onAuthError(error) {
          challenged++;
          assert.deepEqual(error.schemes, ["basic", "bearer"]);
        },
      });
      await assert.rejects(client.request("tools/call", {}), error => error.status === status);
      assert.equal(sent, 1);
      assert.equal(challenged, status === 500 ? 0 : 1);
      await assert.rejects(client.notify("notifications/test", {}), /HTTP/);
      await assert.rejects(client.incoming({id: 2, method: "elicitation/create"}), /HTTP/);
    }
    globalThis.fetch = async () => new Response(JSON.stringify({jsonrpc: "2.0", id: 1,
      error: {code: -32601, message: "Unknown method", data: {method: "unknown"}}}), {status: 404});
    const client = new Client("http://localhost/mcp", "2026-07-28", {onFrame() {}});
    await assert.rejects(client.request("unknown", {}), error =>
      error.code === -32601 && error.data.method === "unknown");
    globalThis.fetch = async () => new Response(JSON.stringify({jsonrpc: "2.0", id: 2,
      error: {code: -32000, message: "Scope required"}}), {status: 403});
    await assert.rejects(client.request("tools/call", {}), /Scope required/);
    client.pages = async (method) => {
      if (method === "resources/list") return [];
      throw new HttpError(new Response(null, {status: 401}));
    };
    await assert.rejects(client.listResources(), /Authentication required/);
    """)

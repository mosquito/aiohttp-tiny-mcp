"""Exercise console notification streams with the browser client's actual JavaScript."""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console"
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
pytestmark = pytest.mark.timeout(10)

HARNESS = r"""
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function until(condition) {
  const deadline = Date.now() + 2500;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error('Condition did not become true');
    await pause(5);
  }
}
function setup(version, enabled = true) {
  const state = {
    requests: [], streams: [], received: [], traffic: [], errors: [], auth: [], active: 0,
  };
  const capabilities = {
    tools: {listChanged: true}, prompts: {listChanged: true},
    resources: {listChanged: true, subscribe: true},
    extensions: {'example.test/events': {notifications: {'notifications/example': null}}},
  };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, 'http://localhost/mcp?org=example');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    assert.equal(options.headers.Authorization, 'Bearer private-token');
    const body = options.body ? JSON.parse(options.body) : null;
    state.requests.push({options, body});
    if (options.method === 'GET' || body?.method === 'subscriptions/listen') {
      if (state.refuse) return new Response('refused', {status: state.refuse,
        headers: {'WWW-Authenticate': 'Bearer'}});
      const stream = {closed: false, options, body};
      const close = () => {
        if (stream.closed) return;
        stream.closed = true;
        state.active--;
      };
      const response = new Response(new ReadableStream({
        start(controller) {
          stream.controller = controller;
          state.active++;
          options.signal.addEventListener('abort', () => {
            if (stream.closed) return;
            close();
            controller.error(new DOMException('Aborted', 'AbortError'));
          }, {once: true});
        },
        cancel() { close(); },
      }), {headers: {'Content-Type': 'text/event-stream'}});
      stream.send = frame => stream.controller.enqueue(
        new TextEncoder().encode('data: ' + JSON.stringify(frame) + '\n\n'));
      stream.end = () => { close(); stream.controller.close(); };
      state.streams.push(stream);
      return response;
    }
    let result = {};
    if (['initialize', 'server/discover'].includes(body.method)) result = {capabilities};
    else if (body.method === 'resources/list') result = {resources: [{uri: 'config://app'}]};
    else if (body.method === 'resources/templates/list') result = {
      resourceTemplates: [{uriTemplate: 'config://items/{id}'}],
    };
    else if (body.method === 'resources/read') result = {
      contents: [{uri: body.params.uri, text: 'ok'}],
    };
    if (body.id === undefined) return new Response(null, {status: 202});
    return new Response(JSON.stringify({jsonrpc: '2.0', id: body.id, result}), {headers: {
      'Content-Type': 'application/json',
      ...(version === '2026-07-28' ? {} : {'Mcp-Session-Id': 'session-test'}),
    }});
  };
  state.client = new Client('http://localhost/mcp?org=example', version, {
    notifications: enabled,
    authHeaders: {Authorization: 'Bearer private-token'},
    onFrame: (direction, frame) => state.traffic.push({direction, frame}),
    onNotify: frame => state.received.push(frame),
    onAuthError: error => state.auth.push(error),
    onNotificationError: error => state.errors.push(error),
  });
  state.prepare = async () => {
    await state.client.initialize();
    await state.client.listResources();
  };
  state.start = async () => {
    await state.prepare();
    state.client.startNotifications();
    await until(() => state.streams.length > 0);
  };
  return state;
}
"""


def run(body: str) -> None:
    if NODE is None:
        pytest.skip("node is not installed")
    source = (CONSOLE / "console.js").read_text()
    driver = (
        'const assert = require("node:assert/strict");\n'
        + source[: source.index("const page =")]
        + HARNESS
        + "\n(async () => {\n"
        + body
        + "\n})().catch(error => { console.error(error); process.exit(1); });"
    )
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_modern_subscription_receives_events_without_a_tool_call() -> None:
    run("""
    const state = setup('2026-07-28');
    await state.start();
    const stream = state.streams[0];
    assert.equal(stream.options.method, 'POST');
    assert.equal(stream.options.headers['Mcp-Method'], 'subscriptions/listen');
    assert.equal(stream.options.headers['MCP-Protocol-Version'], '2026-07-28');
    assert.equal(stream.body.params._meta['io.modelcontextprotocol/protocolVersion'], '2026-07-28');
    assert.deepEqual(stream.body.params.notifications, {
      toolsListChanged: true, promptsListChanged: true, resourcesListChanged: true,
      resourceSubscriptions: ['config://app'], methods: ['notifications/example'],
    });
    const event = {jsonrpc: '2.0', method: 'notifications/resources/updated',
      params: {uri: 'config://app'}};
    stream.send(event);
    await until(() => state.received.length === 1);
    assert.deepEqual(state.received[0], event);
    assert.ok(state.traffic.some(item =>
      item.direction === 'received' && item.frame.method === event.method));
    assert.ok(!JSON.stringify(state.traffic).includes('private-token'));
    state.client.startNotifications();
    await pause(20);
    assert.equal(state.streams.length, 1);
    state.client.abort.abort();
    await until(() => state.active === 0);
    assert.deepEqual(state.errors, []);
    """)


def test_legacy_reconnect_does_not_skip_an_incomplete_event() -> None:
    run("""
    const state = setup('2025-11-25');
    await state.start();
    const stream = state.streams[0];
    const encode = text => new TextEncoder().encode(text);
    const event = {jsonrpc: '2.0', method: 'notifications/example', params: {value: 1}};
    stream.controller.enqueue(encode('id: 40\\ndata: ' + JSON.stringify(event) + '\\n\\n'));
    await until(() => state.client.lastEventId === '40');
    stream.controller.enqueue(encode('id: 41\\n'));
    stream.end();
    await until(() => state.streams.length === 2);
    assert.equal(state.received.length, 1);
    assert.equal(state.streams[1].options.headers['Last-Event-ID'], '40');
    state.streams[1].controller.enqueue(encode(
      'id: 41\\ndata: ' + JSON.stringify({...event, params: {value: 2}}) + '\\n\\n'));
    await until(() => state.received.length === 2);
    assert.equal(state.received[1].params.value, 2);
    assert.equal(state.client.lastEventId, '41');
    state.client.abort.abort();
    await state.client.notificationTask;
    """)


@pytest.mark.parametrize("version", LEGACY)
def test_legacy_subscribes_before_opening_authenticated_get(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.start();
    const subscribe = state.requests.find(item => item.body?.method === 'resources/subscribe');
    assert.equal(subscribe.body.params.uri, 'config://app');
    assert.equal(subscribe.options.headers['Mcp-Session-Id'], 'session-test');
    const stream = state.streams[0];
    assert.equal(stream.options.method, 'GET');
    assert.equal(stream.options.headers['Mcp-Session-Id'], 'session-test');
    const versionHeader = state.client.rules.versionHeader ? state.client.version : undefined;
    assert.equal(subscribe.options.headers['MCP-Protocol-Version'], versionHeader);
    assert.equal(stream.options.headers['MCP-Protocol-Version'], versionHeader);
    assert.equal(state.requests.at(-1).options.method, 'GET');
    const event = {jsonrpc: '2.0', method: 'notifications/example', params: {value: 1}};
    stream.send(event);
    await until(() => state.received.length === 1);
    assert.deepEqual(state.received[0], event);
    state.client.abort.abort();
    await until(() => state.active === 0);
    assert.deepEqual(state.errors, []);
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", *LEGACY])
def test_disabled_notifications_do_not_open_or_subscribe(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)}, false);\n"
        + """
    await state.prepare();
    state.client.startNotifications();
    await state.client.readResource('config://items/one');
    await pause(20);
    assert.equal(state.streams.length, 0);
    assert.ok(!state.requests.some(item => item.body?.method === 'resources/subscribe'));
    state.client.abort.abort();
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_reading_a_template_uri_adds_it_to_the_subscription(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.start();
    await state.client.readResource('config://items/one');
    await until(() => state.requests.some(item =>
      item.body?.params?.notifications?.resourceSubscriptions?.includes('config://items/one') ||
      (item.body?.method === 'resources/subscribe' && item.body.params.uri === 'config://items/one')));
    await until(() => state.active === 1);
    const event = {jsonrpc: '2.0', method: 'notifications/resources/updated',
      params: {uri: 'config://items/one'}};
    state.streams.findLast(stream => !stream.closed).send(event);
    await until(() => state.received.some(frame => frame.params?.uri === 'config://items/one'));
    state.client.abort.abort();
    await until(() => state.active === 0);
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_closed_stream_reports_error_reconnects_and_stops_on_disconnect(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.start();
    state.streams[0].end();
    await until(() => state.errors.length > 0);
    await until(() => state.streams.length === 2);
    assert.equal(state.active, 1);
    state.client.abort.abort();
    await until(() => state.active === 0);
    const count = state.requests.length;
    await pause(650);
    assert.equal(state.requests.length, count);
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_notification_authentication_failure_is_visible_and_not_retried(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.prepare();
    state.refuse = 401;
    state.client.startNotifications();
    await until(() => state.errors.length === 1);
    assert.equal(state.errors[0].status, 401);
    assert.equal(state.auth.length, 1);
    const count = state.requests.length;
    await pause(650);
    assert.equal(state.requests.length, count);
    state.client.abort.abort();
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_disconnect_cancels_a_notification_request_before_headers(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.prepare();
    const fetchNormally = globalThis.fetch;
    let pending = null;
    globalThis.fetch = async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      if (options.method !== 'GET' && body?.method !== 'subscriptions/listen') {
        return fetchNormally(url, options);
      }
      pending = options.signal;
      return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        }, {once: true});
      });
    };
    state.client.startNotifications();
    await until(() => pending !== null);
    state.client.abort.abort();
    await state.client.notificationTask;
    assert.ok(pending.aborted);
    assert.deepEqual(state.errors, []);
    """
    )


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_delayed_authentication_refusal_after_disconnect_does_not_reopen_ui(version: str) -> None:
    run(
        f"const state = setup({json.dumps(version)});\n"
        + """
    await state.prepare();
    const fetchNormally = globalThis.fetch;
    let respond = null, attempts = 0, cancelled = false;
    globalThis.fetch = async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      if (options.method !== 'GET' && body?.method !== 'subscriptions/listen') {
        return fetchNormally(url, options);
      }
      attempts++;
      return new Promise(resolve => { respond = resolve; });
    };
    state.client.startNotifications();
    await until(() => respond !== null);
    state.client.abort.abort();
    respond(new Response(new ReadableStream({
      cancel() { cancelled = true; },
    }), {status: 401, headers: {'WWW-Authenticate': 'Bearer'}}));
    await state.client.notificationTask;
    assert.equal(cancelled, true);
    assert.deepEqual(state.auth, []);
    assert.deepEqual(state.errors, []);
    await pause(650);
    assert.equal(attempts, 1);
    """
    )


def test_connection_controls_apply_notifications_and_reset_after_disconnect() -> None:
    source = (CONSOLE / "console.js").read_text()
    controls = source[source.index("function disconnect()") : source.index("const NARROW =")]
    run(
        controls
        + """
    const state = setup('2026-07-28');
    const page = Object.fromEntries([
      'connect', 'authenticate', 'revision', 'answerable', 'notifications', 'endpoint',
      'outcome', 'refresh', 'subject', 'about', 'invoke', 'args', 'catalogue',
    ].map(name => [name, {disabled: false, append() {}}]));
    page.revision.value = '2026-07-28';
    page.notifications.checked = true;
    let client = null, endpointUrl = null, chosen = null;
    const credentials = {forEndpoint: () => ({Authorization: 'Bearer private-token'})};
    function chosenEndpoint() { return new URL('http://localhost/mcp?org=example'); }
    function clearCredentials() {}
    function openAuthentication() {}
    function setServerInstructions() {}
    function showServerInstructions() {}
    function record() {}
    function noticed() {}
    function askPerson() {}
    function say() {}
    function element() { return {}; }
    function report(title, message) { throw new Error(title + ': ' + message); }
    async function loadCatalogue() { await client.listResources(); }
    await connect();
    assert.ok(page.notifications.disabled);
    assert.equal(page.connect.textContent, 'Disconnect');
    await until(() => state.active === 1);
    const previous = client;
    disconnect();
    await previous.notificationTask;
    assert.equal(state.active, 0);
    assert.equal(page.notifications.disabled, false);
    assert.equal(page.connect.textContent, 'Connect');
    page.notifications.checked = false;
    await connect();
    await pause(20);
    assert.ok(page.notifications.disabled);
    assert.equal(state.streams.length, 1);
    assert.equal(state.active, 0);
    disconnect();
    assert.equal(page.notifications.disabled, false);
    """
    )


def test_notifications_checkbox_is_enabled_by_default() -> None:
    class Inputs(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.notifications: dict[str, str | None] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "input" and values.get("id") == "notifications":
                self.notifications = values

    parser = Inputs()
    parser.feed((CONSOLE / "index.html").read_text())
    assert parser.notifications is not None
    assert parser.notifications["type"] == "checkbox"
    assert "checked" in parser.notifications
    assert "disabled" not in parser.notifications

"""Agent configuration snippets preserve endpoint settings and validate inputs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SOURCE = Path(__file__).resolve().parents[2] / "src/aiohttp_tiny_mcp/console/agent-config.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(body: str) -> str:
    driver = 'const assert = require("node:assert/strict");\n' + SOURCE.read_text() + body
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def run_dialog(body: str) -> None:
    source = SOURCE.with_name("console.js").read_text()
    credentials = source[source.index("class ConsoleCredentials") : source.index("class Client")]
    agents = source[source.index("let agentConnection") : source.index("function resetOAuth")]
    matching = source[
        source.index("function matchingEndpoint") : source.index("function endpointDialog")
    ]
    run(
        credentials
        + r"""
function element(tag, className, text) {
  return {
    value: '', storedText: text || '', children: [], hidden: false, checked: false,
    get textContent() { return this.storedText; },
    set textContent(value) { this.storedText = value; this.children = []; this.value = ''; },
    append(child) { this.children.push(child); },
    addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; },
  };
}
const page = new Proxy({}, {get(target, key) { return target[key] ||= element('input'); }});
const document = {documentElement: {dataset: {resourceTools: 'true'}}};
const location = new URL('https://example.com/console');
const stored = new Map();
const localStorage = {
  getItem: key => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, value),
  removeItem: key => stored.delete(key),
};
const credentials = new ConsoleCredentials();
const configuredEndpoint = new URL('/mcp', location.origin);
let endpointDeclarations = [];
page.serverTitle.textContent = 'Workspace console';
page.endpoint.value = '/mcp';
"""
        + matching
        + agents
        + "\nsetupAgentDialog();\n"
        + body
    )


def test_agent_schemas() -> None:
    run(r"""
const endpoint = 'https://example.com/mcp?org=one&user=alice';
const configs = agentConfigurations({endpoint, level: 'project'});
assert.deepEqual(configs.map(config => config.id), [
  'mcp-json', 'codex', 'cursor', 'copilot', 'gemini', 'pi', 'antigravity', 'opencode',
]);
for (const config of configs) {
  const keys = ['id', 'label', 'target', 'instructions', 'code', 'language', 'verify', 'link'];
  for (const key of keys) {
    assert.equal(typeof config[key], 'string');
    assert.ok(config[key].length);
  }
  assert.match(config.instructions, /Merge/);
  assert.match(config.link, /^https:\/\//);
}
const byId = Object.fromEntries(configs.map(config => [config.id, config]));
assert.equal(byId['mcp-json'].target, '.mcp.json');
assert.equal(byId['codex'].target, '.codex/config.toml');
assert.equal(byId['cursor'].target, '.cursor/mcp.json');
assert.equal(byId['copilot'].target, '.mcp.json');
assert.equal(byId['gemini'].target, '.gemini/settings.json');
assert.equal(byId['pi'].target, '.pi/mcp.json');
assert.equal(byId['antigravity'].target, '.agents/mcp_config.json');
assert.equal(byId['opencode'].target, 'opencode.json');
assert.deepEqual(JSON.parse(byId['mcp-json'].code), {
  mcpServers: {mcp: {type: 'http', url: endpoint}},
});
assert.deepEqual(JSON.parse(byId.cursor.code), {mcpServers: {mcp: {url: endpoint}}});
assert.deepEqual(JSON.parse(byId.copilot.code), {
  mcpServers: {mcp: {type: 'http', url: endpoint + '&resources=tool', tools: ['*']}},
});
assert.deepEqual(JSON.parse(byId.gemini.code), {mcpServers: {mcp: {httpUrl: endpoint}}});
assert.deepEqual(JSON.parse(byId.pi.code), {
  mcpServers: {mcp: {url: endpoint + '&resources=tool'}},
});
assert.deepEqual(JSON.parse(byId.antigravity.code), {
  mcpServers: {mcp: {serverUrl: endpoint + '&resources=tool'}},
});
assert.deepEqual(JSON.parse(byId.opencode.code), {
  $schema: 'https://opencode.ai/config.json',
  mcp: {servers: {mcp: {type: 'remote', url: endpoint}}},
});
assert.ok(!byId.codex.code.includes('http_headers'));
assert.match(byId.codex.verify, /codex mcp login mcp/);
""")


def test_headers_and_query_values_are_preserved() -> None:
    run(r"""
const endpoint = 'http://localhost:8080/mcp?org=a%20b&token=secret%2bvalue&token=second&x=%2F';
const headers = Object.freeze({'Authorization': 'Bearer abc"\\xyz', 'X-Key': 'value 😀'});
const options = Object.freeze({endpoint, name: 'chat-test_1', headers});
const before = JSON.stringify(options);
const configs = agentConfigurations(options);
assert.equal(JSON.stringify(options), before);
for (const config of configs.filter(config => config.language === 'json')) {
  const body = JSON.parse(config.code);
  const server = (body.mcpServers || body.mcp.servers)['chat-test_1'];
  const expected = ['pi', 'antigravity', 'copilot'].includes(config.id)
    ? endpoint + '&resources=tool' : endpoint;
  assert.equal(server.url || server.httpUrl || server.serverUrl, expected);
  assert.deepEqual(server.headers, headers);
  assert.ok(!config.verify.includes('sign in'));
}
const remote = JSON.parse(configs.find(config => config.id === 'opencode').code);
assert.equal(remote.mcp.servers['chat-test_1'].oauth, false);
const tokenOnly = agentConfigurations({endpoint});
const tokenRemote = JSON.parse(tokenOnly.find(config => config.id === 'opencode').code);
assert.equal(tokenRemote.mcp.servers.mcp.oauth, false);
assert.equal(tokenRemote.mcp.servers.mcp.url, endpoint);
assert.ok(!Object.hasOwn(tokenRemote.mcp.servers.mcp, 'headers'));
const headerOnly = agentConfigurations({endpoint: 'https://example.com/mcp', headers});
assert.equal(JSON.parse(headerOnly.find(config => config.id === 'opencode').code)
  .mcp.servers.mcp.oauth, false);
""")


def test_user_configuration_paths_preserve_server_settings() -> None:
    run(r"""
const options = {endpoint: 'https://example.com/mcp?token=query', name: 'team',
  headers: {'Authorization': 'Bearer test'}};
const project = agentConfigurations({...options, level: 'project'});
const user = agentConfigurations({...options, level: 'user'});
assert.deepEqual(agentConfigurations(options), user);
assert.deepEqual(user.map(config => config.target), [
  '~/.claude.json', '~/.codex/config.toml', '~/.cursor/mcp.json',
  '~/.copilot/mcp-config.json', '~/.gemini/settings.json', '~/.pi/agent/mcp.json',
  '~/.gemini/config/mcp_config.json',
  '~/.config/opencode/opencode.json',
]);
for (let index = 0; index < user.length; index += 1) {
  assert.equal(user[index].code, project[index].code);
  assert.ok(user[index].instructions.includes(user[index].target));
  assert.match(user[index].instructions, /all your projects/);
  assert.match(project[index].instructions, /project root/);
  assert.match(user[index].instructions, /Merge/);
}
assert.equal(user[0].label, 'Claude Code');
assert.equal(project[0].label, 'Claude Code');
assert.match(user[0].link, /#user-scope$/);
assert.match(project[0].link, /#project-scope$/);
assert.match(project[1].verify, /Trust this project/);
assert.ok(!user[1].verify.includes('Trust this project'));
""")


def test_codex_toml_round_trip() -> None:
    tomllib = pytest.importorskip("tomllib")
    endpoint = 'https://example.com/mcp?name="quoted"&path=\\&token=a%2Bb'
    headers = {"Authorization": 'Bearer a"b\\c', "X-Label": "café 😀"}
    options = {"endpoint": endpoint, "name": "chat-test_1", "headers": headers}
    result = run(
        f"\nconst configs = agentConfigurations({json.dumps(options)});\n"
        "process.stdout.write(configs.find(config => config.id === 'codex').code);\n"
    )
    assert tomllib.loads(result) == {
        "mcp_servers": {"chat-test_1": {"url": endpoint, "http_headers": headers}},
    }


def test_resource_tools_configuration_and_opt_out() -> None:
    run(r"""
for (const endpoint of [
  'https://example.com/mcp',
  'https://example.com/mcp?',
  'https://example.com/mcp?resources=native&token=a%20b&resources=tool&x=%2f',
  'https://example.com/mcp?%72esources=native&token=a%2Bb',
]) {
  const configured = agentConfigurations({endpoint});
  const disabled = agentConfigurations({endpoint, resourceTools: false});
  for (const id of ['pi', 'antigravity']) {
    const readUrl = configs => {
      const server = JSON.parse(configs.find(config => config.id === id).code).mcpServers.mcp;
      return server.url || server.serverUrl;
    };
    const enabledUrl = new URL(readUrl(configured));
    assert.deepEqual(enabledUrl.searchParams.getAll('resources'), ['tool']);
    const original = new URL(endpoint);
    original.searchParams.delete('resources');
    enabledUrl.searchParams.delete('resources');
    assert.deepEqual([...enabledUrl.searchParams], [...original.searchParams]);
    assert.equal(readUrl(disabled), endpoint);
  }
  const cursor = JSON.parse(configured.find(config => config.id === 'cursor').code).mcpServers.mcp;
  assert.equal(new URL(cursor.url).searchParams.has('resources'), false);
  const disabledCursor = JSON.parse(disabled.find(config => config.id === 'cursor').code)
    .mcpServers.mcp;
  assert.equal(disabledCursor.url, endpoint);
}
""")


def test_explicit_resource_modes_apply_to_every_agent() -> None:
    run(r"""
const endpoint = 'https://example.com/mcp?token=a%20b&resources=native&resources=tool&x=%2f';
for (const resourceModes of [['tool'], ['native'], ['tool', 'native']]) {
  for (const config of agentConfigurations({endpoint, resourceModes})) {
    const expected = 'https://example.com/mcp?token=a%20b&x=%2f'
      + (resourceModes.includes('tool')
        ? '&' + resourceModes.map(mode => `resources=${mode}`).join('&') : '');
    if (config.language === 'toml') assert.ok(config.code.includes(JSON.stringify(expected)));
    else {
      const body = JSON.parse(config.code);
      const server = (body.mcpServers || body.mcp.servers).mcp;
      assert.equal(server.url || server.httpUrl || server.serverUrl, expected);
    }
  }
}
for (const resourceModes of [[], ['invalid'], ['tool', 'invalid'], 'tool']) {
  assert.throws(() => agentConfigurations({endpoint, resourceModes}), /resource interface/);
}
assert.doesNotThrow(() => agentConfigurations({endpoint, resourceModes: [], resourceTools: false}));
const native = agentConfigurations({endpoint: 'https://example.com/mcp?resources=tool&resources=native',
  resourceModes: ['native']});
assert.equal(JSON.parse(native[0].code).mcpServers.mcp.url, 'https://example.com/mcp');
""")


def test_agent_dialog_uses_declared_names_and_keeps_manual_overrides() -> None:
    run_dialog(r"""
endpointDeclarations = [
  {path: '/mcp', server_name: 'workspace_tools', query: {properties: {}}},
  {path: '/other', server_name: 'other_tools', query: {properties: {}}},
];
credentials.set('https://example.com/mcp?old=1', 'bearer', '', 'access-token', '');
page.endpoint.value = '/mcp?resources=tool';
openAgentSetup();
assert.equal(page.agentName.value, 'workspace_tools');
assert.equal(JSON.parse(page.agentCode.value).mcpServers.workspace_tools.headers.Authorization,
  'Bearer access-token');
page.agentName.value = 'personal_name';
page.agentName.oninput();
page.endpoint.value = '/mcp?different=2';
openAgentSetup();
assert.equal(page.agentName.value, 'personal_name');
page.endpoint.value = '/other';
openAgentSetup();
assert.equal(page.agentName.value, 'other_tools');
assert.ok(!JSON.parse(page.agentCode.value).mcpServers.other_tools.headers);
page.endpoint.value = '/mcp';
openAgentSetup();
assert.equal(page.agentName.value, 'personal_name');
page.endpoint.value = '/undeclared';
openAgentSetup();
assert.equal(page.agentName.value, 'workspace_console');
""")


def test_agent_dialog_resource_defaults_and_independent_overrides() -> None:
    run_dialog(r"""
openAgentSetup();
assert.equal(page.agentResourcesTool.checked, false);
assert.equal(page.agentResourcesNative.checked, true);
for (const agent of ['copilot', 'pi', 'antigravity']) {
  page.agentMethod.value = agent;
  page.agentMethod.onchange();
  assert.equal(page.agentResourcesTool.checked, true);
  assert.equal(page.agentResourcesNative.checked, false);
}
page.agentResourcesNative.checked = true;
page.agentResourcesNative.onchange();
const modes = () => {
  const server = JSON.parse(page.agentCode.value).mcpServers[page.agentName.value];
  return new URL(server.serverUrl).searchParams.getAll('resources');
};
assert.deepEqual(modes(), ['tool', 'native']);
page.agentLevel.value = 'project';
page.agentLevel.onchange();
page.agentName.value = 'personal';
page.agentName.oninput();
page.agentHeaders.checked = false;
page.agentHeaders.onchange();
assert.deepEqual(modes(), ['tool', 'native']);
page.agentResourcesTool.checked = false;
page.agentResourcesNative.checked = false;
page.agentResourcesTool.onchange();
assert.equal(page.agentCode.value, '');
assert.equal(page.agentCopy.disabled, true);
assert.match(page.agentError.textContent, /at least one/);
page.agentMethod.value = 'cursor';
page.agentMethod.onchange();
assert.equal(page.agentResourcesTool.checked, false);
assert.equal(page.agentResourcesNative.checked, true);
assert.equal(page.agentCopy.disabled, false);
assert.equal(JSON.parse(page.agentCode.value).mcpServers.personal.url, 'https://example.com/mcp');
document.documentElement.dataset.resourceTools = 'false';
page.endpoint.value = '/mcp?token=a%20b&resources=custom';
openAgentSetup();
assert.equal(page.agentResources.hidden, true);
assert.equal(JSON.parse(page.agentCode.value).mcpServers.personal.url,
  'https://example.com/mcp?token=a%20b&resources=custom');
""")


def test_invalid_configuration_inputs() -> None:
    run(r"""
const endpoint = 'https://example.com/mcp';
for (const level of ['', 'local', 'global', null, 1]) {
  assert.throws(() => agentConfigurations({endpoint, level}), /configuration level/);
}
for (const name of ['', 'a b', 'a.b', 'a"b', 'a/b', 'a'.repeat(65), null, 1]) {
  assert.throws(() => agentConfigurations({endpoint, name}), /server name/);
}
assert.equal(agentConfigurations({endpoint, name: 'a'.repeat(64)}).length, 8);
for (const value of [undefined, '', '/mcp', 'ftp://example.com', 'javascript:alert(1)',
  'http:example.com', 'https://', 'https://user:pass@example.com/mcp',
  'https://user@example.com/mcp', endpoint + '#', endpoint + '#fragment',
  endpoint + '\n', endpoint + '\u0000', ' ' + endpoint, endpoint + '\ud800']) {
  assert.throws(() => agentConfigurations({endpoint: value}), /endpoint/);
}
for (const headers of [null, [], 'value', {'bad header': 'x'}, {'': 'x'},
  {'X-Test': 'a\r\nb'}, {'X-Test': 'a\tb'}, {'X-Test': '\u007f'},
  {'X-Test': '\ud800'}, {'X-Test': 1}, {'X-Test': null}]) {
  assert.throws(() => agentConfigurations({endpoint, headers}), /[Hh]eader/);
}
assert.throws(() => agentConfigurations(), /endpoint/);
""")

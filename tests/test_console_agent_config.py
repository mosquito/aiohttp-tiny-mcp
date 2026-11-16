"""Agent configuration snippets preserve endpoint settings and validate inputs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SOURCE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/agent-config.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(body: str) -> str:
    driver = 'const assert = require("node:assert/strict");\n' + SOURCE.read_text() + body
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_agent_schemas() -> None:
    run(r"""
const endpoint = 'https://example.com/mcp?org=one&user=alice';
const configs = agentConfigurations({endpoint, level: 'project'});
assert.deepEqual(configs.map(config => config.id), [
  'mcp-json', 'codex', 'cursor', 'copilot', 'gemini', 'pi', 'opencode',
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
assert.equal(byId['opencode'].target, 'opencode.json');
assert.deepEqual(JSON.parse(byId['mcp-json'].code), {
  mcpServers: {mcp: {type: 'http', url: endpoint}},
});
assert.deepEqual(JSON.parse(byId.cursor.code), {mcpServers: {mcp: {url: endpoint}}});
assert.deepEqual(JSON.parse(byId.copilot.code), {
  mcpServers: {mcp: {type: 'http', url: endpoint, tools: ['*']}},
});
assert.deepEqual(JSON.parse(byId.gemini.code), {mcpServers: {mcp: {httpUrl: endpoint}}});
assert.deepEqual(JSON.parse(byId.pi.code), {mcpServers: {mcp: {url: endpoint}}});
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
  assert.equal(server.url || server.httpUrl, endpoint);
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


def test_invalid_configuration_inputs() -> None:
    run(r"""
const endpoint = 'https://example.com/mcp';
for (const level of ['', 'local', 'global', null, 1]) {
  assert.throws(() => agentConfigurations({endpoint, level}), /configuration level/);
}
for (const name of ['', 'a b', 'a.b', 'a"b', 'a/b', 'a'.repeat(65), null, 1]) {
  assert.throws(() => agentConfigurations({endpoint, name}), /server name/);
}
assert.equal(agentConfigurations({endpoint, name: 'a'.repeat(64)}).length, 7);
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

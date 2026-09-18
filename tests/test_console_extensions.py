"""Run the browser client's extension discovery and file verification in Node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/console.js"
pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is not installed"),
    pytest.mark.timeout(10),
]


def run(body):
    source = CONSOLE.read_text()
    client = source[: source.index("const page =")]
    verify = source[
        source.index("async function verifiedSkillFile") : source.index("function reportSkill")
    ]
    driver = (
        'const assert = require("node:assert/strict");\n'
        + client
        + verify
        + "\n(async () => {\n"
        + body
        + "\n})().catch(error => { console.error(error); process.exit(1); });"
    )
    subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)


def test_skills_discovery_paging_and_modern_headers():
    run("""
    const sent = [];
    const skill = {uri: "skill://deploy/SKILL.md", frontmatter: {name: "deploy"}, resources: []};
    globalThis.fetch = async (url, options) => {
      const body = JSON.parse(options.body);
      sent.push(body);
      assert.equal(options.headers["Mcp-Method"], body.method);
      assert.equal(options.headers["MCP-Protocol-Version"], "2026-07-28");
      assert.equal(body.params._meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28");
      let result;
      if (body.method === "server/discover") {
        result = {capabilities: {extensions: {[SKILLS_EXTENSION]: {}}}};
      }
      else if (body.method === "skills/list") result = body.params.cursor
        ? {skills: [{...skill, uri: "skill://second/SKILL.md"}]}
        : {skills: [skill], nextCursor: "page-two"};
      else if (body.method === "skills/get") result = {skill};
      else throw new Error(body.method);
      return {headers: new Headers({"Content-Type": "application/json"}),
        text: async () => JSON.stringify({jsonrpc: "2.0", id: body.id, result})};
    };
    const client = new Client("http://localhost/mcp", "2026-07-28", {onFrame() {}});
    await client.initialize();
    assert.deepEqual(Object.keys(client.extensions), [SKILLS_EXTENSION]);
    assert.equal((await client.listSkills()).length, 2);
    assert.deepEqual(await client.getSkill(skill.uri), skill);
    assert.deepEqual(sent.map(item => item.method),
      ["server/discover", "skills/list", "skills/list", "skills/get"]);
    """)


def test_absent_or_legacy_extensions_never_call_skills_methods():
    run("""
    for (const version of Object.keys(REVISIONS)) {
      const client = new Client("http://localhost/mcp", version, {});
      client.request = async () => { throw new Error("must not make a request"); };
      assert.deepEqual(await client.listSkills(), []);
      await assert.rejects(client.getSkill("skill://deploy/SKILL.md"), /not declared/);
      if (version !== "2026-07-28") {
        client.serverCapabilities = {extensions: {[SKILLS_EXTENSION]: {}}};
        assert.deepEqual(client.extensions, {});
        assert.deepEqual(await client.listSkills(), []);
      }
    }
    """)


def test_repeated_pagination_cursor_is_reported():
    run("""
    const client = new Client("http://localhost/mcp", "2026-07-28", {});
    client.serverCapabilities = {extensions: {[SKILLS_EXTENSION]: {}}};
    client.request = async () => ({skills: [], nextCursor: "same"});
    await assert.rejects(client.listSkills(), /repeated cursor/);
    """)


def test_skill_text_and_binary_verification_reject_changed_content():
    run("""
    for (const bytes of [Buffer.from("Deploy ✓\\r\\n"), Buffer.from([0, 255, 128])]) {
      const uri = "skill://deploy/file";
      const hash = require("node:crypto").createHash("sha256").update(bytes).digest("hex");
      const digest = "sha256:" + hash;
      const file = {uri, digest, size: bytes.length};
      const content = bytes[0] === 0
        ? {blob: bytes.toString("base64")} : {text: bytes.toString("utf8")};
      const result = {contents: [{uri, ...content}]};
      assert.equal(await verifiedSkillFile(result, file), result);
      await assert.rejects(verifiedSkillFile(result, {...file, size: file.size + 1}), /size/);
      await assert.rejects(
        verifiedSkillFile(result, {...file, digest: "sha256:" + "0".repeat(64)}), /digest/);
      await assert.rejects(
        verifiedSkillFile(result, {...file, uri: "skill://other/file"}), /requested file/);
    }
    """)

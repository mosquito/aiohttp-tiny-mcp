"""Tool selection survives reloads through the page fragment."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/console.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.mark.parametrize("name", ["search", "a/b #тест%", None])
def test_tool_fragment_round_trip(name):
    source = CONSOLE.read_text()
    functions = source[source.index("function restoreTool(") : source.index("function choose(")]
    driver = (
        functions
        + f"""
let location = new URL("http://localhost/console?mode=debug#old");
const history = {{state: {{keep: true}}, replaceState(state, unused, url) {{ location = url; }} }};
rememberTool({json.dumps(name)});
let picked = null;
const tools = [{{name: "other"}}, {{name: {json.dumps(name)}}}];
restoreTool(tools, [
  {{click() {{ picked = "other"; }} }},
  {{click() {{ picked = {json.dumps(name)}; }} }}
]);
process.stdout.write(JSON.stringify({{
  picked, hash: location.hash, path: location.pathname, query: location.search
}}));
"""
    )
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value["picked"] == name
    assert value["path"] == "/console"
    assert value["query"] == "?mode=debug"
    assert bool(value["hash"]) == (name is not None)


@pytest.mark.parametrize("fragment", ["#missing", "#%broken", ""])
def test_unavailable_or_invalid_tool_is_ignored(fragment):
    source = CONSOLE.read_text()
    functions = source[
        source.index("function restoreTool(") : source.index("function rememberTool(")
    ]
    driver = (
        functions
        + f"""
const location = {{hash: {json.dumps(fragment)}}};
restoreTool([{{name: "search"}}], [{{click() {{ throw Error("unexpected selection"); }} }}]);
"""
    )
    subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)


@pytest.mark.parametrize("restore", [True, False])
def test_server_instructions_do_not_disable_restored_tool(restore):
    source = CONSOLE.read_text()
    connect = source[
        source.index("async function connect()") : source.index("function connectionHelp(")
    ]
    invoke = source[source.index("async function invoke()") : source.index("function disconnect()")]
    driver = (
        connect
        + invoke
        + f"""
const page = Object.fromEntries([
  "connect", "authenticate", "revision", "answerable", "endpoint", "outcome",
  "refresh", "subject", "about", "invoke", "args", "serverTitle"
].map(name => [name, {{disabled: true}}]));
let client, endpointUrl, chosen = null, called = null, serverInstructions = "", catalogue = null;
let remembered = "search";
page.catalogue = {{querySelectorAll() {{ return []; }} }};
function rememberTool(name) {{ remembered = name; }}
const credentials = {{forEndpoint() {{ return {{}}; }} }};
function chosenEndpoint() {{ return new URL("http://localhost/mcp"); }}
function clearCredentials() {{}}
function openAuthentication() {{}}
function record() {{}}
function noticed() {{}}
function askPerson() {{}}
function say() {{}}
function prose(text, into) {{ into.textContent = text; }}
function report() {{}}
function reportValue() {{}}
function resultViews() {{ return {{text: "ok"}}; }}
function missing() {{ return []; }}
function readFields() {{ return {{query: "test"}}; }}
class Client {{
  async initialize() {{ return {{instructions: "Server instructions"}}; }}
  async callTool(name, values) {{ called = {{name, values}}; return {{content: []}}; }}
}}
async function loadCatalogue() {{
  if ({json.dumps(restore)}) {{
    chosen = {{kind: "tool", item: {{name: "search"}}, fields: []}};
    page.subject.textContent = "search";
    page.invoke.disabled = false;
  }}
}}
(async () => {{
  await connect();
  if (!page.invoke.disabled) await invoke();
  const initial = {{disabled: page.invoke.disabled, subject: page.subject.textContent, called}};
  showServerInstructions();
  const instructions = {{
    subject: page.subject.textContent, text: page.about.textContent,
    disabled: page.invoke.disabled, chosen, remembered,
    titleEnabled: !page.serverTitle.disabled
  }};
  setServerInstructions("");
  process.stdout.write(JSON.stringify({{...initial, instructions,
    titleDisabled: page.serverTitle.disabled
  }}));
}})();
"""
    )
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, check=True)
    state = json.loads(result.stdout)
    assert state["disabled"] is not restore
    assert state["subject"] == ("search" if restore else "Instructions")
    assert state["called"] == ({"name": "search", "values": {"query": "test"}} if restore else None)

    assert state["instructions"] == {
        "subject": "Instructions",
        "text": "Server instructions",
        "disabled": True,
        "chosen": None,
        "remembered": None,
        "titleEnabled": True,
    }
    assert state["titleDisabled"] is True

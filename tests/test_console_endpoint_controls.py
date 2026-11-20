"""Declared console endpoints build URLs from their query controls."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CONSOLE = Path(__file__).parent.parent / "src/aiohttp_tiny_mcp/console/console.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DOM = r"""
function element(tag, className, text) {
  return {
    tag, className, dataset: {}, children: [], checked: false, disabled: false,
    hidden: false, storedValue: undefined, storedText: text || '', listeners: {},
    validity: {badInput: false},
    get valueAsNumber() { return this.value === '' ? NaN : Number(this.value); },
    get options() { return this.children; },
    get textContent() { return this.storedText; },
    set textContent(value) { this.storedText = String(value); this.children = []; },
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    checkValidity() { return true; },
    reportValidity() { return true; },
    focus() {},
    set value(value) { this.storedValue = String(value); },
    get value() {
      if (this.tag === 'select') {
        if (this.storedValue === undefined) return this.children[0]?.value || '';
        return this.children.some(child => child.value === this.storedValue)
          ? this.storedValue : '';
      }
      return this.storedValue ?? (this.tag === 'option' ? this.storedText : '');
    }
  };
}
const page = {
  endpoint: element('input'), endpointSelect: element('select'),
  endpointDescription: element('p'), endpointParameters: element('fieldset'),
  endpointManual: element('label'), endpointDeclared: element('section'),
  authenticate: element('button'),
};
page.endpointDeclared.hidden = true;
const document = {documentElement: {dataset: {endpoints: '[]'}}};
const location = new URL('http://localhost/console');
const stored = new Map();
const localStorage = {
  getItem: key => stored.get(key) ?? null,
  setItem: (key, value) => stored.set(key, String(value)),
  removeItem: key => stored.delete(key),
};
let configuredEndpoint = new URL('/mcp', location.origin);
let endpointDeclarations = [], endpointFields = [];
let client = null;
const credentials = new ConsoleCredentials();
let cleared = 0, oauthResets = 0, authEndpoint = 'previous', authSchemes = ['bearer'];
function clearCredentials() { cleared++; credentials.clear(); }
function resetOAuth() { oauthResets++; }
function field(name) { return endpointFields.find(input => input.dataset.name === name); }
function declare(endpoints) {
  document.documentElement.dataset.endpoints = JSON.stringify(endpoints);
  setupEndpoints();
  if (endpoints.length) {
    page.endpointSelect.value = '0';
    updateDeclaredEndpoint();
  }
}
"""


def run(body: str) -> None:
    source = CONSOLE.read_text()
    endpoints = source[
        source.index("function endpointChanged(") : source.index("function element(")
    ]
    fields = source[source.index("function buildFields(") : source.index("function show(")]
    credentials = source[source.index("class ConsoleCredentials") : source.index("class Client")]
    driver = (
        'const assert = require("node:assert/strict");\n'
        + credentials
        + DOM
        + endpoints
        + fields
        + body
    )
    result = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_declared_endpoint_defaults_descriptions_and_selection() -> None:
    run(r"""
    declare([
      {path: '/mcp/team', description: '<script>literal description</script>', query: {
        type: 'object', properties: {org: {type: 'string', default: 'example.com'}}}},
      {path: '/mcp/admin', description: 'Administration', query: {
        type: 'object', properties: {scope: {type: 'string', default: 'read'}}}},
    ]);
    assert.equal(page.endpointManual.hidden, false);
    assert.equal(page.endpointDeclared.hidden, false);
    assert.equal(page.endpointSelect.options.length, 3);
    assert.ok(page.endpointSelect.options.some(option => option.value === ''));
    assert.equal(page.endpointDescription.textContent, '<script>literal description</script>');
    assert.equal(page.endpointDescription.children.length, 0);
    assert.equal(field('org').value, 'example.com');
    assert.equal(declaredEndpoint().href, 'http://localhost/mcp/team?org=example.com');
    page.endpointSelect.value = '1';
    page.endpointSelect.onchange();
    assert.equal(cleared, 0);
    assert.equal(page.endpointDescription.textContent, 'Administration');
    assert.equal(field('org'), undefined);
    assert.equal(declaredEndpoint().href, 'http://localhost/mcp/admin?scope=read');
    """)


def test_query_values_are_encoded_and_arrays_use_repeated_keys() -> None:
    run(r"""
    declare([{path: '/mcp?fixed=yes', description: 'Query values', query: {
      type: 'object', properties: {
        text: {type: 'string'}, count: {type: 'integer'}, enabled: {type: 'boolean'},
        tags: {type: 'array', items: {type: 'string'}},
      }, required: ['text', 'count', 'enabled'],
    }}]);
    field('text').value = '  team & + = / # Привет  ';
    field('count').value = '0';
    field('enabled').checked = false;
    field('tags').value = JSON.stringify(['one&two', 'x+y', 'two words']);
    const url = declaredEndpoint();
    assert.equal(url.pathname, '/mcp');
    assert.equal(url.searchParams.get('fixed'), 'yes');
    assert.equal(url.searchParams.get('text'), '  team & + = / # Привет  ');
    assert.equal(url.searchParams.get('count'), '0');
    assert.equal(url.searchParams.get('enabled'), 'false');
    assert.deepEqual(url.searchParams.getAll('tags'), ['one&two', 'x+y', 'two words']);
    assert.equal(url.hash, '');
    assert.equal(url.searchParams.size, 7);
    """)


def test_optional_blank_and_null_values_are_omitted() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Optional values', query: {
      type: 'object', properties: {
        absent: {type: 'string'},
        nullable: {anyOf: [{type: 'string'}, {type: 'null'}], default: null},
        tags: {type: 'array', items: {type: 'string'}},
      },
    }}]);
    assert.equal(declaredEndpoint().search, '');
    field('nullable').nullToggle.checked = false;
    field('nullable').nullToggle.onchange();
    field('nullable').value = 'present';
    assert.equal(declaredEndpoint().searchParams.get('nullable'), 'present');
    """)


def test_omitted_query_fields_remove_matching_fixed_parameters() -> None:
    run(r"""
    declare([{path: '/mcp?keep=1&optional=old&token=old&nullable=old',
      description: 'Optional overrides', query: {
        type: 'object', properties: {
          optional: {type: 'string'}, token: {type: 'string'},
          nullable: {anyOf: [{type: 'string'}, {type: 'null'}], default: null},
        },
      },
    }]);
    assert.equal(declaredEndpoint().search, '?keep=1');
    """)


def test_required_blank_and_null_values_block_connection() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Required values', query: {
      type: 'object', properties: {
        org: {type: 'string'},
        user: {anyOf: [{type: 'string'}, {type: 'null'}], default: null},
      }, required: ['org', 'user'],
    }}]);
    assert.throws(() => declaredEndpoint());
    field('org').value = 'example.com';
    assert.throws(() => declaredEndpoint());
    field('user').nullToggle.checked = false;
    field('user').nullToggle.onchange();
    field('user').value = 'alice';
    assert.equal(declaredEndpoint().searchParams.get('user'), 'alice');
    """)


def test_query_token_is_masked_and_manual_url_persists_it() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Token', query: {
      type: 'object', properties: {token: {type: 'string'}},
    }}]);
    field('token').value = 'private-query-token';
    assert.equal(field('token').type, 'password');
    assert.equal(field('token').autocomplete, 'off');
    assert.equal(declaredEndpoint().searchParams.get('token'), 'private-query-token');
    page.endpoint.value = declaredEndpoint().pathname + declaredEndpoint().search;
    assert.equal(chosenEndpoint().searchParams.get('token'), 'private-query-token');
    assert.ok(JSON.stringify([...stored]).includes('private-query-token'));
    """)


def test_manual_endpoint_changes_clear_authentication_but_dialog_controls_stay_enabled() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Connection', query: {
      type: 'object', properties: {},
    }}]);
    cleared = 0;
    oauthResets = 0;
    endpointChanged();
    assert.equal(cleared, 1);
    assert.equal(oauthResets, 1);
    assert.equal(authEndpoint, null);
    assert.deepEqual(authSchemes, []);
    for (const disabled of [true, false]) {
      setEndpointDisabled(disabled);
      assert.equal(page.endpoint.disabled, disabled);
      assert.equal(page.endpointSelect.disabled, false);
      assert.equal(page.endpointParameters.disabled, false);
    }
    """)


def test_query_edits_keep_runtime_credentials_and_oauth_state() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Tools', query: {
      type: 'object', properties: {team: {type: 'string'}},
    }}]);
    credentials.set('http://localhost/mcp?team=one', 'bearer', '', 'access-token', '');
    authEndpoint = 'http://localhost/mcp';
    authSchemes = ['bearer'];
    page.endpoint.value = '/mcp?team=two&resources=tool&token=query-token';
    endpointChanged();
    assert.equal(cleared, 0);
    assert.equal(oauthResets, 0);
    assert.deepEqual(authSchemes, ['bearer']);
    assert.equal(credentials.forEndpoint(chosenEndpoint().href).Authorization,
      'Bearer access-token');
    assert.ok(stored.get(endpointKey()).includes('token=query-token'));
    field('team').value = 'three';
    page.endpointParameters.listeners.input();
    assert.equal(oauthResets, 0);
    assert.equal(credentials.forEndpoint(authenticationEndpoint().href).Authorization,
      'Bearer access-token');
    page.endpoint.value = '/other?team=two';
    endpointChanged();
    assert.equal(cleared, 1);
    assert.equal(oauthResets, 1);
    assert.deepEqual(credentials.forEndpoint('http://localhost/other'), {});
    page.endpoint.value = '/mcp?team=four';
    endpointChanged();
    assert.equal(credentials.forEndpoint(chosenEndpoint().href).Authorization,
      'Bearer access-token');
    """)


def test_query_enums_keep_scalar_types_and_array_choices_repeat() -> None:
    run(r"""
    declare([{path: '/mcp?mode=old', description: 'Choices', query: {
      type: 'object', properties: {
        mode: {enum: [0, false, 'read'], default: false},
        tags: {type: 'array', items: {enum: ['red', 'green']}, default: ['green']},
      },
    }}]);
    assert.deepEqual(declaredEndpoint().searchParams.getAll('mode'), ['false']);
    assert.deepEqual(declaredEndpoint().searchParams.getAll('tags'), ['green']);
    field('mode').value = '0';
    field('tags').choiceInputs[0].checked = true;
    assert.equal(declaredEndpoint().searchParams.get('mode'), '0');
    assert.deepEqual(declaredEndpoint().searchParams.getAll('tags'), ['red', 'green']);
    """)


def test_required_arrays_and_nonscalar_items_are_rejected() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Array validation', query: {
      type: 'object', properties: {tags: {type: 'array', items: {type: 'string'}}},
      required: ['tags'],
    }}]);
    field('tags').value = '[]';
    assert.throws(() => declaredEndpoint(), /tags/);
    field('tags').value = '[{"nested":"value"}]';
    assert.throws(() => declaredEndpoint(), /scalar/);
    field('tags').value = '["valid"]';
    assert.deepEqual(declaredEndpoint().searchParams.getAll('tags'), ['valid']);
    """)


def test_without_declarations_manual_urls_and_storage_still_work() -> None:
    run(r"""
    declare([]);
    assert.equal(page.endpointManual.hidden, false);
    assert.equal(page.endpointDeclared.hidden, true);
    page.endpoint.value = '/mcp?org=example.com#discard';
    assert.equal(chosenEndpoint().href, 'http://localhost/mcp?org=example.com');
    assert.equal(stored.get(endpointKey()), '/mcp?org=example.com');
    assert.equal(rememberedEndpoint(), '/mcp?org=example.com');
    page.endpoint.value = 'https://other.example/mcp';
    assert.throws(() => chosenEndpoint(), /this server/);
    page.endpoint.value = 'http://user:password@localhost/mcp';
    assert.throws(() => chosenEndpoint(), /Authentication/);
    """)


def test_manual_url_is_not_limited_by_declared_paths_or_query_fields() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Declared endpoint', query: {
      type: 'object', properties: {org: {type: 'string'}}, required: ['org'],
    }}]);
    assert.throws(() => declaredEndpoint(), /org/);
    page.endpoint.value = '/custom-mcp?undeclared=a%26b&mode=extra';
    const url = chosenEndpoint();
    assert.equal(url.pathname, '/custom-mcp');
    assert.equal(url.searchParams.get('undeclared'), 'a&b');
    assert.equal(url.searchParams.get('mode'), 'extra');
    assert.equal(page.endpoint.value, '/custom-mcp?undeclared=a%26b&mode=extra');
    """)


def test_url_prefills_modal_draft_and_retains_extra_parameters() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Query draft', query: {
      type: 'object', properties: {
        org: {type: 'string', default: 'default.example'}, count: {type: 'integer'},
        enabled: {type: 'boolean'}, tags: {type: 'array', items: {type: 'string'}},
        token: {anyOf: [{type: 'string'}, {type: 'null'}], default: null},
      },
    }}]);
    const original = '/mcp?org=actual.example&count=0&enabled=false&tags=one&tags=two'
      + '&token=existing-token&extra=a%26b';
    page.endpoint.value = original;
    updateDeclaredEndpoint(new URL(original, location.origin));
    assert.equal(field('org').value, 'actual.example');
    assert.equal(field('count').value, '0');
    assert.equal(field('enabled').checked, false);
    assert.deepEqual(JSON.parse(field('tags').value), ['one', 'two']);
    assert.equal(field('token').value, 'existing-token');
    assert.equal(field('token').nullToggle.checked, false);
    assert.equal(field('token').disabled, false);
    field('org').value = 'draft.example';
    assert.equal(page.endpoint.value, original);
    const draft = declaredEndpoint();
    assert.equal(draft.searchParams.get('org'), 'draft.example');
    assert.equal(draft.searchParams.get('extra'), 'a&b');
    """)


def test_custom_dialog_selection_uses_manual_url() -> None:
    run(r"""
    declare([{path: '/mcp', description: 'Declared endpoint', query: {
      type: 'object', properties: {org: {type: 'string'}}, required: ['org'],
    }}]);
    page.endpoint.value = '/custom?raw=1';
    page.endpointSelect.value = '';
    updateDeclaredEndpoint();
    assert.equal(selectedDeclaration(), null);
    assert.equal(endpointFields.length, 0);
    assert.equal(authenticationEndpoint().href, 'http://localhost/custom?raw=1');
    """)

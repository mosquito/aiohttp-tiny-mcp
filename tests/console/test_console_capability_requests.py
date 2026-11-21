import pytest


@pytest.mark.parametrize(
    "version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
)
def test_absent_capabilities_skip_native_catalogue_requests(run_js, version):
    run_js(
        f"const version = {version!r};"
        + """
    const client = new Client('http://localhost/mcp?resources=tool', version, {});
    const sent = [];
    client.request = async method => {
      sent.push(method);
      if (method === client.rules.describe) return {capabilities: {tools: {}}};
      if (method === 'tools/list') return {tools: [{name: 'mcp_resources_read'}]};
      throw new Error(`Unexpected request: ${method}`);
    };
    client.notify = async () => {};
    await client.initialize();
    client.resourceUris.add('config://stale');
    client.readUris.add('config://stale-template/1');
    assert.deepEqual(await client.listTools(), [{name: 'mcp_resources_read'}]);
    assert.deepEqual(await client.listResources(), {resources: [], templates: []});
    assert.deepEqual(await client.listPrompts(), []);
    assert.equal(client.resourceUris.size, 0);
    assert.equal(client.readUris.size, 0);
    assert.deepEqual(client.notificationSelection(), {});
    await assert.rejects(client.readResource('config://stale'), /not declared/);
    await assert.rejects(client.getPrompt('greet', {}), /not declared/);
    assert.deepEqual(sent, [client.rules.describe, 'tools/list']);
    client.serverCapabilities = {};
    assert.deepEqual(await client.listTools(), []);
    assert.equal(client.tools.size, 0);
    await assert.rejects(client.callTool('mcp_resources_read', {}), /not declared/);
    assert.deepEqual(sent, [client.rules.describe, 'tools/list']);
    """
    )


def test_empty_resource_capability_enables_resources_and_templates(run_js):
    run_js("""
    const client = new Client('http://localhost/mcp', '2026-07-28', {});
    client.serverCapabilities = {resources: {}};
    const sent = [];
    client.request = async (method, params) => {
      sent.push(method);
      if (method === 'resources/list') return {resources: [{uri: 'config://app'}]};
      if (method === 'resources/templates/list') return {
        resourceTemplates: [{uriTemplate: 'config://items/{id}'}]};
      if (method === 'resources/read') return {contents: [{uri: params.uri, text: 'ok'}]};
      throw new Error(`Unexpected request: ${method}`);
    };
    assert.deepEqual(await client.listResources(), {
      resources: [{uri: 'config://app'}],
      templates: [{uriTemplate: 'config://items/{id}'}],
    });
    assert.deepEqual(await client.readResource('config://items/1'), {
      contents: [{uri: 'config://items/1', text: 'ok'}],
    });
    assert.deepEqual(sent, ['resources/list', 'resources/templates/list', 'resources/read']);
    assert.deepEqual(client.notificationSelection(), {});
    """)


@pytest.mark.parametrize("kind", ["tools", "prompts"])
def test_empty_catalogue_capability_allows_only_its_methods(run_js, kind):
    run_js(
        f"const kind = {kind!r};"
        + """
    for (const version of Object.keys(REVISIONS)) {
      const client = new Client('http://localhost/mcp', version, {});
      client.serverCapabilities = {[kind]: {}};
      const sent = [];
      client.request = async method => {
        sent.push(method);
        if (method === 'tools/list') return {tools: [{name: 'echo'}]};
        if (method === 'prompts/list') return {prompts: [{name: 'greet'}]};
        if (method === 'tools/call') return {content: [{type: 'text', text: 'ok'}]};
        if (method === 'prompts/get') return {messages: []};
        throw new Error(`Unexpected request: ${method}`);
      };
      assert.deepEqual(await client.listTools(), kind === 'tools' ? [{name: 'echo'}] : []);
      assert.deepEqual(await client.listPrompts(), kind === 'prompts' ? [{name: 'greet'}] : []);
      assert.deepEqual(await client.listResources(), {resources: [], templates: []});
      if (kind === 'tools') {
        assert.deepEqual(await client.callTool('echo', {}), {
          content: [{type: 'text', text: 'ok'}],
        });
        assert.deepEqual(sent, ['tools/list', 'tools/call']);
      } else {
        assert.deepEqual(await client.getPrompt('greet', {}), {messages: []});
        assert.deepEqual(sent, ['prompts/list', 'prompts/get']);
      }
    }
    """
    )

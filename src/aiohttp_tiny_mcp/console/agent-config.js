"use strict";

function agentConfigurations({endpoint, name = 'mcp', headers = {}} = {}) {
  if (typeof name !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
    throw new Error('Use 1–64 letters, numbers, underscores, or hyphens for the server name.');
  }
  const controls = /[\x00-\x1f\x7f]/;
  const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?:^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  if (typeof endpoint !== 'string' || controls.test(endpoint) || loneSurrogate.test(endpoint)
      || endpoint !== endpoint.trim() || !/^https?:\/\//i.test(endpoint)) {
    throw new Error('Enter an absolute HTTP or HTTPS endpoint URL.');
  }
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    throw new Error('Enter a valid endpoint URL.');
  }
  if (url.username || url.password || endpoint.includes('#')) {
    throw new Error('The endpoint URL must not contain credentials or a fragment.');
  }
  if (!headers || typeof headers !== 'object' || Array.isArray(headers)) {
    throw new Error('Headers must be an object.');
  }
  const entries = Object.entries(headers);
  for (const [key, value] of entries) {
    if (!/^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/.test(key)
        || typeof value !== 'string' || controls.test(value) || loneSurrogate.test(value)) {
      throw new Error('Use valid HTTP header names and values without control characters.');
    }
  }
  const credentials = entries.length ? {headers: Object.fromEntries(entries)} : {};
  const server = {url: endpoint, ...credentials};
  const json = value => JSON.stringify(value, null, 2);
  const named = value => ({mcpServers: {[name]: value}});
  const merge = 'Merge this server entry into the file. Keep existing settings.';
  const signIn = entries.length || url.searchParams.has('token')
    ? '' : ' If the server requires OAuth, sign in from the agent.';
  const tomlHeaders = entries.length
    ? '\nhttp_headers = { ' + entries.map(([key, value]) =>
      `${JSON.stringify(key)} = ${JSON.stringify(value)}`).join(', ') + ' }' : '';
  const remote = {type: 'remote', ...server};
  if (entries.length || url.searchParams.has('token')) remote.oauth = false;
  return [
    {
      id: 'mcp-json', label: '.mcp.json · Claude Code', target: '.mcp.json',
      instructions: 'Create this file in the project root. ' + merge,
      code: json(named({type: 'http', ...server})), language: 'json',
      verify: 'Start Claude Code in this project, approve the server, and open /mcp.' + signIn,
      link: 'https://code.claude.com/docs/en/mcp#project-scope',
    },
    {
      id: 'codex', label: 'Codex', target: '~/.codex/config.toml',
      instructions: merge + ' For this project only, use .codex/config.toml in a trusted project.',
      code: `[mcp_servers.${JSON.stringify(name)}]\nurl = ${JSON.stringify(endpoint)}${tomlHeaders}\n`,
      language: 'toml',
      verify: 'Run codex mcp list to check the server.'
        + (signIn ? ` For OAuth, run codex mcp login ${name}.` : ''),
      link: 'https://learn.chatgpt.com/docs/extend/mcp?surface=cli',
    },
    {
      id: 'cursor', label: 'Cursor', target: '.cursor/mcp.json',
      instructions: 'Create this file in the project. ' + merge,
      code: json(named(server)), language: 'json',
      verify: 'Open Cursor Settings → Tools & MCP and check the server.' + signIn,
      link: 'https://cursor.com/docs/mcp',
    },
    {
      id: 'copilot', label: 'GitHub Copilot', target: '.mcp.json',
      instructions: 'Create this file in the project root. ' + merge,
      code: json(named({type: 'http', ...server, tools: ['*']})), language: 'json',
      verify: 'In VS Code, run MCP: List Servers and start the server.' + signIn,
      link: 'https://code.visualstudio.com/docs/agents/reference/mcp-configuration',
    },
    {
      id: 'gemini', label: 'Gemini CLI', target: '.gemini/settings.json',
      instructions: 'Create this file in the project. ' + merge,
      code: json(named({httpUrl: endpoint, ...credentials})), language: 'json',
      verify: 'Start Gemini CLI in this project and open /mcp.' + signIn,
      link: 'https://geminicli.com/docs/tools/mcp-server/',
    },
    {
      id: 'pi', label: 'Pi', target: '.pi/mcp.json',
      instructions: 'Create this file in the project. ' + merge,
      code: json(named(server)), language: 'json',
      verify: 'Start Pi in this project and check the MCP server status.' + signIn,
      link: 'https://docs.pi.dev/latest/mcp',
    },
    {
      id: 'opencode', label: 'OpenCode V2', target: 'opencode.json',
      instructions: 'Create this file in the project root. ' + merge,
      code: json({$schema: 'https://opencode.ai/config.json', mcp: {servers: {[name]: remote}}}),
      language: 'json',
      verify: 'Run opencode mcp list to check the server.' + signIn,
      link: 'https://opencode.ai/docs/mcp-servers/',
    },
  ];
}

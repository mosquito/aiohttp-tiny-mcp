"use strict";

function agentResourceDefaults(agent) {
  return ['pi', 'antigravity', 'copilot'].includes(agent) ? ['tool'] : ['native'];
}

function agentConfigurations({endpoint, name = 'mcp', headers = {}, level = 'user',
  resourceTools = true, resourceModes} = {}) {
  if (!['project', 'user'].includes(level)) {
    throw new Error('Choose Project or User configuration level.');
  }
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
  const withResources = modes => {
    if (!Array.isArray(modes) || !modes.length || modes.some(mode => !['tool', 'native'].includes(mode))) {
      throw new Error('Select at least one resource interface.');
    }
    const queryStart = endpoint.indexOf('?');
    const base = queryStart === -1 ? endpoint : endpoint.slice(0, queryStart);
    const parameters = queryStart === -1 ? [] : endpoint.slice(queryStart + 1).split('&')
      .filter(parameter => parameter && !new URLSearchParams(parameter).has('resources'));
    const selected = [...new Set(modes)];
    if (selected.includes('tool')) {
      parameters.push('resources=tool');
      if (selected.includes('native')) parameters.push('resources=native');
    }
    return parameters.length ? `${base}?${parameters.join('&')}` : base;
  };
  const selectedEndpoint = resourceTools && resourceModes !== undefined ? withResources(resourceModes) : null;
  const endpointFor = agent => !resourceTools ? endpoint : selectedEndpoint ??
    withResources(agentResourceDefaults(agent));
  const server = agent => ({url: endpointFor(agent), ...credentials});
  const json = value => JSON.stringify(value, null, 2);
  const named = value => ({mcpServers: {[name]: value}});
  const merge = 'Create the file if it does not exist. Merge this server entry with existing settings.';
  const userLevel = level === 'user';
  const file = (projectPath, userPath) => {
    const target = userLevel ? userPath : projectPath;
    const location = userLevel ? 'your home directory for all your projects' : 'your project root';
    return {target, instructions: `Add the configuration below to ${target} in ${location}. ${merge}`};
  };
  const signIn = entries.length || url.searchParams.has('token')
    ? '' : ' If the server requires OAuth, sign in from the agent.';
  const tomlHeaders = entries.length
    ? '\nhttp_headers = { ' + entries.map(([key, value]) =>
      `${JSON.stringify(key)} = ${JSON.stringify(value)}`).join(', ') + ' }' : '';
  const remote = {type: 'remote', ...server('opencode')};
  if (entries.length || url.searchParams.has('token')) remote.oauth = false;
  return [
    {
      id: 'mcp-json', label: 'Claude Code',
      ...file('.mcp.json', '~/.claude.json'),
      code: json(named({type: 'http', ...server('mcp-json')})), language: 'json',
      verify: (userLevel ? 'Start Claude Code and open /mcp.'
        : 'Start Claude Code in this project, approve the server, and open /mcp.') + signIn,
      link: `https://code.claude.com/docs/en/mcp#${level}-scope`,
    },
    {
      id: 'codex', label: 'Codex',
      ...file('.codex/config.toml', '~/.codex/config.toml'),
      code: `[mcp_servers.${JSON.stringify(name)}]\nurl = ${JSON.stringify(endpointFor('codex'))}${tomlHeaders}\n`,
      language: 'toml',
      verify: (userLevel ? '' : 'Trust this project in Codex first. ')
        + 'Run codex mcp list to check the server.'
        + (signIn ? ` For OAuth, run codex mcp login ${name}.` : ''),
      link: 'https://learn.chatgpt.com/docs/extend/mcp?surface=cli',
    },
    {
      id: 'cursor', label: 'Cursor',
      ...file('.cursor/mcp.json', '~/.cursor/mcp.json'),
      code: json(named(server('cursor'))), language: 'json',
      verify: 'Open Cursor Settings → Tools & MCP and check the server.' + signIn,
      link: 'https://cursor.com/docs/mcp',
    },
    {
      id: 'copilot', label: 'GitHub Copilot',
      ...file('.mcp.json', '~/.copilot/mcp-config.json'),
      code: json(named({type: 'http', ...server('copilot'), tools: ['*']})), language: 'json',
      verify: 'In VS Code, run MCP: List Servers and start the server.' + signIn,
      link: 'https://code.visualstudio.com/docs/agents/reference/mcp-configuration',
    },
    {
      id: 'gemini', label: 'Gemini CLI',
      ...file('.gemini/settings.json', '~/.gemini/settings.json'),
      code: json(named({httpUrl: endpointFor('gemini'), ...credentials})), language: 'json',
      verify: (userLevel ? 'Start Gemini CLI and open /mcp.'
        : 'Start Gemini CLI in this project and open /mcp.') + signIn,
      link: 'https://geminicli.com/docs/tools/mcp-server/',
    },
    {
      id: 'pi', label: 'Pi',
      ...file('.pi/mcp.json', '~/.pi/agent/mcp.json'),
      code: json(named(server('pi'))), language: 'json',
      verify: (userLevel ? 'Start Pi and open /mcp.'
        : 'Trust this project in Pi, then open /mcp.') + signIn,
      link: 'https://docs.pi.dev/latest/mcp',
    },
    {
      id: 'antigravity', label: 'Antigravity',
      ...file('.agents/mcp_config.json', '~/.gemini/config/mcp_config.json'),
      code: json(named({serverUrl: endpointFor('antigravity'), ...credentials})), language: 'json',
      verify: 'Open MCP Servers in Antigravity, or run /mcp in Antigravity CLI.' + signIn,
      link: 'https://antigravity.google/docs/mcp',
    },
    {
      id: 'opencode', label: 'OpenCode V2',
      ...file('opencode.json', '~/.config/opencode/opencode.json'),
      code: json({$schema: 'https://opencode.ai/config.json', mcp: {servers: {[name]: remote}}}),
      language: 'json',
      verify: 'Run opencode mcp list to check the server.' + signIn,
      link: 'https://opencode.ai/docs/mcp-servers/',
    },
  ];
}

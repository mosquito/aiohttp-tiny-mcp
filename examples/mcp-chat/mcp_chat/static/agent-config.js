export function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

export function agentConfig({ endpoint, user, token = "", auth = "bearer" } = {}) {
  if (typeof endpoint !== "string" || /[\x00-\x1f\x7f]/.test(endpoint)) {
    throw new Error("Use an MCP endpoint without control characters.");
  }
  if (typeof token !== "string" || /[\x00-\x20\x7f]/.test(token)) {
    throw new Error("Token must not contain whitespace or control characters.");
  }
  if (!["bearer", "url"].includes(auth)) throw new Error("Choose Bearer or URL authentication.");
  const url = new URL(endpoint);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("Use an HTTP or HTTPS MCP endpoint without embedded credentials.");
  }
  url.searchParams.delete("token");
  if (token && auth === "url") url.searchParams.set("token", token);
  if (user !== undefined) {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}$/.test(user)) {
      throw new Error("Agent user must contain 1–64 letters, digits, or . _ @ -.");
    }
    url.searchParams.set("user", user);
  }
  if (token) {
    url.searchParams.delete("org");
    url.searchParams.delete("user");
  }
  const address = url.href;
  const bearer = token && auth === "bearer";
  const authorization = `Bearer ${token}`;
  const headers = bearer ? { Authorization: authorization } : null;
  const server = headers ? { url: address, headers } : { url: address };
  const httpServer = { type: "http", ...server };
  const json = (value) => JSON.stringify(value, null, 2);
  const onceHeader = bearer ? ` -c ${shellQuote(`mcp_servers.mcp_chat.http_headers={Authorization=${JSON.stringify(authorization)}}`)}` : "";
  const tokenExport = bearer ? `export MCP_CHAT_TOKEN=${shellQuote(token)}\n` : "";
  const tokenOption = bearer ? " --bearer-token-env-var MCP_CHAT_TOKEN" : "";
  const tomlToken = bearer ? '\nbearer_token_env_var = "MCP_CHAT_TOKEN"' : "";
  const claudeHeader = bearer ? ` --header ${shellQuote(`Authorization: ${authorization}`)}` : "";
  return {
    endpoint: address,
    variants: [
      {
        id: "codex-once",
        label: "Codex · one-time",
        target: "One Codex run · no configuration file changes",
        instructions: "Run this command in bash or zsh. The MCP server is available only in this Codex run.",
        code: `codex -c ${shellQuote(`mcp_servers.mcp_chat.url=${JSON.stringify(address)}`)}${onceHeader}`,
        language: "bash / zsh",
        verify: "In the opened Codex session, run /mcp and check mcp_chat.",
        link: "https://learn.chatgpt.com/docs/config-file/config-advanced#one-off-overrides-from-the-cli",
      },
      {
        id: "codex-persistent",
        label: "Codex · persistent",
        target: "~/.codex/config.toml · user configuration",
        instructions: "Run this command in bash or zsh to save the server. If mcp_chat already exists, edit its entry in the file." + (bearer ? " Export MCP_CHAT_TOKEN in each terminal before starting Codex; the configuration stores the variable name, not its value." : ""),
        code: `${tokenExport}codex mcp add mcp_chat --url ${shellQuote(address)}${tokenOption}`,
        language: "bash / zsh",
        verify: "Run codex mcp get mcp_chat, then start a new Codex session and check /mcp.",
        link: "https://developers.openai.com/learn/docs-mcp",
        extra: {
          label: "Or merge this TOML into ~/.codex/config.toml",
          code: `[mcp_servers.mcp_chat]\nurl = ${JSON.stringify(address)}${tomlToken}`,
        },
      },
      {
        id: "claude-code",
        label: "Claude Code",
        target: "~/.claude.json · user scope",
        instructions: "Run this command in bash or zsh to make the server available across your projects.",
        code: `claude mcp add --transport http --scope user mcp_chat ${shellQuote(address)}${claudeHeader}`,
        language: "bash / zsh",
        verify: "Start Claude Code and run /mcp to check the server connection.",
        link: "https://code.claude.com/docs/en/mcp",
      },
      {
        id: "claude-project",
        label: ".mcp.json · Claude Code project",
        target: ".mcp.json · project root",
        instructions: "Merge this entry into the project's .mcp.json. Preserve existing servers and other settings.",
        code: json({ mcpServers: { mcp_chat: httpServer } }),
        language: "JSON",
        verify: "Open Claude Code in this project, approve the project MCP server when asked, and check /mcp.",
        link: "https://code.claude.com/docs/en/mcp#project-scope",
      },
      {
        id: "pi",
        label: "Pi · pi.dev",
        target: "~/.pi/agent/mcp.json · user configuration",
        instructions: "Current Pi includes MCP support. If needed, install or update it in bash or zsh with: npm install -g --ignore-scripts @earendil-works/pi-coding-agent. Merge this entry into ~/.pi/agent/mcp.json and preserve existing settings.",
        code: json({ mcpServers: { mcp_chat: server } }),
        language: "JSON",
        verify: "Run pi mcp list to check the connection. Start Pi, or run /reload in an existing session, then inspect mcp_chat with /mcp.",
        link: "https://pi.dev/docs/latest/mcp",
      },
      {
        id: "cursor",
        label: "Cursor",
        target: "~/.cursor/mcp.json · user configuration",
        instructions: "Merge this server into your Cursor MCP configuration. Preserve existing servers and other settings.",
        code: json({ mcpServers: { mcp_chat: server } }),
        language: "JSON",
        verify: "Open Cursor's MCP settings and check that mcp_chat is connected and enabled.",
        link: "https://developers.openai.com/learn/docs-mcp",
      },
      {
        id: "vscode",
        label: "VS Code",
        target: ".vscode/mcp.json · workspace configuration",
        instructions: "Merge this server into the workspace's .vscode/mcp.json. Preserve existing servers and other settings.",
        code: json({ servers: { mcp_chat: httpServer } }),
        language: "JSON",
        verify: "Open the MCP server list in VS Code and start mcp_chat. Review any server trust prompt.",
        link: "https://developers.openai.com/learn/docs-mcp",
      },
    ],
    prompt: (
      "Use the mcp_chat MCP server. Call identity with {} to confirm the organization and user.\n" +
      "Read chat://rules with resources/read, then call list_channels with {}.\n" +
      'If I want to follow them, call subscribe with {"channel":"general"} and {"channel":"tasks"}.\n' +
      "Check inbox with {} when working on this chat. Fetch further pages using next_after as after.\n" +
      "Process messages before calling mark_read with the channel and last processed message_id.\n" +
      "Use thread for context and post_message with parent_id for replies. Do not mark unseen messages read.\n" +
      'For a private message, call send_dm with {"user":"teammate","text":"your message"}.\n' +
      "This connection does not start a background polling process."
    ),
  };
}

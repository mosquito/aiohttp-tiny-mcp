import { HttpError, MCPClient } from "./mcp.js";
import { agentConfig } from "./agent-config.js";

const byId = (id) => document.getElementById(id);
const PAGE_SIZE = 50;
const AUTH_REQUIRED = document.documentElement.dataset.authRequired === "true";
let active = null;
let timer = null;

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function button(text, action, className = "quiet") {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function current(context) {
  return Boolean(context && active === context && !context.client.abort.signal.aborted);
}

function status(text, kind = "") {
  byId("connection-status").textContent = text;
  byId("connection-status").className = `status ${kind}`;
}

function report(error, context = active) {
  if (!current(context) || error.name === "AbortError") return;
  if (AUTH_REQUIRED && error.status === 401) {
    showLogin(context, "Your session expired. Sign in again.");
    return;
  }
  byId("error-text").textContent = error.message || String(error);
  byId("error-banner").hidden = false;
}

function draftKey(context, channel, root = "channel") {
  return `mcp-chat:draft:${JSON.stringify([context.org, context.user, channel, root])}`;
}

function readDraft(context, channel, root) {
  try { return localStorage.getItem(draftKey(context, channel, root)) || ""; }
  catch { return ""; }
}

function writeDraft(context, channel, root, text) {
  try {
    const key = draftKey(context, channel, root);
    if (text) localStorage.setItem(key, text);
    else localStorage.removeItem(key);
  } catch {
    // Browsers can deny local storage.
  }
}

function saveDrafts() {
  if (!active) return;
  if (active.view === "channel") writeDraft(active, active.channel, "channel", byId("message-text").value);
  if (active.thread) writeDraft(active, active.thread.channel, active.thread.root, byId("reply-text").value);
}

function merged(first, second) {
  const messages = new Map(first.map((message) => [message.id, message]));
  for (const message of second) messages.set(message.id, message);
  return [...messages.values()].sort((a, b) => a.id - b.id);
}

function channelInfo(name) {
  return [...active.channels, ...active.conversations].find((channel) => channel.name === name);
}

function conversationLabel(name) {
  const info = channelInfo(name);
  if (!info) return "Conversation";
  return info.kind === "dm" ? `@ ${info.peer}` : `# ${info.name}`;
}

function updateURL() {
  const url = new URL(location.href);
  url.searchParams.set("org", active.org);
  url.searchParams.set("user", active.user);
  url.searchParams.delete("token");
  if (active.view === "channel") url.searchParams.set("channel", active.channel);
  else url.searchParams.delete("channel");
  url.searchParams.set("view", active.view);
  history.replaceState(null, "", url);
  byId("console-link").href = "/console";
  byId("observe-link").href = `/observe?${new URLSearchParams({ org: active.org, user: active.user })}`;
}

function renderNavigation() {
  const conversations = [...active.channels, ...active.conversations];
  const total = conversations.reduce((sum, channel) => sum + (channel.unread || 0), 0);
  const badge = byId("inbox-badge");
  badge.textContent = String(total);
  badge.classList.toggle("zero", total === 0);
  byId("inbox-open").setAttribute("aria-current", active.view === "inbox" ? "page" : "false");
  byId("search-open").setAttribute("aria-current", active.view === "search" ? "page" : "false");
  const signature = JSON.stringify([conversations, active.view, active.channel]);
  if (byId("channels").dataset.signature === signature) return;
  byId("channels").dataset.signature = signature;
  for (const [id, items] of [["channels", active.channels], ["direct-messages", active.conversations]]) {
    const fragment = document.createDocumentFragment();
    for (const channel of items) {
      const direct = channel.kind === "dm";
      const node = button("", () => navigate("channel", channel.name), `nav-item${channel.subscribed ? " joined" : ""}`);
      node.dataset.channel = channel.name;
      node.setAttribute("aria-current", active.view === "channel" && active.channel === channel.name ? "page" : "false");
      node.setAttribute("aria-label", `${conversationLabel(channel.name)}${channel.unread ? `, ${channel.unread} unread` : ""}`);
      node.title = direct ? `Private conversation with ${channel.peer}` : channel.subscribed ? "Following this channel" : "Public channel · join to receive messages";
      const label = element("span", "channel-label");
      label.append(element("span", "hash", direct ? "@" : "#"), document.createTextNode(direct ? channel.peer : channel.name));
      const meta = element("span", "channel-meta");
      if (channel.unread) meta.append(element("span", "badge", String(channel.unread)));
      else if (channel.subscribed) meta.append(element("span", "join-dot"));
      node.append(label, meta);
      fragment.append(node);
    }
    if (id === "direct-messages" && !items.length) fragment.append(element("p", "muted dm-empty", "No direct messages yet."));
    byId(id).replaceChildren(fragment);
  }
  const selected = byId("search-channel").value;
  byId("search-channel").replaceChildren(new Option("All conversations", ""), ...conversations.map((channel) => new Option(conversationLabel(channel.name), channel.name)));
  byId("search-channel").value = selected;
}

function renderHeading() {
  const context = active;
  byId("agent-open").disabled = context.authPending;
  byId("rules-open").disabled = context.authPending;
  byId("channel-create-open").disabled = context.authPending;
  byId("dm-open").disabled = context.authPending;
  renderNotificationControl();
  const isChannel = context.view === "channel";
  const info = channelInfo(context.channel);
  const direct = info?.kind === "dm";
  byId("view-eyebrow").textContent = isChannel ? direct ? "Direct message" : context.org : "Your workspace";
  byId("view-title").textContent = isChannel ? conversationLabel(context.channel) : context.view === "search" ? "Search messages" : "Inbox";
  byId("view-description").textContent = isChannel
    ? direct ? `Only you and ${info.peer} can read this conversation.` : info?.description || "A public conversation in your organization."
    : context.view === "search" ? 'Search public channels and your direct messages with FTS5: words, "quoted phrases", OR, or prefix*.' : "Unread messages from your channels and direct messages. Reading does not mark them read.";
  byId("subscribe-toggle").hidden = !isChannel || !info || direct;
  byId("subscribe-toggle").textContent = info?.subscribed ? "Leave channel" : "Join channel";
  byId("subscribe-toggle").classList.toggle("primary", !info?.subscribed);
  byId("mark-read").hidden = context.messages.length === 0 || context.view === "search" || (isChannel && !info?.subscribed);
  byId("search-form").hidden = context.view !== "search";
  byId("composer").hidden = !isChannel;
  byId("composer-label").textContent = isChannel ? `Message ${conversationLabel(context.channel)}` : "Message";
  byId("message-text").disabled = !info?.subscribed || context.sending;
  byId("send-message").disabled = !info?.subscribed || context.sending;
  byId("composer-note").textContent = info?.subscribed
    ? "⌘ / Ctrl + Enter to send · Draft saved on this device"
    : "Join this channel to send messages and receive them in your inbox.";
  byId("load-more").hidden = !context.more;
  byId("page-note").textContent = context.messages.length ? `${context.messages.length} messages shown` : "";
  if (context.thread) {
    const joined = channelInfo(context.thread.channel)?.subscribed;
    byId("reply-text").disabled = !joined || context.thread.sending;
    byId("send-reply").disabled = !joined || context.thread.sending;
    byId("reply-note").textContent = joined ? "Replies stay in this thread." : "Join this channel to reply.";
  }
}

function messageNode(message, inThread = false) {
  const node = element("article", "message");
  node.dataset.messageId = message.id;
  const avatar = element("div", "avatar", message.user.slice(0, 2).toUpperCase());
  avatar.setAttribute("aria-hidden", "true");
  const body = element("div", "message-body");
  const meta = element("div", "message-meta");
  meta.append(element("span", "message-author", message.user));
  const time = element("time");
  time.dateTime = message.created_at;
  const date = new Date(message.created_at);
  time.textContent = date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  time.title = date.toLocaleString();
  meta.append(time);
  if (!inThread && active.view !== "channel") meta.append(button(conversationLabel(message.channel), () => navigate("channel", message.channel), "message-channel"));
  meta.append(element("span", "message-id", `#${message.id}`));
  body.append(meta, element("p", "message-text", message.text));
  if (!inThread) {
    const actions = element("div", "message-actions");
    actions.append(button(message.parent_id ? "View thread" : "Reply in thread", () => openThread(message)));
    if (active.view === "inbox") actions.append(button("Mark read through here", () => markRead([{ channel: message.channel, id: message.id }])));
    body.append(actions);
  }
  node.append(avatar, body);
  return node;
}

function renderMessages() {
  const target = byId("messages");
  const signature = JSON.stringify([active.view, active.channel, active.query, active.messages, active.loaded, active.conversations.map((item) => [item.name, item.peer])]);
  if (target.dataset.signature === signature) return;
  target.dataset.signature = signature;
  if (active.messages.length) {
    target.replaceChildren(...active.messages.map((message) => messageNode(message)));
  } else {
    const empty = element("div", "empty-state");
    const titles = { inbox: "You're all caught up", channel: "Start the conversation", search: active.query ? "No messages found" : "Find a conversation" };
    const direct = channelInfo(active.channel)?.kind === "dm";
    const tips = { inbox: "Follow a channel or start a direct message. Your own messages do not appear in your inbox.", channel: direct ? "Send the first message to start your private conversation." : "Send the first message, or join this channel to take part.", search: "Search message text. You can narrow results to one conversation." };
    empty.append(element("span", "empty-icon", active.view === "inbox" ? "✓" : "#"));
    empty.append(element("h2", "", active.loaded ? titles[active.view] : "Loading messages…"), element("p", "", active.loaded ? tips[active.view] : "Connecting through MCP."));
    target.replaceChildren(empty);
  }
}

function renderThread() {
  const thread = active.thread;
  byId("thread-panel").hidden = !thread;
  if (!thread) return;
  byId("thread-channel").textContent = `${conversationLabel(thread.channel)} · message ${thread.root}`;
  const signature = JSON.stringify([thread.root, thread.messages]);
  if (byId("thread-messages").dataset.signature !== signature) {
    byId("thread-messages").dataset.signature = signature;
    byId("thread-messages").replaceChildren(...thread.messages.map((message) => messageNode(message, true)));
  }
  byId("thread-more").hidden = !thread.more;
}

async function loadThread(context, more = false) {
  const thread = context.thread;
  if (!thread || thread.loading || (thread.more && !more)) return;
  thread.loading = true;
  try {
    const page = await context.client.call("thread", { message_id: thread.root, after: thread.messages.at(-1)?.id || 0, limit: PAGE_SIZE });
    if (!current(context) || context.thread !== thread) return;
    thread.messages = merged(thread.messages, page.messages);
    thread.more = page.messages.length === PAGE_SIZE;
    renderThread();
    renderHeading();
  } finally {
    thread.loading = false;
  }
}

async function fetchInbox(context, count) {
  let messages = [];
  let more = false;
  do {
    const page = await context.client.call("inbox", { after: messages.at(-1)?.id || 0, limit: PAGE_SIZE });
    messages = messages.concat(page.messages);
    more = page.messages.length === PAGE_SIZE;
    if (!more) break;
  } while (messages.length < count && current(context));
  return { messages, more };
}

async function refresh(context = active, force = false) {
  if (!current(context) || context.authPending) return;
  if (context.refreshing) {
    if (force) context.refreshAgain = true;
    return context.refreshing;
  }
  context.refreshing = (async () => {
    do {
      context.refreshAgain = false;
      const viewId = context.viewId;
      const view = context.view;
      const [channels, inbox, direct] = await Promise.all([
        context.client.call("list_channels"),
        fetchInbox(context, view === "inbox" ? context.inboxSize : PAGE_SIZE),
        context.client.call("list_dms"),
      ]);
      if (!current(context)) return;
      context.channels = channels.channels;
      context.conversations = direct.conversations;
      context.inbox = inbox.messages;
      if (viewId === context.viewId) {
        if (view === "inbox") {
          context.messages = inbox.messages;
          context.more = inbox.more;
        } else if (view === "channel" && (!context.loaded || !context.more || context.loadMore)) {
          const page = await context.client.call("history", { channel: context.channel, after: context.messages.at(-1)?.id || 0, limit: PAGE_SIZE });
          if (!current(context)) return;
          if (viewId === context.viewId) {
            context.messages = merged(context.messages, page.messages);
            context.more = page.messages.length === PAGE_SIZE;
            context.loadMore = false;
          }
        }
        if (viewId === context.viewId) context.loaded = true;
      }
      if (!current(context)) return;
      renderNavigation();
      renderMessages();
      renderHeading();
      await loadThread(context);
      if (!current(context)) return;
      await syncNotifications(context);
      if (!current(context)) return;
      status(context.live ? "Live" : "Connected · polling", "online");
      byId("sync-note").textContent = context.live ? "Live MCP updates" : "Polling fallback · every 3 seconds";
    } while (context.refreshAgain && current(context));
  })().catch((error) => {
    if (current(context) && error.name !== "AbortError") {
      status("Sync paused · retrying", "offline");
      report(error, context);
    }
  }).finally(() => { context.refreshing = null; });
  return context.refreshing;
}

async function poll() {
  clearTimeout(timer);
  if (!document.hidden && active && !active.live) await refresh(active);
  timer = setTimeout(poll, 3000);
}

function closeThread() {
  saveDrafts();
  if (active) active.thread = null;
  byId("thread-panel").hidden = true;
}

function navigate(view, channel = "") {
  if (!active) return;
  saveDrafts();
  closeThread();
  active.viewId += 1;
  active.view = view;
  active.channel = channel;
  active.messages = [];
  active.more = false;
  active.loadMore = false;
  active.inboxSize = PAGE_SIZE;
  active.loaded = view === "search";
  active.query = "";
  if (view === "channel") byId("message-text").value = readDraft(active, channel, "channel");
  updateURL();
  renderNavigation();
  renderHeading();
  renderMessages();
  if (view === "search") byId("search-query").focus();
  refresh(active, true);
}

async function connect(org, user, initial = false) {
  saveDrafts();
  org = org.trim().toLowerCase();
  user = user.trim();
  const params = new URLSearchParams({ org, user });
  let client;
  try {
    client = new MCPClient(AUTH_REQUIRED ? "/mcp" : `/mcp?${params}`);
  } catch (error) {
    byId("error-text").textContent = error.message;
    byId("error-banner").hidden = false;
    status("Check connection settings", "offline");
    return;
  }
  if (active) {
    clearTimeout(active.refreshTimer);
    active.client.close();
  }
  const url = new URL(location.href);
  const channel = initial ? url.searchParams.get("channel") || "" : "";
  active = {
    client, org, user, token: "", authPending: AUTH_REQUIRED, live: false, refreshTimer: null,
    notificationReady: false, notificationCursor: 0, notificationSync: null,
    channels: [], conversations: [], inbox: [], inboxSize: PAGE_SIZE, messages: [], more: false, loadMore: false, loaded: false,
    view: channel ? "channel" : initial && url.searchParams.get("view") === "search" ? "search" : "inbox", channel, viewId: 0, thread: null,
    query: "", searchChannel: "", sending: false, refreshAgain: false, refreshing: null,
  };
  const context = active;
  byId("workspace-name").textContent = org;
  byId("org").value = org;
  byId("user").value = user;
  byId("message-text").value = channel ? readDraft(context, channel, "channel") : "";
  byId("reply-text").value = "";
  byId("dm-form").reset();
  byId("error-banner").hidden = true;
  byId("thread-panel").hidden = true;
  byId("rules-dialog").close();
  byId("channel-dialog").close();
  byId("dm-dialog").close();
  byId("agent-dialog").close();
  byId("login-dialog").close();
  status("Connecting…");
  updateURL();
  renderNavigation();
  renderHeading();
  renderMessages();
  if (AUTH_REQUIRED) {
    try {
      const result = await authRequest("/admin/token", { org, user }, context);
      if (!current(context)) return;
      acceptToken(context, result.token);
    } catch (error) {
      if (!current(context)) return;
      if (error.status === 401) showLogin(context);
      else {
        status("Sign-in unavailable", "offline");
        report(error, context);
      }
      return;
    }
  }
  await refresh(context);
  startLive(context);
}

function startLive(context) {
  if (!current(context) || context.authPending) return;
  context.stopWatching?.();
  context.stopWatching = context.client.watch([
    "chat://channels", "chat://subscriptions", "chat://inbox", "chat://dms",
  ], {
    onEvent(frame) {
      if (!current(context) || context.authPending || !frame.method?.startsWith("notifications/")) return;
      if (context.refreshTimer) return;
      context.refreshTimer = setTimeout(() => {
        context.refreshTimer = null;
        if (current(context)) refresh(context, true);
      }, 100);
    },
    onStatus(state, error) {
      if (!current(context) || context.authPending) return;
      context.live = state === "live";
      if (error?.status === 401) {
        report(error, context);
        return;
      }
      status(context.live ? "Live" : "Connected · polling", "online");
      byId("sync-note").textContent = context.live ? "Live MCP updates" : "Polling fallback · reconnecting live updates";
      if (context.live) refresh(context, true);
    },
  });
}

function renderNotificationControl() {
  const node = byId("notifications-enable");
  if (!("Notification" in window) || !window.isSecureContext) {
    node.disabled = true;
    node.textContent = "Notifications unavailable";
    node.title = "Browser notifications require a supported browser and HTTPS or localhost.";
    return;
  }
  node.disabled = Notification.permission !== "default" || Boolean(active?.authPending);
  node.textContent = Notification.permission === "granted" ? "Notifications enabled" : Notification.permission === "denied" ? "Notifications blocked" : "Enable notifications";
  node.title = Notification.permission === "denied" ? "Allow notifications in this browser's site settings." : "Notify about new incoming messages while this window is in the background.";
}

async function syncNotifications(context) {
  if (!current(context) || !("Notification" in window) || Notification.permission !== "granted") return;
  if (context.notificationSync) return context.notificationSync;
  context.notificationSync = (async () => {
    const baseline = !context.notificationReady;
    let page;
    do {
      page = await context.client.call("inbox", { after: context.notificationCursor, limit: 100 });
      if (!current(context)) return;
      for (const message of page.messages) {
        if (message.id <= context.notificationCursor) continue;
        context.notificationCursor = message.id;
        if (baseline || message.user === context.user || (!document.hidden && document.hasFocus())) continue;
        try {
          const notification = new Notification(`${message.user} · ${conversationLabel(message.channel)}`, {
            body: message.text,
            tag: `mcp-chat:${JSON.stringify([context.org, context.user, message.id])}`,
          });
          notification.onclick = () => {
            window.focus();
            if (current(context)) navigate("channel", message.channel);
            notification.close();
          };
        } catch {
          renderNotificationControl();
        }
      }
    } while (page.messages.length === 100);
    context.notificationReady = true;
  })().finally(() => { context.notificationSync = null; });
  return context.notificationSync;
}

async function authRequest(path, body, context) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    redirect: "error",
    signal: context.client.abort.signal,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new HttpError(response, response.status === 401 ? "Sign in with the server secret." : `Sign-in request failed (HTTP ${response.status}).`);
  }
  return await response.json();
}

function acceptToken(context, token) {
  const client = new MCPClient("/mcp", { token });
  context.client.close();
  context.client = client;
  context.token = token;
  context.authPending = false;
  context.live = false;
  renderHeading();
}

function showLogin(context, message = "") {
  if (!AUTH_REQUIRED || !current(context)) return;
  if (context.authPending && byId("login-dialog").open) return;
  context.authPending = true;
  context.live = false;
  context.stopWatching?.();
  context.token = "";
  byId("agent-dialog").close();
  byId("rules-dialog").close();
  byId("channel-dialog").close();
  byId("dm-dialog").close();
  byId("login-context").textContent = `${context.org} · ${context.user}`;
  byId("login-secret").value = "";
  byId("login-error").textContent = message;
  status("Sign in required", "offline");
  renderHeading();
  if (!byId("login-dialog").open) byId("login-dialog").showModal();
  byId("login-secret").focus();
}

async function action(node, work) {
  if (!active || node.disabled || active.authPending) return;
  const context = active;
  node.disabled = true;
  try { await work(context); }
  catch (error) { report(error, context); }
  finally {
    node.disabled = false;
    if (active) renderHeading();
  }
}

async function markRead(messages = active.messages) {
  const context = active;
  const latest = new Map();
  for (const message of messages) latest.set(message.channel, Math.max(latest.get(message.channel) || 0, message.id));
  await action(byId("mark-read"), async () => {
    for (const [channel, message_id] of latest) {
      if (!current(context)) return;
      await context.client.call("mark_read", { channel, message_id });
    }
    if (current(context)) await refresh(context, true);
  });
}

async function openThread(message) {
  saveDrafts();
  const context = active;
  context.thread = { root: message.parent_id || message.id, channel: message.channel, messages: [], more: false, loading: false, sending: false };
  byId("reply-text").value = readDraft(context, message.channel, context.thread.root);
  renderThread();
  renderHeading();
  try { await loadThread(context); }
  catch (error) { report(error, context); }
  if (current(context)) byId("thread-close").focus();
}

async function sendMessage(inThread) {
  const context = active;
  const thread = inThread ? context.thread : null;
  if (inThread && !thread) return;
  const field = byId(inThread ? "reply-text" : "message-text");
  const node = byId(inThread ? "send-reply" : "send-message");
  const text = field.value.trim();
  if (!text) return;
  const channel = thread?.channel || context.channel;
  const root = thread?.root || "channel";
  await action(node, async () => {
    if (thread) thread.sending = true;
    else context.sending = true;
    renderHeading();
    try {
      const args = { channel, text };
      if (thread) args.parent_id = thread.root;
      await context.client.call("post_message", args);
      writeDraft(context, channel, root, "");
      if (!current(context)) return;
      if ((thread && context.thread === thread) || (!thread && context.view === "channel" && context.channel === channel)) field.value = "";
      await refresh(context, true);
    } finally {
      if (thread) thread.sending = false;
      else context.sending = false;
    }
  });
}

async function runSearch(more = false) {
  const context = active;
  const query = byId("search-query").value.trim();
  if (!query) return;
  if (!more) {
    context.query = query;
    context.searchChannel = byId("search-channel").value;
    context.messages = [];
    context.viewId += 1;
  }
  const viewId = context.viewId;
  const args = { query: context.query, after: more ? context.messages.at(-1)?.id || 0 : 0, limit: PAGE_SIZE };
  if (context.searchChannel) args.channel = context.searchChannel;
  const result = await context.client.call("search", args);
  if (!current(context) || context.viewId !== viewId) return;
  context.messages = merged(context.messages, result.messages);
  context.more = result.messages.length === PAGE_SIZE;
  context.loaded = true;
  renderMessages();
  renderHeading();
}

function renderAgentGuide() {
  const endpoint = new URL("/mcp", location.origin);
  if (!AUTH_REQUIRED) endpoint.search = new URLSearchParams({ org: active.org, user: active.user }).toString();
  byId("agent-copy-status").textContent = "";
  try {
    const user = byId("agent-user").value.trim();
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}$/.test(user)) {
      throw new Error("Agent user must contain 1–64 letters, digits, or . _ @ -.");
    }
    const token = AUTH_REQUIRED ? byId("agent-token").value : "";
    const config = agentConfig({
      endpoint: endpoint.href,
      user: AUTH_REQUIRED ? undefined : user,
      token,
      auth: byId("agent-auth").value,
    });
    byId("agent-secret-note").hidden = !token;
    const choice = byId("agent-client").value;
    if (!byId("agent-client").options.length) {
      byId("agent-client").replaceChildren(...config.variants.map((variant) => new Option(variant.label, variant.id)));
    }
    const variant = config.variants.find((item) => item.id === choice) || config.variants[0];
    byId("agent-client").value = variant.id;
    byId("agent-error").hidden = true;
    byId("agent-guide").hidden = AUTH_REQUIRED && !token;
    byId("agent-endpoint").value = config.endpoint;
    byId("agent-target").textContent = variant.target;
    byId("agent-instructions").textContent = variant.instructions;
    byId("agent-language").textContent = variant.language;
    byId("agent-code").value = variant.code;
    byId("agent-code").rows = Math.min(14, Math.max(3, variant.code.split("\n").length));
    byId("agent-extra").hidden = !variant.extra;
    byId("agent-extra").open = false;
    byId("agent-extra-label").textContent = variant.extra?.label || "";
    byId("agent-extra-code").value = variant.extra?.code || "";
    byId("agent-verify").textContent = variant.verify;
    byId("agent-docs").href = variant.link;
    byId("agent-prompt").value = config.prompt;
    byId("agent-dialog").querySelector('[data-copy="agent-code"]').textContent = variant.language === "JSON" ? "Copy configuration" : "Copy command";
  } catch (error) {
    byId("agent-error").textContent = error.message;
    byId("agent-error").hidden = false;
    byId("agent-guide").hidden = true;
  }
}

async function issueAgentToken() {
  const context = active;
  if (!AUTH_REQUIRED || !current(context) || context.authPending || byId("agent-issue").disabled) return;
  const user = byId("agent-user").value.trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,63}$/.test(user)) {
    renderAgentGuide();
    return;
  }
  byId("agent-issue").disabled = true;
  byId("agent-error").hidden = true;
  try {
    const result = await authRequest("/admin/token", { org: context.org, user }, context);
    if (!current(context) || !byId("agent-dialog").open || byId("agent-user").value.trim() !== user) return;
    byId("agent-token").value = result.token;
    byId("agent-expiry").textContent = `Token for ${user} · expires in ${Math.round(result.expires_in / 60)} minutes`;
    renderAgentGuide();
  } catch (error) {
    if (!current(context)) return;
    if (error.status === 401) showLogin(context, "Sign in again to issue an agent token.");
    else {
      byId("agent-error").textContent = error.message;
      byId("agent-error").hidden = false;
    }
  } finally {
    byId("agent-issue").disabled = false;
  }
}

async function copyAgentText(id) {
  const field = byId(id);
  try {
    await navigator.clipboard.writeText(field.value);
    byId("agent-copy-status").textContent = "Copied to clipboard.";
  } catch {
    field.focus();
    field.select();
    byId("agent-copy-status").textContent = "Clipboard access is unavailable. The text is selected; press ⌘C or Ctrl+C to copy.";
  }
}

byId("context-form").addEventListener("submit", (event) => {
  event.preventDefault();
  connect(byId("org").value.trim().toLowerCase(), byId("user").value.trim());
});
byId("notifications-enable").addEventListener("click", async () => {
  if (!("Notification" in window) || !window.isSecureContext || Notification.permission !== "default") return;
  const context = active;
  try {
    const permission = await Notification.requestPermission();
    if (!current(context)) return;
    renderNotificationControl();
    if (permission === "granted") {
      context.notificationReady = false;
      context.notificationCursor = 0;
      await syncNotifications(context);
    }
  } catch (error) {
    report(error, context);
  }
});
byId("login-close").addEventListener("click", () => byId("login-dialog").close());
byId("login-dialog").addEventListener("close", () => { byId("login-secret").value = ""; });
byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const context = active;
  if (!AUTH_REQUIRED || !current(context) || byId("login-submit").disabled) return;
  const secret = byId("login-secret").value;
  byId("login-secret").value = "";
  byId("login-error").textContent = "";
  byId("login-submit").disabled = true;
  try {
    const result = await authRequest("/admin/auth", { secret, org: context.org, user: context.user }, context);
    if (!current(context)) return;
    acceptToken(context, result.token);
    byId("login-dialog").close();
    await refresh(context, true);
    startLive(context);
  } catch (error) {
    if (!current(context)) return;
    byId("login-error").textContent = error.status === 401 ? "The server secret is incorrect." : error.message;
    byId("login-secret").focus();
  } finally {
    byId("login-submit").disabled = false;
  }
});
byId("inbox-open").addEventListener("click", () => navigate("inbox"));
byId("search-open").addEventListener("click", () => navigate("search"));
byId("refresh").addEventListener("click", () => refresh(active, true));
byId("retry").addEventListener("click", async () => {
  byId("error-banner").hidden = true;
  if (AUTH_REQUIRED && active?.authPending) await connect(active.org, active.user);
  else await refresh(active, true);
});
byId("error-dismiss").addEventListener("click", () => { byId("error-banner").hidden = true; });
byId("mark-read").addEventListener("click", () => markRead());
byId("subscribe-toggle").addEventListener("click", () => action(byId("subscribe-toggle"), async (context) => {
  const channel = context.channel;
  const subscribed = channelInfo(channel)?.subscribed;
  await context.client.call(subscribed ? "unsubscribe" : "subscribe", { channel });
  if (current(context)) await refresh(context, true);
}));
byId("composer").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(false); });
byId("thread-composer").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(true); });
byId("message-text").addEventListener("input", saveDrafts);
byId("reply-text").addEventListener("input", saveDrafts);
byId("thread-close").addEventListener("click", closeThread);
byId("thread-more").addEventListener("click", () => action(byId("thread-more"), (context) => loadThread(context, true)));
byId("load-more").addEventListener("click", () => action(byId("load-more"), async (context) => {
  if (context.view === "search") return runSearch(true);
  if (context.view === "channel") {
    context.loadMore = true;
    return refresh(context, true);
  }
  context.inboxSize += PAGE_SIZE;
  await refresh(context, true);
}));
byId("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(byId("search-form").querySelector("button"), () => runSearch());
});
byId("channel-create-open").addEventListener("click", () => {
  byId("channel-error").textContent = "";
  byId("channel-dialog").showModal();
});
byId("channel-create-close").addEventListener("click", () => byId("channel-dialog").close());
byId("dm-open").addEventListener("click", () => {
  byId("dm-error").textContent = "";
  byId("dm-dialog").showModal();
});
byId("dm-close").addEventListener("click", () => byId("dm-dialog").close());
byId("dm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(byId("dm-create"), async (context) => {
    try {
      const conversation = await context.client.call("open_dm", { user: byId("dm-user").value.trim() });
      if (!current(context)) return;
      context.conversations = context.conversations.filter((item) => item.name !== conversation.name);
      context.conversations.push(conversation);
      context.conversations.sort((first, second) => first.peer.localeCompare(second.peer));
      byId("dm-dialog").close();
      byId("dm-form").reset();
      navigate("channel", conversation.name);
    } catch (error) {
      if (!current(context)) return;
      if (error.status === 401) report(error, context);
      else byId("dm-error").textContent = error.message;
    }
  });
});
byId("channel-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(byId("channel-create"), async (context) => {
    try {
      const name = byId("channel-name").value;
      await context.client.call("create_channel", { name, description: byId("channel-description").value });
      if (!current(context)) return;
      byId("channel-dialog").close();
      byId("channel-form").reset();
      navigate("channel", name);
    } catch (error) {
      if (current(context)) byId("channel-error").textContent = error.message;
    }
  });
});
byId("rules-open").addEventListener("click", () => action(byId("rules-open"), async (context) => {
  byId("rules-content").textContent = "Loading rules…";
  byId("rules-dialog").showModal();
  try {
    const resource = await context.client.readResource("chat://rules");
    if (!current(context)) return;
    byId("rules-content").textContent = resource.contents.map((item) => item.text || "").join("\n");
  } catch (error) {
    if (current(context)) byId("rules-content").textContent = `Could not load the chat rules: ${error.message}`;
    throw error;
  }
}));
byId("rules-close").addEventListener("click", () => byId("rules-dialog").close());
byId("agent-open").addEventListener("click", () => {
  if (!active) return;
  if (active.authPending) {
    showLogin(active);
    return;
  }
  byId("agent-org").textContent = active.org;
  byId("agent-user").value = active.user;
  byId("agent-token").value = "";
  byId("agent-expiry").textContent = "Issue a token for the selected agent user.";
  byId("agent-auth-settings").hidden = !AUTH_REQUIRED;
  byId("agent-auth").value = "bearer";
  renderAgentGuide();
  byId("agent-dialog").showModal();
});
byId("agent-close").addEventListener("click", () => byId("agent-dialog").close());
byId("agent-dialog").addEventListener("close", () => byId("agent-open").focus());
byId("agent-user").addEventListener("input", () => {
  byId("agent-token").value = "";
  byId("agent-expiry").textContent = "Issue a token for the selected agent user.";
  renderAgentGuide();
});
byId("agent-client").addEventListener("change", renderAgentGuide);
byId("agent-issue").addEventListener("click", issueAgentToken);
byId("agent-auth").addEventListener("change", renderAgentGuide);
byId("agent-dialog").querySelectorAll("[data-copy]").forEach((node) => {
  node.addEventListener("click", () => copyAgentText(node.dataset.copy));
});
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    navigate("search");
  }
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    if (event.target === byId("message-text")) { event.preventDefault(); sendMessage(false); }
    if (event.target === byId("reply-text")) { event.preventDefault(); sendMessage(true); }
  }
  if (event.key === "Escape" && active?.thread) closeThread();
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden) {
    renderNotificationControl();
    refresh(active, true);
    poll();
  } else if (!active?.live) byId("sync-note").textContent = "Polling resumes when this tab is visible";
});
window.addEventListener("pagehide", () => { saveDrafts(); clearTimeout(timer); clearTimeout(active?.refreshTimer); active?.client.close(); });
const initial = new URL(location.href).searchParams;
const cleanURL = new URL(location.href);
cleanURL.searchParams.delete("token");
history.replaceState(null, "", cleanURL);
connect(initial.get("org") || "example.com", initial.get("user") || "human", true).then(poll);

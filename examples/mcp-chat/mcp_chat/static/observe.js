const byId = (id) => document.getElementById(id);
const AUTH_REQUIRED = document.documentElement.dataset.authRequired === "true";
const PAGE_SIZE = 50;
const CONVERSATION_TOOLS = ["post_message", "send_dm"];
let customTools = [];
let active = null;
let timer = null;

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function current(context) {
  return active === context && !context.abort.signal.aborted;
}

function status(text, kind = "") {
  byId("observe-status").textContent = text;
  byId("observe-status").className = `status ${kind}`;
}

function formatted(value) {
  if (typeof value === "string") return value;
  return JSON.stringify(value ?? null, null, 2);
}

function summary(event) {
  if (event.error != null) return typeof event.error === "string" ? event.error : formatted(event.error);
  const args = event.arguments || {};
  const result = event.result?.structuredContent ?? event.result;
  const peer = event.conversation?.peer || (event.tool === "send_dm" ? args.user : null);
  const destination = peer ? `@${peer} (DM)` : args.channel ? `#${args.channel}` : "";
  if (typeof args.text === "string") return `${destination ? `To ${destination}: ` : ""}${args.text}`;
  if (event.tool === "subscribe") return `Subscribed to #${args.channel}`;
  if (event.tool === "unsubscribe") return `Unsubscribed from #${args.channel}`;
  if (event.tool === "open_dm") return `Opened a direct conversation with @${args.user}`;
  if (event.tool === "create_channel") return `Created #${args.name}`;
  if (args.channel && args.message_id) return `${destination}: read through message ${args.message_id}`;
  if (args.query) return `Search: ${args.query} · ${result?.messages?.length || 0} messages returned`;
  if (args.uri) return `Read ${args.uri}`;
  if (Array.isArray(result?.messages)) {
    return result.messages.length ? `${result.messages.length} messages returned` : "No messages returned";
  }
  if (Array.isArray(result?.channels)) return `${result.channels.length} channels returned`;
  if (Array.isArray(result?.conversations)) return `${result.conversations.length} direct conversations returned`;
  if (typeof result?.text === "string") return result.text;
  if (args.user) return `User: ${args.user}`;
  if (result?.org && result?.user) return `${result.org} · ${result.user}`;
  return "Completed";
}

function eventNode(event) {
  const group = element("tbody", "observe-event");
  group.dataset.eventId = event.id;
  const row = element("tr", "observe-event-row");
  const name = element("td", "observe-event-name", event.tool);
  const failed = event.error != null;
  const outcome = element("td", "observe-event-status");
  outcome.append(element("span", `observe-outcome${failed ? " failed" : ""}`, failed ? "Error" : "OK"));
  const rawDate = typeof event.created_at === "number" && event.created_at < 1e12 ? event.created_at * 1000 : event.created_at;
  const date = new Date(rawDate);
  const time = element("time");
  if (Number.isFinite(date.getTime())) {
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    time.title = date.toLocaleString();
  } else time.textContent = "Unknown time";
  const timestamp = element("td", "observe-event-time");
  timestamp.append(time);
  const description = element("td");
  const brief = element("div", "observe-event-summary", summary(event));
  brief.title = brief.textContent;
  description.append(brief);
  const control = element("td", "observe-event-control");
  const toggle = element("button", "quiet observe-toggle", "Details");
  toggle.type = "button";
  toggle.setAttribute("aria-label", `Details for event #${event.id}`);
  toggle.setAttribute("aria-expanded", "false");
  const expanded = element("tr", "observe-event-details");
  expanded.id = `observe-details-${event.id}`;
  expanded.hidden = true;
  toggle.setAttribute("aria-controls", expanded.id);
  toggle.addEventListener("click", () => {
    expanded.hidden = !expanded.hidden;
    toggle.setAttribute("aria-expanded", String(!expanded.hidden));
    toggle.textContent = expanded.hidden ? "Details" : "Hide";
  });
  control.append(toggle);
  row.append(timestamp, name, description, outcome, control);
  const details = element("td");
  details.colSpan = 5;
  details.append(element("div", "observe-event-id", `Event #${event.id}`));
  for (const [label, value] of [["Arguments", event.arguments], ["Result", event.result], ["Error", event.error]]) {
    if (label === "Error" && value == null) continue;
    details.append(element("h3", "", label), element("pre", "", formatted(value)));
  }
  expanded.append(details);
  group.append(row, expanded);
  return group;
}

function render(context, older = false) {
  const target = byId("observe-events");
  const previousHeight = target.scrollHeight;
  const previousTop = target.scrollTop;
  const atTop = previousTop < 20;
  const existing = new Map([...target.querySelectorAll("[data-event-id]")].map((node) => [Number(node.dataset.eventId), node]));
  const events = [...context.events.values()].sort((first, second) => second.id - first.id);
  if (!events.length) {
    const empty = element("div", "empty-state");
    empty.append(element("h2", "", "No matching activity"), element("p", "", `Activity by ${context.user} in ${context.org} will appear here when it matches the selected tools.`));
    target.replaceChildren(empty);
  } else {
    const table = element("table", "observe-table");
    table.setAttribute("aria-label", "Agent activity");
    const heading = table.createTHead().insertRow();
    for (const label of ["Time", "Action", "Details", "Status", "Inspect"]) {
      const cell = element("th", "", label);
      cell.scope = "col";
      heading.append(cell);
    }
    for (const event of events) table.append(existing.get(event.id) || eventNode(event));
    target.replaceChildren(table);
  }
  if (older) target.scrollTop = previousTop;
  else target.scrollTop = atTop ? 0 : previousTop + Math.max(0, target.scrollHeight - previousHeight);
  byId("observe-count").textContent = `${events.length} calls shown`;
  byId("observe-more").hidden = !context.hasMore;
  byId("observe-page-note").textContent = events.length && !context.hasMore ? "Beginning of recorded activity" : "";
}

function showLogin(context) {
  if (!current(context) || !AUTH_REQUIRED) return;
  context.needsLogin = true;
  context.live = false;
  context.source?.close();
  status("Sign in required", "offline");
  byId("observe-error").hidden = true;
  if (!byId("observe-login-dialog").open) {
    byId("observe-secret").value = "";
    byId("observe-login-error").textContent = "";
    byId("observe-login-dialog").showModal();
    byId("observe-secret").focus();
  }
}

function report(error, context) {
  if (!current(context) || error.name === "AbortError") return;
  if (error.status === 401 && AUTH_REQUIRED) {
    showLogin(context);
    return;
  }
  byId("observe-error").textContent = error.message;
  byId("observe-error").hidden = false;
  status("Sync paused · retrying", "offline");
}

async function request(path, context, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
    credentials: "same-origin",
    redirect: "error",
    cache: "no-store",
    signal: context.abort.signal,
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    await response.body?.cancel();
    const error = new Error(response.status === 401 ? "Sign in with the JWT secret." : `Observer request failed (HTTP ${response.status}). Check the organization and agent user.`);
    error.status = response.status;
    throw error;
  }
  if (body) {
    await response.body?.cancel();
    return null;
  }
  return await response.json();
}

function observerQuery(context, extra = {}) {
  const query = new URLSearchParams({ org: context.org, user: context.user, ...extra });
  for (const tool of context.tools) query.append("tool", tool);
  return query;
}

async function page(context, before) {
  const query = observerQuery(context, { limit: PAGE_SIZE });
  if (before !== null && before !== undefined) query.set("before", before);
  return await request(`/admin/observe?${query}`, context);
}

function suggestions(result) {
  byId("observe-orgs").replaceChildren(...(result.orgs || []).map((name) => new Option(name, name)));
  byId("observe-users").replaceChildren(...(result.users || []).map((name) => new Option(name, name)));
}

async function load(context = active, older = false) {
  if (!current(context) || context.loading || context.needsLogin) return;
  context.loading = true;
  byId("observe-more").disabled = true;
  byId("observe-refresh").disabled = true;
  byId("observe-events").setAttribute("aria-busy", "true");
  try {
    let newest = 0;
    for (const id of context.events.keys()) newest = Math.max(newest, id);
    let result = await page(context, older ? context.before : null);
    if (!current(context)) return;
    suggestions(result);
    if (older || !newest) {
      for (const event of result.events) context.events.set(event.id, event);
      context.before = result.next_before;
      context.hasMore = result.has_more;
    } else {
      while (true) {
        const newEvents = result.events.filter((event) => event.id > newest);
        for (const event of newEvents) context.events.set(event.id, event);
        if (newEvents.length !== result.events.length || !result.has_more || !result.events.length) break;
        result = await page(context, result.next_before);
        if (!current(context)) return;
      }
    }
    if (!current(context)) return;
    render(context, older);
    byId("observe-error").hidden = true;
    status(context.live ? "Live" : "Connected · polling", "online");
  } catch (error) {
    report(error, context);
  } finally {
    context.loading = false;
    if (current(context)) {
      byId("observe-more").disabled = false;
      byId("observe-refresh").disabled = false;
      byId("observe-events").setAttribute("aria-busy", "false");
      if (context.refreshAgain) {
        context.refreshAgain = false;
        load(context);
      }
    }
  }
}

async function poll() {
  clearTimeout(timer);
  if (!document.hidden && active && !active.live) await load(active);
  timer = setTimeout(poll, 3000);
}

async function connect() {
  if (active) {
    active.abort.abort();
    active.source?.close();
  }
  const org = byId("observe-org").value.trim().toLowerCase();
  const user = byId("observe-user").value.trim();
  const mode = byId("observe-mode").value;
  const tools = mode === "conversations" ? [...CONVERSATION_TOOLS] : mode === "custom" ? [...customTools] : [];
  active = { org, user, tools, abort: new AbortController(), events: new Map(), before: null, hasMore: false, loading: false, needsLogin: false, live: false, refreshAgain: false };
  const context = active;
  byId("observe-org").value = org;
  byId("observe-user").value = user;
  byId("observe-title").textContent = `@ ${user}`;
  byId("observe-description").textContent = tools.length
    ? `Activity in ${org}: ${tools.join(", ")}.`
    : `All tool calls and resource reads in ${org}.`;
  byId("observe-error").hidden = true;
  byId("observe-login-dialog").close();
  byId("observe-count").textContent = "";
  byId("observe-more").hidden = true;
  byId("observe-page-note").textContent = "";
  const empty = element("div", "empty-state");
  empty.append(element("h2", "", "Loading activity…"), element("p", "", "Reading the agent activity log."));
  byId("observe-events").replaceChildren(empty);
  byId("observe-events").scrollTop = 0;
  status("Connecting…");
  const url = new URL(location.href);
  const query = observerQuery(context);
  if (!tools.length) query.set("view", "all");
  url.search = query.toString();
  history.replaceState(null, "", url);
  byId("observe-chat").href = `/ui/?${new URLSearchParams({ org, user })}`;
  await load(context);
  startStream(context);
}

function startStream(context) {
  if (!current(context) || context.needsLogin || !("EventSource" in window)) return;
  context.source?.close();
  const query = observerQuery(context, { stream: "true" });
  const source = new EventSource(`/admin/observe?${query}`, { withCredentials: true });
  context.source = source;
  source.onopen = () => {
    if (!current(context) || context.source !== source) return;
    context.live = true;
    status("Live", "online");
    if (context.loading) context.refreshAgain = true;
    else load(context);
  };
  source.addEventListener("audit", () => {
    if (!current(context) || context.source !== source) return;
    if (context.loading) context.refreshAgain = true;
    else load(context);
  });
  source.onerror = () => {
    if (!current(context) || context.source !== source) return;
    context.live = false;
    status("Connected · polling", "online");
  };
}

byId("observe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  connect();
});
byId("observe-mode").addEventListener("change", connect);
byId("observe-more").addEventListener("click", () => load(active, true));
byId("observe-refresh").addEventListener("click", () => {
  if (active.needsLogin) showLogin(active);
  else load(active);
});
byId("observe-login-close").addEventListener("click", () => byId("observe-login-dialog").close());
byId("observe-login-dialog").addEventListener("close", () => { byId("observe-secret").value = ""; });
byId("observe-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const context = active;
  if (!AUTH_REQUIRED || !current(context) || byId("observe-login-submit").disabled) return;
  const secret = byId("observe-secret").value;
  byId("observe-secret").value = "";
  byId("observe-login-error").textContent = "";
  byId("observe-login-submit").disabled = true;
  try {
    await request("/admin/auth", context, { secret, org: context.org, user: context.user });
    if (!current(context)) return;
    context.needsLogin = false;
    byId("observe-login-dialog").close();
    await load(context);
    startStream(context);
  } catch (error) {
    if (!current(context) || error.name === "AbortError") return;
    byId("observe-login-error").textContent = error.status === 401 ? "The JWT secret is incorrect." : error.message;
    byId("observe-secret").focus();
  } finally {
    byId("observe-login-submit").disabled = false;
  }
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(timer);
  if (!document.hidden) {
    load(active);
    poll();
  }
});
window.addEventListener("pagehide", () => { clearTimeout(timer); active?.abort.abort(); active?.source?.close(); });
const initial = new URL(location.href).searchParams;
byId("observe-org").value = initial.get("org") || "example.com";
byId("observe-user").value = initial.get("user") || "human";
customTools = [...new Set(initial.getAll("tool"))];
if (customTools.length) {
  const conversations = customTools.length === CONVERSATION_TOOLS.length && CONVERSATION_TOOLS.every((tool) => customTools.includes(tool));
  byId("observe-mode").value = conversations ? "conversations" : "custom";
  byId("observe-mode").querySelector('[value="custom"]').hidden = conversations;
} else byId("observe-mode").value = initial.get("view") === "all" ? "all" : "conversations";
connect().then(poll);

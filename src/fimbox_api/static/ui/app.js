"use strict";

// FIMBox browser UI. All rendering uses DOM APIs and textContent (no innerHTML of server data).
const TOKEN_KEY = "fimbox_session_token";
const PREVIEW_SUFFIXES = [".json", ".md", ".txt", ".csv", ".log", ".xml", ".yaml", ".yml", ".geojson"];
const RASTER_SUFFIXES = [".cog", ".tif", ".tiff"];
const DEFAULT_SOURCE_VERSION = "4.5.2.11";
const state = {
  config: null, token: sessionStorage.getItem(TOKEN_KEY), scenarios: [], examples: [], jobPoll: null,
  workspace: null, workspaceVersion: 0, workspaceAction: 0, workspaceLocation: null, userId: null, scope: "",
};
state.workspaces = [];
state.reachSet = [];

function $(selector, root = document) { return root.querySelector(selector); }

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (name === "text") node.textContent = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name in node && name !== "list") node[name] = value;
    else node.setAttribute(name, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child instanceof Node ? child : String(child));
    }
  }
  return node;
}

function showMessage(text, isError = false) {
  const box = $("#message");
  box.textContent = text;
  box.className = "message" + (isError ? " error" : "");
  box.hidden = !text;
}

function formatBytes(size) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function hasSuffix(name, suffixes) {
  const lower = name.toLowerCase();
  return suffixes.some((suffix) => lower.endsWith(suffix));
}

function joinKey(...parts) {
  return parts.map((part) => (part || "").replace(/^\/+|\/+$/g, "")).filter(Boolean).join("/");
}

function errorDetail(body, status) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("\n");
  }
  return detail ? String(detail) : `HTTP ${status}`;
}

class ApiError extends Error {
  constructor(message, status, headers, stale = false) {
    super(message); this.status = status; this.headers = headers; this.stale = stale;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const workspaceRequest = path.startsWith("/jobs") || path.startsWith("/outputs");
  const workspace = workspaceRequest ? state.workspace : null;
  const workspaceVersion = state.workspaceVersion;
  if (workspaceRequest && !workspace) {
    throw new ApiError("Create or open a workspace before using jobs or outputs.", 400);
  }
  if (workspace) headers.set("X-FIMBox-Workspace", `s3://${workspace.bucket}/${workspace.prefix}`);
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options = { ...options, body: JSON.stringify(options.json) };
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && state.token) {
    setToken(null);
    showSignedOut();
  }
  if (workspaceRequest && (workspaceVersion !== state.workspaceVersion
      || !state.workspace || state.workspace.workspace_id !== workspace.workspace_id)) {
    throw new ApiError("The active workspace changed while this request was running.", 409, response.headers, true);
  }
  if (options.raw) {
    if (!response.ok) throw new ApiError(`HTTP ${response.status}`, response.status, response.headers);
    return response;
  }
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!response.ok) throw new ApiError(errorDetail(body, response.status), response.status, response.headers);
  return body;
}

function query(params) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(name, value);
  }
  return search.toString();
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

// ---------- Tabs ----------

function activateTab(group, name, root = document) {
  for (const button of root.querySelectorAll(`[data-tabs="${group}"] button`)) {
    button.classList.toggle("active", button.dataset.tab === name);
    if (button.getAttribute("role") === "tab") {
      button.setAttribute("aria-selected", String(button.dataset.tab === name));
      button.tabIndex = button.dataset.tab === name ? 0 : -1;
    }
  }
  for (const panel of root.querySelectorAll(`[data-panel="${group}"]`)) {
    panel.hidden = panel.dataset.name !== name;
  }
  root.dispatchEvent(new CustomEvent("tabchange", { detail: { group, name } }));
}

function bindTabs(group, root = document) {
  for (const button of root.querySelectorAll(`[data-tabs="${group}"] button`)) {
    button.addEventListener("click", () => activateTab(group, button.dataset.tab, root));
  }
}

// ---------- Authentication (browser-approved device login) ----------

function setToken(token) {
  state.token = token;
  state.userId = null;
  state.scope = "";
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  clearActiveWorkspace();
}

function authReady() {
  return Boolean(state.config && state.config.authentication_enabled && state.token && state.userId);
}

function workspacePreferenceKey() { return `fimbox-workspace:v1:${location.origin}:${state.userId}`; }

function rememberedWorkspacesKey() { return `fimbox-workspaces:v1:${location.origin}:${state.userId}`; }

function loadRememberedWorkspaces() {
  state.workspaces = [];
  try {
    const saved = JSON.parse(localStorage.getItem(rememberedWorkspacesKey()) || "[]");
    if (!Array.isArray(saved) || saved.some((item) =>
      !item || typeof item.location !== "string" || !item.location.startsWith("s3://")
      || typeof item.name !== "string" || !item.name.trim())) {
      throw new Error("Invalid remembered workspace list");
    }
    state.workspaces = saved;
  } catch (error) {
    showMessage(`Could not restore remembered workspaces: ${error.message}`, true);
  }
  renderRememberedWorkspaces();
}

function renderRememberedWorkspaces() {
  const list = $("#workspace-list");
  list.replaceChildren();
  if (!state.workspaces.length) {
    list.append(el("li", { className: "hint", text: "No remembered workspaces yet. Open or create one below." }));
  }
  for (const item of state.workspaces) {
    const open = el("button", { type: "button", text: "Open", onclick: () => openWorkspaceLocation(item.location, open) });
    list.append(el("li", {},
      el("div", { className: "workspace-details" }, el("strong", { text: item.name }), el("code", { text: item.location })),
      open,
      el("button", { type: "button", text: "Forget", onclick: () => forgetWorkspace(item) })));
  }
}

function rememberWorkspace(workspace) {
  const item = { name: workspace.name, location: `s3://${workspace.bucket}/${workspace.prefix}` };
  const workspaces = [item, ...state.workspaces.filter((other) => other.location !== item.location)];
  localStorage.setItem(rememberedWorkspacesKey(), JSON.stringify(workspaces));
  state.workspaces = workspaces;
  renderRememberedWorkspaces();
}

function forgetWorkspace(item) {
  try {
    const workspaces = state.workspaces.filter((other) => other.location !== item.location);
    localStorage.setItem(rememberedWorkspacesKey(), JSON.stringify(workspaces));
    if (localStorage.getItem(workspacePreferenceKey()) === item.location) {
      localStorage.removeItem(workspacePreferenceKey());
    }
    state.workspaces = workspaces;
    renderRememberedWorkspaces();
    showMessage(`Forgot ${item.name} in this browser. Shared workspace data is unchanged.`);
  } catch (error) { showMessage(`Could not forget workspace: ${error.message}`, true); }
}

function canWriteWorkspace() { return state.scope.split(/\s+/).includes("write"); }

function syncWorkspaceGate() {
  const signedIn = authReady();
  const active = signedIn && Boolean(state.workspace);
  $("#workspace-gate").hidden = !state.config || !state.config.authentication_enabled || active;
  $("#workspace-auth-required").hidden = signedIn;
  $("#workspace-context").hidden = !active;
  $("#workspace-choice").hidden = !signedIn || active;
  if (active) {
    $("#workspace-name").textContent = state.workspace.name;
    $("#workspace-name").title = state.workspace.name;
  }
  $("#main-tabs").hidden = !signedIn;
  $("#main-content").hidden = !signedIn;
  for (const tab of $("#main-tabs").querySelectorAll("button")) {
    tab.disabled = !active && tab.dataset.tab !== "input";
  }
  if (signedIn && !active) activateTab("main", "input");
  $("#workspace-create button[type=submit]").disabled = !canWriteWorkspace();
  $("#job-options button[type=submit]").disabled = !active || !canWriteWorkspace();
  $("#compare-results button[type=submit]").disabled = !active || !canWriteWorkspace();
  if (!signedIn) {
    $("#workspace-auth-required").textContent = "Sign in with HydroShare to create or open a workspace.";
  } else if (active && !canWriteWorkspace()) {
    $("#options-hint").textContent = "Read-only HydroShare access: you can inspect this workspace and existing results, but cannot create jobs.";
  }
}

function clearActiveWorkspace({ removePreference = false } = {}) {
  if (removePreference && state.userId) {
    try { localStorage.removeItem(workspacePreferenceKey()); }
    catch (error) { showMessage(`Could not clear saved workspace preference: ${error.message}`, true); }
  }
  state.workspaceVersion += 1;
  state.workspaceAction += 1;
  state.workspace = null;
  state.workspaceLocation = null;
  state.currentJob = null;
  state.scenarios = [];
  state.reachSet = [];
  flowSelection.batchLookup++;
  if ($("#timestep-content")) $("#timestep-content").replaceChildren();
  if ($("#scenario-editor")) renderScenarioEditor();
  if ($("#reach-set-editor")) renderReachSet();
  if ($("#json-text")) $("#json-text").value = "";
  if ($("#input-description")) {
    $("#input-description").textContent = "Sign in before browsing the input dataset.";
    $("#input-browser").replaceChildren();
    $("#source-browser").replaceChildren();
  }

  if ($("#compare-results")) $("#compare-results").reset();
  if ($("#reach-picker")) closeReachPicker();
  if (picker.map) {
    picker.map.remove();
    picker.map = null;
    picker.highlight = null;
    picker.reaches = null;
    picker.features.clear();
  }
  if (state.jobPoll) { clearTimeout(state.jobPoll); state.jobPoll = null; }
  state.viewId = (state.viewId || 0) + 1;
  const list = $("#job-list");
  if (list) list.replaceChildren(el("li", { className: "hint", text: "Open a workspace to list jobs." }));
  const view = $("#result-view");
  if (view) {
    const blank = view.cloneNode(false);
    view.replaceWith(blank);
    blank.replaceChildren(el("p", { className: "hint", text: "Select a job or open a workspace output." }));
  }
  syncWorkspaceGate();
}

function activateWorkspace(workspace) {
  clearActiveWorkspace();
  state.workspace = workspace;
  state.workspaceLocation = `s3://${workspace.bucket}/${workspace.prefix}`;
  try {
    rememberWorkspace(workspace);
    localStorage.setItem(workspacePreferenceKey(), state.workspaceLocation);
  }
  catch (error) { showMessage(`Workspace opened, but could not save browser preferences: ${error.message}`, true); }
  syncWorkspaceGate();
  loadInputDataset();
  loadJobs();
}

async function restoreWorkspace() {
  let saved;
  try { saved = localStorage.getItem(workspacePreferenceKey()); }
  catch (error) { showMessage(`Could not restore workspace preference: ${error.message}`, true); return; }
  if (!saved) { syncWorkspaceGate(); return; }
  const userId = state.userId;
  const action = ++state.workspaceAction;
  try {
    const workspace = await api("/workspaces/open", { method: "POST", json: { location: saved } });
    if (state.userId !== userId || !state.token || state.workspaceAction !== action) return;
    activateWorkspace(workspace);
  } catch (error) {
    if (state.userId === userId && state.token && state.workspaceAction === action) {
      if (!error.stale) showMessage(`Could not reopen saved workspace: ${error.message}. Open or create a workspace to continue.`, true);
      syncWorkspaceGate();
    }
  }
}

async function renderAuth() {
  const status = $("#auth-status");
  if (!state.config.authentication_enabled) {
    status.textContent = "Authentication is not configured";
    $("#sign-in").hidden = $("#sign-out").hidden = true;
    $("#workspace-gate").hidden = true;
    $("#main-tabs").hidden = $("#main-content").hidden = true;
    showMessage("Configuration error: browser access requires HydroShare authentication. No work can be performed in anonymous mode.", true);
    return;
  }
  if (!state.token) { showSignedOut(); return; }
  const token = state.token;
  try {
    const session = await api("/auth/session");
    if (state.token !== token) return;
    state.userId = session.user_id;
    state.scope = session.scope || "";
    status.textContent = `Signed in as HydroShare user ${session.user_id}`;
    $("#sign-in").hidden = true;
    $("#sign-out").hidden = false;
    loadRememberedWorkspaces();
    await restoreWorkspace();
  } catch {
    if (state.token === token) setToken(null);
    showSignedOut();
  }
}

function showSignedOut() {
  $("#auth-status").textContent = "Not signed in";
  $("#sign-in").hidden = false;
  $("#sign-out").hidden = true;
  state.userId = null;
  state.scope = "";
  state.workspaces = [];
  state.scenarios = [];
  state.reachSet = [];
  if ($("#scenario-editor")) renderScenarioEditor();
  if ($("#reach-set-editor")) renderReachSet();
  if ($("#json-text")) $("#json-text").value = "";
  renderRememberedWorkspaces();
  syncWorkspaceGate();
}

function switchWorkspace() {
  clearActiveWorkspace({ removePreference: true });
  syncWorkspaceGate();
  showMessage("Open or create a workspace to continue.");
}

async function openWorkspace(event) {
  event.preventDefault();
  await openWorkspaceLocation(event.target.elements.location.value.trim(), $("button[type=submit]", event.target));
}

async function openWorkspaceLocation(locationValue, button) {
  if (!authReady()) { showMessage("Sign in before opening a workspace.", true); return; }
  const userId = state.userId;
  const action = ++state.workspaceAction;
  button.disabled = true;
  try {
    const workspace = await api("/workspaces/open", { method: "POST", json: { location: locationValue } });
    if (state.workspaceAction !== action || state.userId !== userId) return;
    showMessage("");
    activateWorkspace(workspace);
  } catch (error) {
    if (state.workspaceAction === action && state.userId === userId) showMessage(`Could not open workspace: ${error.message}`, true);
  } finally { button.disabled = false; }
}

async function createWorkspace(event) {
  event.preventDefault();
  if (!authReady()) { showMessage("Sign in before creating a workspace.", true); return; }
  if (!canWriteWorkspace()) { showMessage("Creating a workspace requires HydroShare write access.", true); return; }
  const form = event.target;
  const locationValue = form.elements.location.value.trim();
  const name = form.elements.name.value.trim();
  const button = $("button[type=submit]", form);
  const userId = state.userId;
  const action = ++state.workspaceAction;
  button.disabled = true;
  try {
    const workspace = await api("/workspaces", { method: "POST", json: { location: locationValue, name } });
    if (state.workspaceAction !== action || state.userId !== userId) return;
    showMessage(`Created workspace ${workspace.name}.`);
    activateWorkspace(workspace);
    form.reset();
  } catch (error) {
    if (state.workspaceAction === action && state.userId === userId) showMessage(`Could not create workspace: ${error.message}`, true);
  } finally { button.disabled = !canWriteWorkspace(); }
}

// ---------- Workspace-relative paths and previews ----------

function relativeKeyError(value, label) {
  if (value.startsWith("/")) return `${label} must be relative (no leading /).`;
  if (value.split("/").some((part) => part === "." || part === "..")) return `${label} must not contain '.' or '..' segments.`;
  return null;
}

const previewRequests = new WeakMap();

function beginPreview(container) {
  const request = (previewRequests.get(container) || 0) + 1;
  const workspaceVersion = state.workspaceVersion;
  previewRequests.set(container, request);
  return () => container.isConnected
    && request === previewRequests.get(container)
    && workspaceVersion === state.workspaceVersion;
}

function workspaceRelativeError(value, label) {
  const error = relativeKeyError(value, label);
  if (error) return error;
  if (!value || value.split("/").some((part) => !part)) return `${label} must contain nonempty relative path segments.`;
  return null;
}

async function signIn() {
  // Open the window synchronously in the click handler so popup blockers allow it.
  const popup = window.open("", "fimbox_login");
  const button = $("#sign-in");
  button.disabled = true;
  try {
    const started = await api("/auth/device/start", { method: "POST" });
    if (popup) popup.location.href = started.verification_uri;
    else showMessage(`Open this link to sign in: ${started.verification_uri}`);
    $("#auth-status").textContent = "Waiting for HydroShare approval…";
    const deadline = Date.now() + started.expires_in * 1000;
    let interval = started.interval * 1000;
    while (Date.now() < deadline) {
      await sleep(interval);
      let poll;
      try {
        poll = await api("/auth/device/poll", { method: "POST", json: { device_code: started.device_code } });
      } catch (error) {
        if (error.status === 429) { interval = Number(error.headers.get("Retry-After") || 5) * 1000 + 500; continue; }
        throw error;
      }
      if (poll.status === "authorized") {
        setToken(poll.token.access_token);
        showMessage("Signed in.");
        await refreshAfterAuth();
        return;
      }
      if (poll.status === "denied") throw new Error("HydroShare authorization was denied");
      if (poll.status === "failed") throw new Error("HydroShare authorization failed");
    }
    throw new Error("Sign-in timed out; try again");
  } catch (error) {
    if (popup && !popup.closed) popup.close();
    showMessage(`Sign-in failed: ${error.message}`, true);
    await renderAuth();
  } finally {
    button.disabled = false;
  }
}

async function signOut() {
  try { await api("/auth/logout", { method: "POST" }); } catch { /* session may already be gone */ }
  setToken(null);
  showMessage("Signed out.");
  await refreshAfterAuth();
}

async function refreshAfterAuth() {
  await renderAuth();
  if (authReady()) {
    loadInputDataset();
    if (state.workspace) loadJobs();
  }
}

// ---------- Markdown (subset used by generated output READMEs) ----------

function renderInline(text, linkHandler) {
  const nodes = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    if (match[1]) nodes.push(el("code", { text: match[1].slice(1, -1) }));
    else if (match[2]) nodes.push(el("strong", { text: match[2].slice(2, -2) }));
    else {
      const [label, target] = [match[3], match[4]];
      if (/^https?:\/\//i.test(target)) {
        nodes.push(el("a", { href: target, target: "_blank", rel: "noopener noreferrer", text: label }));
      } else if (linkHandler) {
        nodes.push(el("button", { type: "button", className: "link", text: label, onclick: () => linkHandler(target) }));
      } else {
        nodes.push(label);
      }
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderMarkdown(markdown, linkHandler) {
  const root = el("div");
  const lines = markdown.split(/\r?\n/);
  let list = null, paragraph = [];
  const flush = () => {
    if (paragraph.length) root.append(el("p", {}, renderInline(paragraph.join(" "), linkHandler)));
    paragraph = [];
    list = null;
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flush();
      const code = [];
      for (i += 1; i < lines.length && !lines[i].startsWith("```"); i += 1) code.push(lines[i]);
      root.append(el("pre", {}, el("code", { text: code.join("\n") })));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      root.append(el(`h${Math.min(heading[1].length + 2, 6)}`, {}, renderInline(heading[2], linkHandler)));
    } else if (item) {
      if (paragraph.length) { const saved = list; flush(); list = saved; }
      if (!list) { list = el("ul"); root.append(list); }
      list.append(el("li", {}, renderInline(item[1], linkHandler)));
    } else if (!line.trim()) {
      flush();
    } else {
      paragraph.push(line.trim());
    }
  }
  flush();
  return root;
}

// ---------- File browser ----------

async function downloadObject(bucket, key) {
  try {
    const link = await api(`/ui/objects/download?${query({ bucket, key })}`);
    const anchor = el("a", { href: link.url, rel: "noopener noreferrer" });
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } catch (error) {
    showMessage(`Download failed: ${error.message}`, true);
  }
}

async function previewObject(container, bucket, key, linkHandler) {
  const current = beginPreview(container);
  container.replaceChildren(el("p", { className: "hint", text: `Loading ${key}…` }));
  try {
    const preview = await api(`/ui/objects/preview?${query({ bucket, key })}`);
    if (!current()) return;
    let body;
    if (key.toLowerCase().endsWith(".md")) {
      body = renderMarkdown(preview.text, linkHandler);
      body.className = "readme";
    } else {
      let text = preview.text;
      if (!preview.truncated && /\.(geo)?json$/i.test(key)) {
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* show raw text */ }
      }
      body = el("pre", { text });
    }
    container.replaceChildren(
      el("h4", {}, el("span", { text: key.split("/").pop() }),
        el("span", {},
          preview.truncated ? el("span", { className: "hint", text: `first ${formatBytes(preview.text.length)} of ${formatBytes(preview.size_bytes)} ` }) : null,
          el("button", { type: "button", className: "small", text: "Download", onclick: () => downloadObject(bucket, key) }))),
      body,
    );
  } catch (error) {
    if (!current()) return;
    container.replaceChildren(el("p", { className: "message error", text: `Preview failed: ${error.message}` }));
  }
}

async function previewParquet(container, bucket, key) {
  const current = beginPreview(container);
  container.replaceChildren(el("p", { className: "hint", text: `Loading ${key}…` }));
  try {
    const preview = await api(`/ui/objects/parquet?${query({ bucket, key, limit: 100 })}`);
    if (!current()) return;
    const summary = `${preview.num_rows.toLocaleString()} rows · ${preview.columns.length} columns · `
      + `${preview.num_row_groups} row group${preview.num_row_groups === 1 ? "" : "s"} · ${formatBytes(preview.size_bytes)}`;
    const schema = el("details", {},
      el("summary", { text: "Schema" }),
      el("table", {}, el("tbody", {}, ...preview.columns.map((column) =>
        el("tr", {}, el("td", { text: column.name }), el("td", { className: "hint", text: column.type }))))));
    const rows = el("table", {},
      el("thead", {}, el("tr", {}, ...preview.columns.map((column) => el("th", { text: column.name, title: column.type })))),
      el("tbody", {}, ...preview.rows.map((row) =>
        el("tr", {}, ...row.map((cell) => el("td", { text: cell ?? "null", className: cell === null ? "hint" : "" }))))));
    container.replaceChildren(
      el("h4", {}, el("span", { text: key.split("/").pop() }),
        el("span", {},
          preview.truncated ? el("span", { className: "hint", text: `first ${preview.rows.length} rows ` }) : null,
          el("button", { type: "button", className: "small", text: "Download", onclick: () => downloadObject(bucket, key) }))),
      el("div", { className: "parquet" }, el("p", { className: "hint", text: summary }), schema, el("div", { className: "table-scroll" }, rows)),
    );
  } catch (error) {
    if (!current()) return;
    container.replaceChildren(el("p", { className: "message error", text: `Preview failed: ${error.message}` }));
  }
}

async function previewRaster(container, bucket, key) {
  const current = beginPreview(container);
  const loading = el("p", { className: "hint", text: `Loading ${key}…` });
  container.replaceChildren(loading);
  try {
    const response = await api(`/ui/objects/raster?${query({ bucket, key })}`, { raw: true });
    const blob = await response.blob();
    if (!current() || !container.contains(loading)) return;
    const url = URL.createObjectURL(blob);
    const image = el("img", { src: url, alt: `First-band preview of ${key}`,
      onload: () => URL.revokeObjectURL(url),
      onerror: () => {
        URL.revokeObjectURL(url);
        image.replaceWith(el("p", { className: "message error", text: "Could not display raster preview." }));
      } });
    image.style.maxWidth = "100%";
    container.replaceChildren(
      el("h4", {}, el("span", { text: key.split("/").pop() }),
        el("button", { type: "button", className: "small", text: "Download", onclick: () => downloadObject(bucket, key) })),
      el("p", { className: "hint", text: "Band 1 · downsampled to at most 1024 pixels per side · grayscale stretched to sampled values (2–98 percentiles). Nodata is transparent. This is an image preview, not a georeferenced map." }),
      image,
    );
  } catch (error) {
    if (current() && container.contains(loading)) {
      container.replaceChildren(el("p", { className: "message error", text: `Preview failed: ${error.message}` }));
    }
  }
}

function createBrowser(container, { bucket, root, onRaster, linkHandler }) {
  const workspaceVersion = state.workspaceVersion;
  let listingRequest = 0;
  const crumbs = el("div", { className: "breadcrumb" });
  const table = el("table");
  const more = el("div", { className: "row" });
  const preview = el("div", { className: "preview", hidden: true });
  container.replaceChildren(crumbs, el("div", { className: "browser-scroll" }, table), more, preview);
  const rootPrefix = root ? joinKey(root) + "/" : "";

  function renderCrumbs(prefix) {
    const parts = prefix.slice(rootPrefix.length).split("/").filter(Boolean);
    const label = rootPrefix ? `${bucket}/${rootPrefix}` : `${bucket}/`;
    crumbs.replaceChildren(el("button", { type: "button", text: label, onclick: () => open(rootPrefix) }));
    let path = rootPrefix;
    for (const part of parts) {
      path += part + "/";
      const target = path;
      crumbs.append(" / ", el("button", { type: "button", text: part, onclick: () => open(target) }));
    }
  }

  function fileRow(file) {
    const actions = el("td");
    if (hasSuffix(file.name, PREVIEW_SUFFIXES)) {
      actions.append(el("button", { type: "button", className: "small", text: "View", onclick: () => {
        preview.hidden = false;
        previewObject(preview, bucket, file.key, linkHandler);
      } }), " ");
    } else if (hasSuffix(file.name, [".parquet"])) {
      actions.append(el("button", { type: "button", className: "small", text: "View", onclick: () => {
        preview.hidden = false;
        previewParquet(preview, bucket, file.key);
      } }), " ");
    } else if (hasSuffix(file.name, RASTER_SUFFIXES)) {
      actions.append(el("button", { type: "button", className: "small", text: "View", onclick: () => {
        preview.hidden = false;
        previewRaster(preview, bucket, file.key);
      } }), " ");
    }
    if (onRaster && (hasSuffix(file.name, RASTER_SUFFIXES) || file.name === "manifest.json")) {
      actions.append(el("button", { type: "button", className: "small", text: "Map", onclick: () => onRaster(file.key) }), " ");
    }
    actions.append(el("button", { type: "button", className: "small", text: "Download", onclick: () => downloadObject(bucket, file.key) }));
    return el("tr", {},
      el("td", { text: "📄 " + file.name }),
      el("td", { className: "num", text: formatBytes(file.size_bytes) }),
      el("td", { text: file.last_modified ? new Date(file.last_modified).toLocaleString() : "" }),
      actions);
  }

  async function open(prefix, token = null) {
    const request = ++listingRequest;
    if (!token) {
      renderCrumbs(prefix);
      table.replaceChildren(el("tr", {}, el("td", { className: "hint", text: "Loading…" })));
    }
    more.replaceChildren();
    try {
      const listing = await api(`/ui/objects?${query({ bucket, prefix, token })}`);
      if (workspaceVersion !== state.workspaceVersion || request !== listingRequest || !container.isConnected) return;
      if (!token) {
        table.replaceChildren(el("tr", {}, el("th", { text: "Name" }), el("th", { text: "Size" }),
          el("th", { text: "Modified" }), el("th", { text: "" })));
        if (prefix !== rootPrefix) {
          const parent = prefix.slice(0, prefix.slice(0, -1).lastIndexOf("/") + 1);
          table.append(el("tr", {}, el("td", { colSpan: 4 },
            el("button", { type: "button", className: "link", text: "⬆ ..", onclick: () => open(parent.length < rootPrefix.length ? rootPrefix : parent) }))));
        }
      }
      for (const folder of listing.folders) {
        table.append(el("tr", {}, el("td", { colSpan: 4 },
          el("button", { type: "button", className: "link", text: "📁 " + folder.name + "/", onclick: () => open(folder.prefix) }))));
      }
      for (const file of listing.files) table.append(fileRow(file));
      if (!token && !listing.folders.length && !listing.files.length) {
        table.append(el("tr", {}, el("td", { colSpan: 4, className: "hint", text: "Empty folder" })));
      }
      if (listing.next_token) {
        more.append(el("button", { type: "button", text: "Load more", onclick: () => open(prefix, listing.next_token) }));
      }
    } catch (error) {
      if (workspaceVersion !== state.workspaceVersion || request !== listingRequest || !container.isConnected) return;
      table.replaceChildren(el("tr", {}, el("td", { className: "message error", text: `Listing failed: ${error.message}` })));
    }
  }

  open(rootPrefix);
  return { open, preview: (key) => { preview.hidden = false; previewObject(preview, bucket, key, linkHandler); } };
}

// ---------- Input dataset ----------

function loadInputDataset() {
  const description = $("#input-description");
  const container = $("#input-browser");
  loadSourceBrowser();
  if (!authReady()) {
    description.textContent = "Sign in with HydroShare to browse input datasets.";
    container.replaceChildren();
    return;
  }
  const dataset = state.config.input_dataset;
  if (!dataset) {
    description.textContent = "This server has no prepared streaming hydrofabric configured. Legacy jobs download "
      + "NOAA OWP HAND data per HUC from the public ciroh-owp-hand-fim bucket during the run.";
    container.replaceChildren();
    return;
  }
  description.textContent = `Prepared cloud-optimized HAND hydrofabric read by streaming jobs: `
    + `s3://${dataset.bucket}/${dataset.prefix} (endpoint ${dataset.endpoint}). It holds dataset.parquet, per-HUC metadata (hucs/<huc>/huc.parquet), `
    + `and a per-HUC reach rating Parquet file (hucs/<huc>/reaches.parquet). Rasters are read in place from the NOAA source files.`;
  if (!dataset.unsigned && !authReady()) {
    container.replaceChildren(el("p", { className: "hint", text: "Sign in with HydroShare to browse this dataset." }));
    return;
  }
  if (!dataset.unsigned && !state.config.authentication_enabled) {
    container.replaceChildren(el("p", { className: "hint", text: "Browsing requires HydroShare authentication to be configured on this server." }));
    return;
  }
  createBrowser(container, { bucket: dataset.bucket, root: dataset.prefix });
}

function loadSourceBrowser() {
  const container = $("#source-browser");
  if (!authReady()) {
    $("#source-description").textContent = "";
    container.replaceChildren();
    return;
  }
  const bucket = state.config.source_bucket;
  $("#source-description").textContent = `Public NOAA/OWP HAND-FIM source data in s3://${bucket}/ (endpoint ${state.config.source_endpoint}). The prepared dataset references these rasters in place.`;
  createBrowser(container, { bucket, root: "" }).open(`hand_fim_${DEFAULT_SOURCE_VERSION.replaceAll(".", "_")}/`);
}

// ---------- Scenario editor ----------

function refreshTimestepSelection() {
  if ($("#timestep-panel")?.hidden === false) openFlowSelection();
}

function renderReachSet(scrollToEnd = false) {
  const editor = $("#reach-set-editor");
  editor.replaceChildren();
  refreshTimestepSelection();
  if (picker.map) refreshReachMap();
  if (!state.reachSet.length) {
    editor.append(el("p", { className: "hint", text: "No reaches in the set yet." }));
    return;
  }
  const rows = state.reachSet.map((reach, index) => el("tr", {},
    el("td", {}, el("input", { value: reach.HUC, maxLength: 8, size: 9, pattern: "\\d{8}", required: true,
      "aria-label": `HUC8 for reach ${index + 1}`,
      oninput: (event) => {
        reach.HUC = event.target.value.trim();
        refreshTimestepSelection();
      } })),
    el("td", {}, el("input", { value: reach.ReachID, size: 12, required: true, "aria-label": `ReachID ${index + 1}`,
      oninput: (event) => {
        reach.ReachID = event.target.value.trim();
        refreshTimestepSelection();
      },
      onchange: () => { if (picker.map) refreshReachMap(); } })),
    el("td", {},
      el("button", { type: "button", className: "small", text: "View on map",
        onclick: () => openReachPicker(reach.ReachID) }),
      " ",
      el("button", { type: "button", className: "small", text: "Remove", onclick: () => {
        state.reachSet.splice(index, 1);
        renderReachSet();
      } }))));
  editor.append(el("table", {},
    el("tr", {}, el("th", { text: "HUC (8 digits)" }), el("th", { text: "NWM ReachID" }),
      el("th")),
    rows));
  if (scrollToEnd) editor.scrollTop = editor.scrollHeight;
}

function renderScenarioEditor() {
  const editor = $("#scenario-editor");
  editor.replaceChildren();
  state.scenarios.forEach((scenario, index) => {
    const rows = scenario.Reaches.map((reach, reachIndex) => el("tr", {},
      el("td", { text: reach.HUC }),
      el("td", { text: reach.ReachID }),
      el("td", {}, el("input", {
        type: "number", className: "num", min: 0, step: "any", required: true,
        value: reach.Streamflow,
        "aria-label": `Streamflow for ${scenario.ScenarioID}, reach ${reachIndex + 1} (m³/s)`,
        oninput: (event) => {
          reach.Streamflow = event.target.value === "" ? "" : Number(event.target.value);
          $("#json-text").value = JSON.stringify(state.scenarios, null, 2);
        },
      }))));
    editor.append(el("fieldset", {},
      el("legend", {}, "Scenario ",
        el("span", { text: scenario.ScenarioID }),
        el("button", { type: "button", className: "small", text: "Select streamflow",
          onclick: () => openFlowSelection(scenario) }),
        el("button", { type: "button", className: "small", text: "Remove scenario", onclick: () => {
          state.scenarios.splice(index, 1);
          renderScenarioEditor();
        } })),
      el("table", {},
        el("tr", {}, el("th", { text: "HUC (8 digits)" }), el("th", { text: "NWM ReachID" }),
          el("th", { text: "Streamflow (m³/s)" })),
        rows)));
  });
  if (!state.scenarios.length) editor.append(el("p", { className: "hint", text: "No scenarios yet. Load an example or add one from the reach set above." }));
  $("#json-text").value = JSON.stringify(state.scenarios, null, 2);
}

function setScenarios(scenarios) {
  if (!Array.isArray(scenarios)) throw new Error("Scenario JSON must be an array");
  closeReachPicker();
  closeFlowSelection();
  state.scenarios = scenarios.map((scenario) => ({
    ScenarioID: String(scenario.ScenarioID ?? ""),
    Reaches: (scenario.Reaches || []).map((reach) => ({
      HUC: String(reach.HUC ?? ""), ReachID: String(reach.ReachID ?? ""), Streamflow: reach.Streamflow,
    })),
  }));
  renderScenarioEditor();
}

// ---------- Reach picker ----------

const NLDI_COMID_URL = "https://api.water.usgs.gov/nldi/linked-data/comid";
const WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer";
const NHDPLUS_URL = "https://watersgeo.epa.gov/arcgis/rest/services/NHDPlus/NHDPlus/MapServer";
const picker = { map: null, highlight: null, reaches: null, features: new Map(), lookup: 0, render: 0 };
const flowSelection = { lookup: 0, batchLookup: 0 };

function osmLayer() {
  return L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
    // OSM blocks tile requests without a Referer; send only the origin, not the page path.
    referrerPolicy: "strict-origin",
  });
}

// Web-mercator tiles rendered by an ArcGIS MapServer's export operation.
function arcgisExportLayer(url, layers, options) {
  const Layer = L.TileLayer.extend({
    getTileUrl(coords) {
      const extent = 20037508.342789244;
      const span = (2 * extent) / 2 ** coords.z;
      const west = -extent + coords.x * span;
      const north = extent - coords.y * span;
      return `${url}/export?${query({
        bbox: `${west},${north - span},${west + span},${north}`, bboxSR: 3857, imageSR: 3857,
        size: "256,256", layers: `show:${layers}`, format: "png32", transparent: true, f: "image",
      })}`;
    },
  });
  return new Layer("", options);
}

// HUC8 boundary lines drawn as one export image of the visible map. The WBD service's HUC8
// polygon layer is scale-limited to ~zoom 8-9 and its server intermittently answers bursts of
// tile requests with 503, so a single image per view (with retries) renders far more reliably.
function hucBoundaryLayer() {
  const Layer = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      this._request = 0;
      this._timer = null;
      map.on("moveend", this._schedule, this);
      this._schedule();
    },
    onRemove(map) {
      map.off("moveend", this._schedule, this);
      clearTimeout(this._timer);
      this._request++;
      if (this._overlay) { this._overlay.remove(); this._overlay = null; }
    },
    getAttribution() { return "USGS WBD"; },
    _schedule() {
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._load(++this._request, 0), 250);
    },
    _load(request, attempt) {
      const map = this._map;
      if (!map || request !== this._request) return;
      const bounds = map.getBounds();
      const size = map.getSize();
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const southWest = L.CRS.EPSG3857.project(bounds.getSouthWest());
      const northEast = L.CRS.EPSG3857.project(bounds.getNorthEast());
      const url = `${WBD_URL}/export?${query({
        bbox: [southWest.x, southWest.y, northEast.x, northEast.y].map((value) => value.toFixed(2)).join(","),
        bboxSR: 3857, imageSR: 3857, size: `${Math.round(size.x * ratio)},${Math.round(size.y * ratio)}`,
        dpi: Math.round(96 * ratio), layers: "show:0",
        layerDefs: JSON.stringify({ 0: "hudigit <= 8" }), format: "png32", transparent: true, f: "image",
        attempt: attempt || undefined,
      })}`;
      const image = new Image();
      image.referrerPolicy = "no-referrer";
      image.onload = () => {
        if (request !== this._request || !this._map) return;
        const overlay = L.imageOverlay(image, bounds, { opacity: 0.9, interactive: false }).addTo(map);
        if (this._overlay) this._overlay.remove();
        this._overlay = overlay;
      };
      image.onerror = () => {
        if (request === this._request && attempt < 3) {
          this._timer = setTimeout(() => this._load(request, attempt + 1), 1000 * 2 ** attempt);
        }
      };
      image.src = url;
    },
  });
  return new Layer();
}

async function fetchPublicJson(url) {
  const response = await fetch(url, { credentials: "omit", referrerPolicy: "no-referrer" });
  if (!response.ok) throw new Error(`${new URL(url).host} returned HTTP ${response.status}`);
  const body = await response.json();
  if (body.error) throw new Error(`${new URL(url).host}: ${body.error.message || "request failed"}`);
  return body;
}

async function lookupReach(latlng) {
  const lng = latlng.lng.toFixed(6);
  const lat = latlng.lat.toFixed(6);
  const [reaches, hucs] = await Promise.all([
    fetchPublicJson(`${NLDI_COMID_URL}/position?${query({ coords: `POINT(${lng} ${lat})` })}`),
    fetchPublicJson(`${WBD_URL}/4/query?${query({
      geometry: `${lng},${lat}`, geometryType: "esriGeometryPoint", inSR: 4326,
      spatialRel: "esriSpatialRelIntersects", outFields: "huc8,name", returnGeometry: false, f: "json",
    })}`),
  ]);
  const feature = reaches.features?.[0];
  const huc = hucs.features?.[0]?.attributes;
  if (!feature) throw new Error("No NHDPlus reach was found at this location.");
  if (!huc) throw new Error("No HUC8 was found at this location.");
  return { reachId: String(feature.properties.comid), huc: huc.huc8, hucName: huc.name, feature };
}

function highlightReach(feature, fit) {
  if (picker.highlight) picker.highlight.remove();
  picker.highlight = L.geoJSON(feature, {
    interactive: false,
    style: { color: "#d62728", weight: 5 },
    pointToLayer: (item, latlng) => L.circleMarker(latlng, {
      radius: 8, color: "#d62728", fillOpacity: 0.8, interactive: false,
    }),
  }).addTo(picker.map);
  if (fit) picker.map.fitBounds(picker.highlight.getBounds(), { maxZoom: 14 });
}

function ensurePickerMap() {
  if (picker.map) return picker.map;
  const map = L.map($("#picker-map"), { minZoom: 3 });
  osmLayer().addTo(map);
  const hucs = hucBoundaryLayer().addTo(map);
  const flowlines = arcgisExportLayer(NHDPLUS_URL, 2, { attribution: "EPA NHDPlus v2.1", minZoom: 11 }).addTo(map);
  L.control.layers(null, { "HUC8 boundaries": hucs, "NHDPlus flowlines": flowlines }, { collapsed: false }).addTo(map);
  map.setView([39, -98], 4);
  map.on("click", (event) => pickReachAt(event.latlng));
  picker.map = map;
  picker.reaches = L.geoJSON(null, {
    style: { color: "#1d5fa8", weight: 5 },
    pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
      radius: 7, color: "#1d5fa8", fillOpacity: 0.8,
    }),
    onEachFeature: (feature, layer) => layer.bindTooltip(el("span", {
      text: `Reach ${feature.properties.comid}`,
    })),
  }).addTo(map);
  return map;
}

async function refreshReachMap(fit = false, focus = null) {
  if (!picker.map || !picker.reaches) return;
  const request = ++picker.render;
  const map = picker.map;
  const workspaceVersion = state.workspaceVersion;
  const signature = JSON.stringify(state.reachSet.map((reach) => reach.ReachID));
  const ids = [...new Set(state.reachSet.map((reach) => String(reach.ReachID).trim()).filter((id) => /^\d+$/.test(id)))];
  picker.reaches.clearLayers();
  const status = $("#picker-status");
  status.className = "hint";
  status.textContent = ids.length ? "Loading reach set locations…" : "Add reaches manually or click the map to build the reach set.";
  if (!ids.length) return;
  const failures = [];
  for (const id of ids) {
    let feature = picker.features.get(id);
    try {
      if (!feature) {
        const body = await fetchPublicJson(`${NLDI_COMID_URL}/${encodeURIComponent(id)}`);
        feature = body.features?.[0];
        if (!feature) throw new Error("not found in NHDPlus");
      }
    } catch (error) {
      failures.push(`Reach ${id}: ${error.message}`);
    }
    if (request !== picker.render || map !== picker.map || workspaceVersion !== state.workspaceVersion
        || signature !== JSON.stringify(state.reachSet.map((reach) => reach.ReachID))) return;
    if (feature) {
      picker.features.set(id, feature);
      picker.reaches.addData(feature);
      if (focus === id) {
        highlightReach(feature, true);
      }
    }
  }
  const bounds = picker.reaches.getBounds();
  if (fit && bounds.isValid()) map.fitBounds(bounds, { maxZoom: 14 });
  if (picker.highlight) picker.highlight.bringToFront();
  status.textContent = `${ids.length - failures.length} of ${state.reachSet.length} reach-set rows shown.`
    + (failures.length ? ` ${failures.join("; ")}` : "");
  status.className = failures.length ? "error" : "hint";
}

// ---------- Reach streamflow lookups ----------

const NWPS_REACHES_URL = "https://api.water.noaa.gov/nwps/v1/reaches";
const NWPS_SERIES = [
  {
    key: "analysisAssimilation", param: null, label: "Recent analysis",
    description: "NOAA National Water Model analysis and assimilation, delivered by the National Water Prediction Service (NWPS). "
      + "Modeled streamflow informed by observations; not a direct stream-gauge measurement.",
    window: "Recent conditions in the rolling NWPS record. The exact window and time resolution are shown after loading.",
  },
  {
    key: "shortRange", param: "short_range", label: "Short-range forecast",
    description: "NOAA National Water Model short-range deterministic forecast, delivered by NWPS.",
    window: "Up to 18 hours ahead for CONUS. The available window depends on the latest published run.",
  },
  {
    key: "mediumRangeBlend", param: "medium_range_blend", label: "Medium-range blend forecast",
    description: "NOAA National Water Model medium-range deterministic blend forecast, delivered by NWPS. "
      + "Uses National Blend of Models precipitation and Global Forecast System forcing.",
    window: "Up to 10 days ahead for CONUS. The available window depends on the latest published run.",
  },
  {
    key: "longRange", param: "long_range", label: "Long-range forecast",
    description: "NOAA National Water Model long-range ensemble forecast, delivered by NWPS. "
      + "FIMBox uses the published mean when supplied, rather than choosing an individual ensemble member.",
    window: "Up to 30 days ahead for CONUS. The available window depends on the latest published run.",
  },
];
const FT3_TO_M3 = 0.0283168466;
const RETROSPECTIVE_RANGE = { min: "1979-02-01", max: "2023-02-01" };

function roundFlow(flow) { return Number(flow.toFixed(3)); }

// Ensemble forecasts (medium/long range) publish members plus a "mean"; others publish "series".
function nwpsPoints(section) {
  const series = section?.series ?? section?.mean;
  if (!Array.isArray(series?.data)) return [];
  const factor = /ft/.test(series.units || "") ? FT3_TO_M3 : 1;
  return series.data
    .filter((point) => Number.isFinite(point.flow) && point.flow >= 0)
    .map((point) => ({ time: point.validTime, flow: point.flow * factor }));
}

const MAX_TIMESTEP_SCENARIOS = 240;
const TIMESTEP_ID = /^\d{8}T\d{4}Z$/;
const HOUR_MS = 3600 * 1000;

// datetime-local inputs are read and written as UTC.
function utcInputValue(time) { return new Date(time).toISOString().slice(0, 16); }

function parseUtcInput(value) { return value ? Date.parse(`${value}:00Z`) : NaN; }

function timestepId(time) { return `${new Date(time).toISOString().slice(0, 16).replace(/[-:]/g, "")}Z`; }

function sharedTimesteps(reaches, series) {
  const pointMaps = [];
  for (const reach of reaches) {
    const points = series.get(reach);
    if (!points) return [];
    const byTime = new Map();
    for (const point of points) {
      const time = Date.parse(point.time);
      if (Number.isFinite(time) && Number.isFinite(point.flow) && point.flow >= 0) {
        byTime.set(time, point);
      }
    }
    pointMaps.push(byTime);
  }
  if (!pointMaps.length) return [];
  return [...pointMaps[0].keys()].filter((time) => pointMaps.every((points) => points.has(time)))
    .sort((a, b) => a - b)
    .map((time) => ({
      time: new Date(time).toISOString(),
      reaches: reaches.map((reach, index) => ({ target: reach, point: pointMaps[index].get(time) })),
    }));
}

function renderTimestepBatch(selected, series, isCurrent) {
  if (!selected.length) return el("div");
  const available = sharedTimesteps(selected, series);
  if (!available.length) {
    return el("p", { className: "hint", text: "No exact UTC timestamps are shared by every reach in the set. Choose different sources or time ranges." });
  }

  const start = el("input", {
    type: "datetime-local", step: 3600, value: utcInputValue(Date.parse(available[0].time)),
    min: utcInputValue(Date.parse(available[0].time)), max: utcInputValue(Date.parse(available.at(-1).time)),
    "aria-label": "First shared time (UTC)",
  });
  const end = el("input", {
    type: "datetime-local", step: 3600, value: utcInputValue(Date.parse(available.at(-1).time)),
    min: utcInputValue(Date.parse(available[0].time)), max: utcInputValue(Date.parse(available.at(-1).time)),
    "aria-label": "Last shared time (UTC)",
  });
  const every = el("select", { "aria-label": "Shared timestep interval" },
    [1, 3, 6, 12, 24].map((hours) => el("option", { value: hours, text: `every ${hours} h` })));
  const count = el("span", { className: "hint" });
  const create = el("button", { type: "button", className: "small", text: "Build timestep scenarios" });
  const chosen = () => {
    const first = parseUtcInput(start.value);
    const last = parseUtcInput(end.value);
    const stepMs = Number(every.value) * HOUR_MS;
    if (!(first <= last)) return [];
    return available.filter((item) => {
      const time = Date.parse(item.time);
      return time >= first && time <= last && (time - first) % stepMs === 0;
    });
  };
  const update = () => {
    const total = chosen().length;
    create.disabled = !total || total > MAX_TIMESTEP_SCENARIOS;
    count.textContent = total > MAX_TIMESTEP_SCENARIOS
      ? `${total} timesteps; choose at most ${MAX_TIMESTEP_SCENARIOS} (narrow the range or use a longer step).`
      : `${total} shared timestep(s), with ${selected.length} reaches in each scenario.`;
  };
  [start, end, every].forEach((input) => input.addEventListener("input", update));
  create.addEventListener("click", () => {
    const times = chosen();
    if (!isCurrent()) { showMessage("The reach set changed. Load streamflow again.", true); return; }
    if (times.length && times.length <= MAX_TIMESTEP_SCENARIOS) applyGroupedTimesteps(times, selected, series);
  });
  update();
  return el("fieldset", { className: "timestep-batch" },
    el("legend", { text: "Build scenarios from reach timesteps" }),
    el("p", { className: "hint", text: "Only exact UTC timestamps present for every reach in the set are used; missing samples are not interpolated." }),
    el("div", { className: "row" },
      el("label", { text: "From (UTC) " }, start), el("label", { text: "to " }, end), every, create, count));
}

function applyGroupedTimesteps(chosen, selected, series) {
  if (!selected.length || chosen.length === 0 || chosen.length > MAX_TIMESTEP_SCENARIOS) return;
  const ids = selected.map((reach) => String(reach.ReachID).trim());
  if (ids.some((id) => !/^\d+$/.test(id)) || new Set(ids).size !== ids.length
      || selected.some((reach) => !/^\d{8}$/.test(String(reach.HUC).trim()))) {
    showMessage("Every reach must have a unique numeric ReachID and 8-digit HUC.", true);
    return;
  }
  const available = new Map(sharedTimesteps(selected, series).map((item) => [item.time, item]));
  const grouped = chosen.map((item) => available.get(item.time));
  if (grouped.some((item) => !item)) {
    showMessage("The selected shared timesteps changed. Review the timestep batch and try again.", true);
    return;
  }

  for (const item of grouped) {
    const id = timestepId(item.time);
    const scenario = {
      ScenarioID: id,
      Reaches: item.reaches.map(({ target, point }) => ({
        HUC: String(target.HUC).trim(),
        ReachID: String(target.ReachID).trim(),
        Streamflow: roundFlow(point.flow),
      })),
    };
    const existing = state.scenarios.findIndex((candidate) => candidate.ScenarioID === id);
    if (existing >= 0) state.scenarios[existing] = scenario;
    else state.scenarios.push(scenario);
  }
  const others = state.scenarios.filter((scenario) => !TIMESTEP_ID.test(scenario.ScenarioID));
  const steps = state.scenarios.filter((scenario) => TIMESTEP_ID.test(scenario.ScenarioID))
    .sort((a, b) => a.ScenarioID.localeCompare(b.ScenarioID));
  state.scenarios.splice(0, state.scenarios.length, ...others, ...steps);
  renderScenarioEditor();
  activateTab("reach-tools", "scenarios");
  showMessage(`Built ${grouped.length} timestep scenario(s), each with ${selected.length} reaches.`);
}

function addScenarioFromReachSet() {
  const reaches = state.reachSet;
  if (!reaches.length) {
    showMessage("Add at least one reach to the reach set first.", true);
    return;
  }
  const ids = reaches.map((reach) => String(reach.ReachID).trim());
  if (ids.some((id) => !/^\d+$/.test(id)) || new Set(ids).size !== ids.length
      || reaches.some((reach) => !/^\d{8}$/.test(String(reach.HUC).trim()))) {
    showMessage("Every reach needs a unique numeric ReachID and an 8-digit HUC.", true);
    return;
  }
  let suffix = state.scenarios.length + 1;
  let id = `scenario-${suffix}`;
  while (state.scenarios.some((scenario) => scenario.ScenarioID === id)) {
    suffix += 1;
    id = `scenario-${suffix}`;
  }
  state.scenarios.push({
    ScenarioID: id,
    Reaches: reaches.map((reach) => ({
      HUC: String(reach.HUC).trim(),
      ReachID: String(reach.ReachID).trim(),
      Streamflow: "",
    })),
  });
  renderScenarioEditor();
  showMessage(`Added ${id} with all ${reaches.length} reaches.`);
}

function flowCoverage(points) {
  const times = points.map((point) => Date.parse(point.time));
  const intervals = [...new Set(times.slice(1).map((time, index) => (time - times[index]) / 60000))];
  const resolution = !intervals.length ? "One sample; time resolution unavailable"
    : intervals.length === 1 ? `${intervals[0]} minutes between samples`
      : `Variable spacing (${Math.min(...intervals)}–${Math.max(...intervals)} minutes between samples)`;
  return el("dl", { className: "summary" },
    el("dt", { text: "Available times (UTC)" }),
    el("dd", { text: `${utcInputValue(times[0]).replace("T", " ")} to ${utcInputValue(times[times.length - 1]).replace("T", " ")}` }),
    el("dt", { text: "Returned time resolution" }),
    el("dd", { text: `${resolution}; ${points.length} sample(s). Missing samples are not interpolated.` }));
}

function groupedFlowCoverage(reaches, series) {
  const groups = new Map();
  for (const reach of reaches) {
    const times = [...new Set(series.get(reach).map((point) => Date.parse(point.time)))].sort((a, b) => a - b);
    const key = times.join(",");
    if (!groups.has(key)) groups.set(key, { times, reaches: [] });
    groups.get(key).reaches.push(reach);
  }
  return [...groups.values()].map((group, index) => el("div", {},
    el("h4", { text: `Sample set ${index + 1} (${group.reaches.length} reach${group.reaches.length === 1 ? "" : "es"})` }),
    flowCoverage(group.times.map((time) => ({ time: new Date(time).toISOString() }))),
    el("p", { text: group.reaches.map((reach) => `Reach ${reach.ReachID} (HUC ${reach.HUC})`).join(", ") })));
}

async function fetchReachFlows(reach, source, start, end) {
  let points;
  if (source === "retrospective") {
    const body = await api(`/ui/streamflow/retrospective?${query({ reach_id: reach.ReachID, start, end })}`);
    const first = Date.parse(body.start);
    points = body.flows.map((flow, index) => ({
      time: new Date(first + index * HOUR_MS).toISOString(), flow,
    }));
  } else {
    const series = NWPS_SERIES.find((item) => item.key === source);
    if (!series) throw new Error("Choose a supported streamflow source.");
    const base = `${NWPS_REACHES_URL}/${encodeURIComponent(reach.ReachID)}/streamflow`;
    const body = await fetchPublicJson(series.param ? `${base}?${query({ series: series.param })}` : base);
    points = nwpsPoints(body[series.key]);
  }
  points = points.filter((point) => Number.isFinite(Date.parse(point.time))
    && Number.isFinite(point.flow) && point.flow >= 0)
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  if (!points.length) throw new Error(`No streamflow samples are available for reach ${reach.ReachID}. Choose another source or date range.`);
  return points;
}

function applyScenarioFlows(scenario, timestep, isCurrent) {
  if (!isCurrent()) { showMessage("The scenario changed. Load streamflow again.", true); return; }
  if (!timestep || timestep.reaches.length !== scenario.Reaches.length
      || timestep.reaches.some(({ target, point }, index) => target !== scenario.Reaches[index]
        || !Number.isFinite(point.flow) || point.flow < 0)) {
    showMessage("An exact streamflow sample is required for every reach. No flows were changed.", true);
    return;
  }
  for (const { target, point } of timestep.reaches) target.Streamflow = roundFlow(point.flow);
  renderScenarioEditor();
  closeFlowSelection();
  showMessage(`Set all ${scenario.Reaches.length} reach flows in ${scenario.ScenarioID} at ${timestep.time}.`);
}

function renderScenarioFlowTimes(scenario, reaches, series, isCurrent) {
  const times = sharedTimesteps(reaches, series);
  if (!times.length) return el("p", { className: "hint", text: "No exact UTC timestamps are shared by every reach in this scenario. Choose another source or date range. No flows were changed." });
  const select = el("select", { "aria-label": "Scenario streamflow time (UTC)" },
    times.map((item) => el("option", { value: item.time, text: item.time })));
  return el("div", { className: "row" },
    el("label", { text: "Time (UTC) " }, select),
    el("button", { type: "button", text: "Apply to whole scenario",
      onclick: () => applyScenarioFlows(scenario, times.find((item) => item.time === select.value), isCurrent) }));
}

function showStreamflows(scenario, reaches, box, isCurrent) {
  const source = el("select", { "aria-label": "Streamflow source" },
    NWPS_SERIES.map((series) => el("option", { value: series.key, text: series.label })),
    el("option", { value: "retrospective", text: "Historic — NWM v3.0 retrospective" }));
  const description = el("div");
  const controls = el("div", { "aria-live": "polite" });
  let version = 0;
  const update = () => {
    const selectedVersion = ++version;
    const current = () => isCurrent() && selectedVersion === version;
    const historic = source.value === "retrospective";
    const series = NWPS_SERIES.find((item) => item.key === source.value);
    description.replaceChildren(
      el("p", { text: historic
        ? "NOAA National Water Model v3.0 CONUS retrospective simulation, read from NOAA's public AWS Zarr archive. "
          + "Driven by Analysis of Record for Calibration (AORC) weather data, without streamflow assimilation. "
          + "These are modeled historical flows, not stream-gauge observations."
        : series.description }),
      el("dl", { className: "summary" },
        el("dt", { text: "Time window" }),
        el("dd", { text: historic
          ? `${RETROSPECTIVE_RANGE.min} to ${RETROSPECTIVE_RANGE.max} (UTC); select up to 92 days per lookup.`
          : series.window }),
        el("dt", { text: "Resolution" }),
        el("dd", { text: historic ? "Hourly streamflow for an individual NWM river reach."
          : "Streamflow for an individual NWM river reach. Time spacing is measured from the returned samples below, not the forecast update frequency." })),
      el("p", { className: "hint", text: "Values are displayed in m³/s. Reach-based streamflow is not a gridded flood-depth product." }),
      el("a", {
        href: historic ? "https://registry.opendata.aws/nwm-archive/" : "https://water.noaa.gov/about/nwm",
        target: "_blank", rel: "noopener noreferrer", text: "Source documentation",
      }));
    controls.replaceChildren();
    const start = el("input", { type: "date", ...RETROSPECTIVE_RANGE, value: "2023-01-01", required: true });
    const end = el("input", { type: "date", ...RETROSPECTIVE_RANGE, value: "2023-01-31", required: true });
    const output = el("div");
    const button = el("button", { type: "button", text: "Load streamflow" });
    let request = 0;
    const load = async () => {
      const selectedRequest = ++request;
      const active = () => current() && selectedRequest === request;
      button.disabled = true;
      output.replaceChildren(el("p", { className: "hint", text: historic
        ? "Reading the NWM retrospective for every reach; each lookup can take 10–30 seconds…"
        : `Loading ${series.label.toLowerCase()} for every reach…` }));
      try {
        if (historic && (!start.value || !end.value || start.value > end.value)) {
          throw new Error("Choose a valid UTC date range.");
        }
        const loaded = new Map();
        for (const reach of reaches) {
          let points;
          try { points = await fetchReachFlows(reach, source.value, start.value, end.value); }
          catch (error) { throw new Error(`Reach ${reach.ReachID}: ${error.message}`); }
          if (!active()) return;
          loaded.set(reach, points);
        }
        output.replaceChildren(
          ...groupedFlowCoverage(reaches, loaded),
          scenario ? renderScenarioFlowTimes(scenario, reaches, loaded, active)
            : renderTimestepBatch(reaches, loaded, active));
      } catch (error) {
        if (active()) output.replaceChildren(el("p", {
          className: "error", text: `Streamflow lookup failed: ${error.message} No flows were changed.`,
        }));
      } finally {
        if (active()) button.disabled = false;
      }
    };
    button.addEventListener("click", load);
    const invalidate = () => {
      request++;
      button.disabled = false;
      output.replaceChildren(el("p", { className: "hint", text: "Date range changed. Load streamflow again." }));
    };
    [start, end].forEach((input) => input.addEventListener("input", invalidate));
    if (historic) controls.append(el("div", { className: "row" },
      el("label", { text: "From (UTC) " }, start), el("label", { text: "to " }, end)), button);
    controls.append(output);
    if (!historic) load();
  };
  source.addEventListener("change", update);
  box.replaceChildren(
    el("h4", { text: `Streamflow for all ${reaches.length} reaches` }),
    el("label", { text: "Streamflow source " }, source), description, controls);
  box.hidden = false;
  update();
}

function openFlowSelection(scenario = null) {
  if (!scenario) {
    activateTab("reach-tools", "timesteps");
    if (flowSelection.batchCurrent?.()) return;
    flowSelection.batchLookup++;
    $("#timestep-content").replaceChildren();
  }
  const sourceReaches = () => scenario ? scenario.Reaches : state.reachSet;
  const reaches = sourceReaches().slice();
  const ids = reaches.map((reach) => String(reach.ReachID).trim());
  if (!reaches.length || new Set(ids).size !== ids.length
      || reaches.some((reach) => !/^\d+$/.test(String(reach.ReachID).trim())
        || !/^\d{8}$/.test(String(reach.HUC).trim()))) {
    const message = "Add at least one reach, with unique numeric ReachIDs and 8-digit HUCs, before selecting streamflow.";
    if (scenario) showMessage(message, true);
    else $("#timestep-content").append(el("p", { className: "hint", text: message }));
    return;
  }
  const key = scenario ? "lookup" : "batchLookup";
  const lookup = ++flowSelection[key];
  const workspaceVersion = state.workspaceVersion;
  const signatures = reaches.map((reach) => `${reach.HUC}/${reach.ReachID}`);
  const current = () => lookup === flowSelection[key] && workspaceVersion === state.workspaceVersion
    && (!scenario || state.scenarios.includes(scenario))
    && sourceReaches().length === reaches.length
    && reaches.every((reach, index) => sourceReaches()[index] === reach
      && signatures[index] === `${reach.HUC}/${reach.ReachID}`);
  if (scenario) {
    $("#flow-selection-title").textContent = `Select streamflow for scenario ${scenario.ScenarioID || "(unnamed)"}`;
    $("#flow-selection").hidden = false;
  } else {
    flowSelection.batchCurrent = current;
  }
  const content = scenario ? $("#flow-selection-content") : $("#timestep-content");
  showStreamflows(
    scenario,
    reaches,
    content,
    current,
  );
  (scenario ? $("#flow-selection") : $("#reach-tools")).scrollIntoView({ block: "nearest" });
}

function closeFlowSelection() {
  flowSelection.lookup++;
  $("#flow-selection").hidden = true;
  $("#flow-selection-content").replaceChildren();
}

async function pickReachAt(latlng) {
  const result = $("#picker-result");
  const lookup = ++picker.lookup;
  result.replaceChildren(el("span", { className: "hint", text: "Looking up reach and HUC8…" }));
  try {
    const found = await lookupReach(latlng);
    if (lookup !== picker.lookup) return;
    highlightReach(found.feature, false);
    result.replaceChildren(
      el("span", { text: `ReachID ${found.reachId} in HUC8 ${found.huc} (${found.hucName})` }),
      el("button", { type: "button", className: "primary small", text: "Add to reach set", onclick: () => {
        if (lookup !== picker.lookup) { showMessage("The map selection changed. Click the reach again.", true); return; }
        if (state.reachSet.some((reach) => String(reach.ReachID).trim() === found.reachId)) {
          showMessage(`Reach ${found.reachId} is already in the reach set.`, true);
          return;
        }
        picker.features.set(found.reachId, found.feature);
        state.reachSet.push({ HUC: found.huc, ReachID: found.reachId });
        renderReachSet();
        showMessage(`Added reach ${found.reachId} in HUC8 ${found.huc} to the reach set.`);
      } }));
  } catch (error) {
    if (lookup === picker.lookup) result.replaceChildren(el("span", { className: "error", text: error.message }));
  }
}

function openReachPicker(focus = null) {
  if (!authReady() || !state.workspace) {
    showMessage("Sign in and open a workspace before using the reach map.", true);
    return;
  }
  if (!window.L) { showMessage("The map library could not be loaded.", true); return; }
  activateTab("reach-tools", "map");
  const map = ensurePickerMap();
  map.invalidateSize();
  refreshReachMap(!focus, focus);
  $("#reach-picker").scrollIntoView({ block: "nearest" });
}

function closeReachPicker() {
  picker.lookup++;
  picker.render++;
  if (picker.highlight) { picker.highlight.remove(); picker.highlight = null; }
  $("#picker-result").replaceChildren();
}

function setupReachTabs() {
  const tabs = [...document.querySelectorAll('[data-tabs="reach-tools"] button')];
  const select = (tab) => {
    if (tab.dataset.tab === "map") openReachPicker();
    else if (tab.dataset.tab === "timesteps") openFlowSelection();
    else activateTab("reach-tools", tab.dataset.tab);
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[next].focus();
      select(tabs[next]);
    });
  });
  document.addEventListener("tabchange", (event) => {
    if (event.detail.group === "main" && event.detail.name === "submit"
        && picker.map && !$("#reach-picker").hidden) picker.map.invalidateSize();
  });
}

function loadExample(example) {
  setScenarios(example.scenarios);
  const reaches = new Map();
  for (const scenario of state.scenarios) {
    for (const reach of scenario.Reaches) {
      const id = reach.ReachID.trim();
      if (!reaches.has(id)) reaches.set(id, { HUC: reach.HUC.trim(), ReachID: id });
    }
  }
  state.reachSet = [...reaches.values()];
  flowSelection.batchLookup++;
  $("#timestep-content").replaceChildren();
  renderReachSet();
  activateTab("reach-tools", "scenarios");
}

async function loadExamples() {
  try {
    state.examples = await api("/ui/examples");
  } catch (error) {
    showMessage(`Could not load examples: ${error.message}`, true);
    return;
  }
  const select = $("#example-select");
  for (const example of state.examples) select.append(el("option", { value: example.name, text: example.name }));
  if (state.examples.length) {
    const preferred = state.examples.find((example) => example.name === "single_reach") || state.examples[0];
    select.value = preferred.name;
    loadExample(preferred);
  }
}

function setupJobOptions() {
  const form = $("#job-options");
  const modes = form.elements.execution_mode;
  for (const mode of state.config.execution_modes) modes.append(el("option", { value: mode, text: mode }));
  const updateHint = () => {
    const streamed = modes.value.startsWith("streaming-");
    const notes = [];
    if (streamed) notes.push("Streaming modes run one process.");
    notes.push("Outputs are assigned under this workspace; existing objects are never overwritten.");
    $("#options-hint").textContent = notes.join(" ");
    form.elements.max_procs.disabled = streamed;
  };
  modes.addEventListener("change", updateHint);
  updateHint();
}

async function submitJob(event) {
  event.preventDefault();
  if (!authReady() || !state.workspace) { showMessage("Sign in and open a workspace before submitting jobs.", true); return; }
  if (!canWriteWorkspace()) { showMessage("Submitting a job requires HydroShare write access.", true); return; }
  if (!$("#json-editor").hidden) {
    try { setScenarios(JSON.parse($("#json-text").value)); } catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); return; }
  }
  if (state.scenarios.some((scenario) => scenario.Reaches.some((reach) =>
    !Number.isFinite(reach.Streamflow) || reach.Streamflow < 0))) {
    showMessage("Enter a finite nonnegative streamflow for every reach in each scenario, or use Select streamflow.", true);
    return;
  }
  const form = event.target;
  const maxProcs = form.elements.max_procs.value;
  const body = {
    scenarios: state.scenarios,
    execution_mode: form.elements.execution_mode.value,
  };
  const displayName = form.elements.display_name.value.trim();
  if (displayName) body.display_name = displayName;
  if (maxProcs && !form.elements.max_procs.disabled) body.max_procs = Number(maxProcs);
  const button = $("button[type=submit]", form);
  const workspaceVersion = state.workspaceVersion;
  button.disabled = true;
  try {
    const submitted = await api("/jobs/hand-fim", { method: "POST", json: body });
    if (workspaceVersion !== state.workspaceVersion) return;
    showMessage(`Job ${submitted.job_id} submitted (${submitted.status}).`);
    activateTab("main", "results");
    await loadJobs();
    openJob(submitted.job_id);
  } catch (error) {
    if (!error.stale && workspaceVersion === state.workspaceVersion) showMessage(`Submission failed:\n${error.message}`, true);
  } finally {
    if (workspaceVersion === state.workspaceVersion) button.disabled = false;
  }
}

// ---------- Results ----------

async function loadJobs() {
  const list = $("#job-list");
  if (!authReady() || !state.workspace) {
    updateCompareJobOptions([]);
    list.replaceChildren(el("li", { className: "hint", text: "Open a workspace to list jobs." }));
    return;
  }
  const workspaceVersion = state.workspaceVersion;
  try {
    const { jobs } = await api("/jobs");
    if (workspaceVersion !== state.workspaceVersion) return;
    updateCompareJobOptions(jobs);
    list.replaceChildren(...jobs.slice().reverse().map((job) => {
      const stateLabel = job.execution_state_known === false ? `${job.status} · stale` : job.status;
      const item = el("li", { onclick: () => openJob(job.job_id) },
        el("span", { text: job.display_name || job.job_id.slice(0, 8) + "…", title: job.display_name || job.job_id }),
        el("span", { text: job.user_id || "Unknown submitter" }),
        el("span", { className: `status ${job.execution_state_known === false ? "stale" : job.status}`, text: stateLabel }));
      item.dataset.job = job.job_id;
      item.classList.toggle("active", job.job_id === state.currentJob);
      return item;
    }));
    if (!jobs.length) list.append(el("li", { className: "hint", text: "No jobs in this workspace." }));
  } catch (error) {
    if (workspaceVersion !== state.workspaceVersion || error.stale) return;
    updateCompareJobOptions([]);
    list.replaceChildren(el("li", { className: "hint", text: `Could not list jobs: ${error.message}` }));
  }
}

function updateCompareJobOptions(jobs) {
  const completed = jobs.filter((job) => job.status === "success");
  for (const side of ["base", "candidate"]) {
    const select = $(`#compare-results select[name="${side}_job_id"]`);
    if (!select) continue;
    const selected = select.value;
    select.replaceChildren(el("option", {
      value: "", text: completed.length ? "Choose a completed job" : "No completed jobs available",
      disabled: true, selected: true,
    }), ...completed.map((job) => {
      const shortId = job.job_id.slice(0, 8);
      return el("option", {
        value: job.job_id,
        text: `${job.display_name || job.job_type || "Job"} · ${shortId}`,
        title: job.job_id,
      });
    }));
    if (completed.some((job) => job.job_id === selected)) select.value = selected;
  }
}

function newResultView(title) {
  if (state.jobPoll) { clearTimeout(state.jobPoll); state.jobPoll = null; }
  state.viewId = (state.viewId || 0) + 1;
  const old = $("#result-view");
  // Replace the element so listeners bound to a previous result do not fire.
  const view = old.cloneNode(false);
  old.replaceWith(view);
  view.dataset.viewId = state.viewId;
  view.replaceChildren($("#result-template").content.cloneNode(true));
  $(".result-title", view).textContent = title;
  bindTabs("result", view);
  return view;
}

function formatDuration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
  if (hours) return `${hours}h ${minutes}m ${seconds}s`;
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatMemory(bytes) {
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString(undefined, { timeZoneName: "short" }) : null;
}

// Execution time from worker start to finish; a running job's elapsed time updates every second.
function durationEntry(job) {
  if (!job.started_at) return null;
  const started = new Date(job.started_at).getTime();
  if (job.finished_at) return formatDuration(new Date(job.finished_at).getTime() - started);
  if (job.status !== "started") return null;
  const node = el("span", { text: `${formatDuration(Date.now() - started)} (running)` });
  const viewId = state.viewId;
  const timer = setInterval(() => {
    if (viewId !== state.viewId || !node.isConnected) { clearInterval(timer); return; }
    node.textContent = `${formatDuration(Date.now() - started)} (running)`;
  }, 1000);
  return node;
}

function addSummary(view, entries) {
  const list = $("dl.summary", view);
  for (const [label, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    list.append(el("dt", { text: label }), el("dd", {}, value instanceof Node ? value : String(value)));
  }
}

function renderParameters(container, scenarios, bucket, key) {
  const table = el("table", {}, el("tr", {}, el("th", { text: "ScenarioID" }), el("th", { text: "HUC" }),
    el("th", { text: "ReachID" }), el("th", { text: "Streamflow (m³/s)" })));
  for (const scenario of scenarios) {
    for (const reach of scenario.Reaches || []) {
      table.append(el("tr", {}, el("td", { text: scenario.ScenarioID }), el("td", { text: reach.HUC }),
        el("td", { text: reach.ReachID }), el("td", { className: "num", text: reach.Streamflow })));
    }
  }
  container.replaceChildren(
    el("div", { className: "row" },
      el("button", { type: "button", text: "Edit and resubmit", onclick: () => {
        setScenarios(scenarios);
        activateTab("main", "submit");
        showMessage("Loaded these parameters into the submission form; choose a new output prefix before submitting.");
      } }),
      key ? el("button", { type: "button", text: "Download scenarios.json", onclick: () => downloadObject(bucket, key) }) : null),
    table);
}

const DIFF_LEGEND = "Differences: red cells flood only in the {base}, orange cells only in the {candidate}.";

function parityLabels(compared) {
  return compared ? { base: "base", candidate: "candidate", Base: "Base", Candidate: "Candidate" }
    : { base: "legacy", candidate: "streaming", Base: "Legacy", Candidate: "Streaming" };
}

function renderParity(container, parity, compared, onMap) {
  if (!parity || !parity.length) return;
  const labels = parityLabels(compared);
  const table = el("table", {}, el("tr", {}, ...["ScenarioID", "Matches", `${labels.Base} cells`, `${labels.Candidate} cells`,
    `${labels.Base}-only`, `${labels.Candidate}-only`, "Grid shift (rows, cols)", "Missing reaches", ""].map((text) => el("th", { text }))));
  for (const item of parity) {
    table.append(el("tr", {},
      el("td", { text: item.ScenarioID }),
      el("td", { className: `status ${item.matches ? "success" : "failure"}`, text: item.matches ? "yes" : "no" }),
      el("td", { className: "num", text: item.legacy_flooded_cells }),
      el("td", { className: "num", text: item.streaming_flooded_cells }),
      el("td", { className: "num", text: item.legacy_only_cells }),
      el("td", { className: "num", text: item.streaming_only_cells }),
      el("td", { text: `${item.grid_shift_rows}, ${item.grid_shift_columns}` }),
      el("td", { text: (item.missing_reaches || []).join(", ") }),
      el("td", {}, onMap ? el("button", { type: "button", className: "small", text: "Map diff",
        onclick: () => onMap({ name: `${item.ScenarioID} differences`, key: item.diff_key }) }) : "")));
  }
  const matched = parity.filter((item) => item.matches).length;
  container.replaceChildren(
    el("h3", { text: compared ? "Result comparison" : "Legacy/streaming parity" }),
    el("p", { text: `${matched} of ${parity.length} scenarios match. ` +
      DIFF_LEGEND.replace("{base}", labels.base).replace("{candidate}", labels.candidate) +
      " Grid shifts of up to two cells are realigned before counting." }),
    table);
}

function authTileLayer(bucket, key, info) {
  const Layer = L.GridLayer.extend({
    createTile(coords, done) {
      const tile = document.createElement("img");
      tile.alt = "";
      api(`/outputs/tiles/${coords.z}/${coords.x}/${coords.y}.png?${query({ bucket, key })}`, { raw: true })
        .then((response) => response.blob())
        .then((blob) => {
          tile.onload = () => { URL.revokeObjectURL(tile.src); done(null, tile); };
          tile.src = URL.createObjectURL(blob);
        })
        .catch((error) => done(error, tile));
      return tile;
    },
  });
  const [west, south, east, north] = info.bounds;
  return new Layer({
    minZoom: info.minzoom, maxNativeZoom: info.maxzoom, maxZoom: 22, opacity: 0.8,
    bounds: L.latLngBounds([south, west], [north, east]),
  });
}

// Result maps list at most this many scenario layers up front; larger (time-step) results show
// one scenario and load the others as the player reaches them.
const MAX_VISIBLE_SCENARIOS = 12;
const LAYER_OPACITY = 0.8;

function createResultMap(view, bucket, legend = "Flooded cells are shown in blue; toggle scenarios with the layer control.") {
  const status = $(".map-status", view);
  const container = $(".map", view);
  if (!window.L) { status.textContent = "The map library could not be loaded."; container.hidden = true; return null; }
  if (!state.config.authentication_enabled) {
    status.textContent = "Map display requires HydroShare authentication and S3 output.";
    container.hidden = true;
    return null;
  }
  const map = L.map(container, { minZoom: 2 });
  osmLayer().addTo(map);
  map.setView([39, -98], 4);
  const control = L.control.layers(null, null, { collapsed: false }).addTo(map);
  const overlays = new Map();
  const ready = new Map();
  const notes = new Set();
  let player = null;
  status.textContent = legend;
  view.addEventListener("tabchange", (event) => { if (event.detail.name === "map") map.invalidateSize(); });

  // Resolves to { overlay, info } (or null on error), registering the layer once.
  function overlayFor(layer) {
    if (!overlays.has(layer.key)) {
      overlays.set(layer.key, api(`/outputs/info?${query({ bucket, key: layer.key })}`).then((info) => {
        const entry = { overlay: authTileLayer(bucket, layer.key, info), info };
        control.addOverlay(entry.overlay, layer.name);
        if (info.minzoom > 2) notes.add(`Layers appear at zoom ≥ ${info.minzoom}.`);
        ready.set(layer.key, entry);
        return entry;
      }).catch((error) => {
        notes.add(`${layer.name}: ${error.message}`);
        return null;
      }).finally(() => { status.textContent = [legend, ...notes].join(" "); }));
    }
    return overlays.get(layer.key);
  }

  function fitTo(info) {
    const [west, south, east, north] = info.bounds;
    map.invalidateSize();
    map.fitBounds([[south, west], [north, east]]);
    if (map.getZoom() < info.minzoom) map.setZoom(info.minzoom);
  }

  async function add(layer, fit) {
    const entry = await overlayFor(layer);
    if (!entry) return;
    entry.overlay.setOpacity(LAYER_OPACITY);
    if (!map.hasLayer(entry.overlay)) entry.overlay.addTo(map);
    if (fit) fitTo(entry.info);
  }

  function hide(overlay) {
    map.removeLayer(overlay);
    overlay.setOpacity(LAYER_OPACITY);
  }

  function whenLoaded(overlay) {
    if (!map.hasLayer(overlay) || !overlay.isLoading()) return Promise.resolve();
    return new Promise((resolve) => { overlay.once("load", resolve); setTimeout(resolve, 10000); });
  }

  // Cycles through scenario layers one at a time, preloading the next frame invisibly.
  function animate(frames, describe = () => "") {
    if (player || frames.length < 2) return;
    let current = -1, playing = false, run = 0;
    const slider = el("input", { type: "range", min: 0, max: frames.length - 1, step: 1, value: 0, "aria-label": "Scenario frame" });
    const label = el("span", { className: "frame-label" });
    const delay = el("select", { "aria-label": "Frame delay" },
      [["2000", "2 s / frame"], ["1000", "1 s / frame"], ["500", "0.5 s / frame"], ["250", "0.25 s / frame"]]
        .map(([value, text]) => el("option", { value, text, selected: value === "1000" })));
    const play = el("button", { type: "button", className: "small", text: "Play" });
    const step = (offset) => el("button", { type: "button", className: "small", text: offset < 0 ? "◀" : "▶",
      title: offset < 0 ? "Previous scenario" : "Next scenario",
      onclick: () => { stop(); show((Math.max(current, 0) + offset + frames.length) % frames.length); } });
    player = el("div", { className: "row player" }, play, step(-1), slider, step(1), delay, label);
    status.after(player);

    function stop() { playing = false; run++; play.textContent = "Play"; }

    async function show(index) {
      current = index;
      slider.value = index;
      label.textContent = `${index + 1}/${frames.length} ${frames[index].name}${describe(frames[index].name)}`;
      const next = frames[(index + 1) % frames.length];
      const entry = await overlayFor(frames[index]);
      if (current !== index) return;
      for (const frame of frames) {
        const other = ready.get(frame.key);
        if (other && frame !== frames[index] && frame !== next && map.hasLayer(other.overlay)) hide(other.overlay);
      }
      if (entry) {
        if (!map.hasLayer(entry.overlay)) entry.overlay.addTo(map);
        entry.overlay.setOpacity(LAYER_OPACITY);
        entry.overlay.bringToFront();
      }
      const preload = await overlayFor(next);
      if (current !== index || !preload || preload === entry) return;
      if (!map.hasLayer(preload.overlay)) preload.overlay.setOpacity(0).addTo(map);
    }

    async function loop(token) {
      while (playing && token === run && container.isConnected) {
        await sleep(Number(delay.value));
        if (!playing || token !== run) return;
        const index = (current + 1) % frames.length;
        const entry = await overlayFor(frames[index]);
        if (entry) await whenLoaded(entry.overlay);
        if (!playing || token !== run) return;
        await show(index);
      }
    }

    play.addEventListener("click", () => {
      if (playing) { stop(); return; }
      playing = true;
      play.textContent = "Pause";
      loop(++run);
    });
    slider.addEventListener("input", () => { stop(); show(Number(slider.value)); });
    // Leave the initially added layers alone until the player is used.
    current = 0;
    label.textContent = `1/${frames.length} ${frames[0].name}${describe(frames[0].name)}`;
  }

  return { add, animate };
}

async function loadOutputDetails(view, bucket, prefix, result) {
  const root = joinKey(prefix);
  const readmeKey = joinKey(root, "README.md");
  const compared = result && result.compared;
  // Comparison jobs write only parity files; their parameters are the base result's scenarios.
  const scenariosBucket = compared ? compared.base_bucket : bucket;
  const scenariosKey = compared ? joinKey(compared.base_prefix, "scenarios.json") : joinKey(root, "scenarios.json");
  const parity = (result && result.parity) || [];
  let browser = null;
  const linkHandler = (target) => {
    if (/^[A-Za-z0-9._-][A-Za-z0-9._/-]*$/.test(target) && !target.split("/").includes("..")) {
      const key = joinKey(root, target);
      if (hasSuffix(target, PREVIEW_SUFFIXES)) {
        activateTab("result", "files", view);
        browser.preview(key);
      } else if (hasSuffix(target, RASTER_SUFFIXES)) {
        showRaster(key);
      } else {
        downloadObject(bucket, key);
      }
    }
  };
  let map;
  let layers = [];
  let scenarios = [];
  const describe = (id) => {
    const reaches = scenarios.find((scenario) => scenario.ScenarioID === id)?.Reaches || [];
    if (!reaches.length) return "";
    if (reaches.length > 3) return ` — ${reaches.length} reaches`;
    return ` — ${reaches.map((reach) => `${reach.ReachID}: ${reach.Streamflow} m³/s`).join(", ")}`;
  };
  const addLayers = () => {
    const frames = layers.filter((layer) => !layer.diff);
    const eager = frames.length <= MAX_VISIBLE_SCENARIOS;
    layers.forEach((layer, index) => {
      if (eager || layer.diff || layer === frames[0]) map.add(layer, index === 0);
    });
    map.animate(frames, describe);
  };
  const ensureMap = () => {
    if (map === undefined) {
      const labels = parityLabels(compared);
      map = createResultMap(view, bucket, parity.length
        ? `Flooded cells are shown in blue. ${DIFF_LEGEND.replace("{base}", labels.base).replace("{candidate}", labels.candidate)}`
        : undefined);
      if (map) addLayers();
      if (map && !layers.length) $(".map-status", view).textContent = "No scenario rasters found; use Map on a file in the Files tab.";
    }
    return map;
  };
  const showLayer = (layer) => {
    activateTab("result", "map", view);
    const current = ensureMap();
    if (current) current.add(layer, true);
  };
  const showRaster = (key) => showLayer({ name: key.slice(root.length).replace(/^\//, ""), key });
  browser = createBrowser($(".files", view), { bucket, root, linkHandler, onRaster: showRaster });
  view.addEventListener("tabchange", (event) => { if (event.detail.name === "map") ensureMap(); });
  renderParity($(".parity", view), parity, compared, showLayer);

  const readme = $(".readme", view);
  if (compared) {
    readme.replaceChildren(el("p", { className: "hint",
      text: "Comparison jobs write parity/report.json and one parity/<ScenarioID>.diff.tif per scenario; input parameters are the base result's." }));
  } else {
    readme.replaceChildren(el("p", { className: "hint", text: "Loading README…" }));
    api(`/ui/objects/preview?${query({ bucket, key: readmeKey })}`)
      .then((preview) => readme.replaceChildren(renderMarkdown(preview.text, linkHandler)))
      .catch((error) => readme.replaceChildren(el("p", { className: "hint", text: `README.md unavailable: ${error.message}` })));
  }

  try {
    const preview = await api(`/ui/objects/preview?${query({ bucket: scenariosBucket, key: scenariosKey })}`);
    scenarios = JSON.parse(preview.text);
    renderParameters($(".parameters", view), scenarios, scenariosBucket, scenariosKey);
  } catch (error) {
    $(".parameters", view).replaceChildren(el("p", { className: "hint", text: `scenarios.json unavailable: ${error.message}` }));
  }

  if (result) {
    if (result.scenario_manifests && result.scenario_manifests.length) {
      layers = result.scenario_manifests.map((item) => ({ name: item.ScenarioID, key: item.key }));
    } else {
      const ids = (result.outputs || []).map((output) => output.ScenarioID);
      layers = ids.map((id) => ({ name: id, key: joinKey(root, `${id}.cog`) }))
        .filter((layer) => (result.files || []).some((file) => file.key === layer.key));
    }
  } else if (scenarios.length) {
    try {
      const listing = await api(`/ui/objects?${query({ bucket, prefix: root })}`);
      const files = new Set(listing.files.map((file) => file.key));
      const folders = new Set(listing.folders.map((folder) => folder.name));
      for (const { ScenarioID: id } of scenarios) {
        if (files.has(joinKey(root, `${id}.cog`))) layers.push({ name: id, key: joinKey(root, `${id}.cog`) });
        else if (folders.has(id)) layers.push({ name: id, key: joinKey(root, id, "manifest.json") });
      }
    } catch { /* the Files tab reports listing errors */ }
  }
  const diffLayers = parity.map((item) => ({ name: `${item.ScenarioID} differences`, key: item.diff_key, diff: true }));
  // Comparison jobs write no scenario rasters of their own; their map shows only differences.
  layers = compared ? diffLayers : [...layers, ...diffLayers];
  // Layers discovered after the map opened are added now.
  if (map) addLayers();
}

function startLogPolling(view, jobId, status) {
  const pre = $(".logs", view);
  const viewId = state.viewId;
  const workspaceVersion = state.workspaceVersion;
  let offset = 0;
  const poll = async () => {
    try {
      const logs = await api(`/jobs/${jobId}/logs?${query({ offset })}`);
      if (viewId !== state.viewId || workspaceVersion !== state.workspaceVersion) return;
      if (logs.logs) {
        pre.append(logs.logs);
        pre.scrollTop = pre.scrollHeight;
      }
      offset = logs.next_offset;
      if (logs.status !== status) { loadJobs(); openJob(jobId); return; }
      if (["queued", "started"].includes(logs.status)) state.jobPoll = setTimeout(poll, 5000);
      else if (logs.logs) state.jobPoll = setTimeout(poll, 0);
      else if (!pre.textContent) pre.textContent = "No worker output retained.";
    } catch (error) {
      if (error.stale || workspaceVersion !== state.workspaceVersion || viewId !== state.viewId) return;
      pre.append(`\n[log polling stopped: ${error.message}]`);
    }
  };
  poll();
}

function jobNameEditor(view, jobId, job, workspaceVersion, viewId) {
  if (!canWriteWorkspace()) return null;
  const form = el("form", { className: "row" });
  const input = el("input", {
    type: "text", maxLength: 120, value: job.display_name || "",
    placeholder: "Leave empty to clear",
  });
  const button = el("button", { type: "submit", text: "Save name" });
  form.append(el("label", { text: "Job display name" }, input), button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      await api(`/jobs/${jobId}`, {
        method: "PATCH",
        json: { display_name: input.value.trim() || null },
      });
      if (workspaceVersion !== state.workspaceVersion || viewId !== state.viewId) return;
      showMessage("Job name updated.");
      await loadJobs();
      openJob(jobId);
    } catch (error) {
      if (!error.stale && workspaceVersion === state.workspaceVersion && viewId === state.viewId) {
        showMessage(`Could not update job name: ${error.message}`, true);
      }
    } finally {
      if (workspaceVersion === state.workspaceVersion && viewId === state.viewId) button.disabled = false;
    }
  });
  return form;
}

async function openJob(jobId) {
  if (!state.workspace) { showMessage("Open a workspace before viewing jobs.", true); return; }
  state.currentJob = jobId;
  for (const item of document.querySelectorAll("#job-list li")) item.classList.toggle("active", item.dataset.job === jobId);
  const view = newResultView(`Job ${jobId}`);
  const viewId = state.viewId;
  const workspaceVersion = state.workspaceVersion;
  let job;
  try {
    job = await api(`/jobs/${jobId}`);
    if (viewId !== state.viewId || workspaceVersion !== state.workspaceVersion) return;
  } catch (error) {
    if (error.stale || viewId !== state.viewId || workspaceVersion !== state.workspaceVersion) return;
    view.append(el("p", { className: "message error", text: `Could not load job: ${error.message}` }));
    return;
  }
  const result = job.result;
  $(".result-title", view).textContent = job.display_name
    ? `${job.display_name} (${jobId})` : `Job ${jobId}`;
  const location = result && result.output_bucket
    ? `s3://${result.output_bucket}/${joinKey(result.output_prefix)}` : null;
  const compared = result && result.compared;
  addSummary(view, [
    ["Job ID", jobId],
    ["Display name", job.display_name],
    ["Status", el("span", { className: `status ${job.status}`, text: job.status })],
    ["Error", job.error],
    ["Started", formatTime(job.started_at)],
    ["Finished", formatTime(job.finished_at)],
    [job.finished_at ? "Execution time" : "Elapsed", durationEntry(job)],
    ["Submitted by", job.user_id],
    ["Submitted", formatTime(job.submitted_at)],
    ["Job type", job.job_type || (compared ? "compare" : null)],
    ["Execution state", job.execution_state_known === false ? "Stale — worker state is no longer available" : "Current"],
    ["Base", compared ? locationLink(compared.base_bucket, compared.base_prefix) : null],
    ["Candidate", compared ? locationLink(compared.candidate_bucket, compared.candidate_prefix) : null],
    ["Skipped scenarios", compared && compared.skipped_scenarios.join(", ")],
    ["Execution mode", result && !compared ? result.execution_mode : null],
    ["Peak worker-container memory", result && result.peak_memory_bytes != null
      ? `${formatMemory(result.peak_memory_bytes)} (sampled)` : null],
    ["Scenarios", result && (result.outputs || []).map((output) => output.ScenarioID).join(", ")],
    ["Output location", location],
    ["Output files", result && (result.files || []).length],
  ]);
  const nameEditor = jobNameEditor(view, jobId, job, workspaceVersion, viewId);
  if (nameEditor) $("dl.summary", view).after(nameEditor);
  if (job.status === "queued" && job.execution_state_known !== false && job.user_id === state.userId) {
    $("dl.summary", view).after(el("div", { className: "row" }, el("button", { type: "button", text: "Cancel job", onclick: async () => {
      try {
        await api(`/jobs/${jobId}`, { method: "DELETE" });
        if (workspaceVersion !== state.workspaceVersion) return;
        showMessage(`Cancellation requested for ${jobId}.`); openJob(jobId); loadJobs();
      } catch (error) {
        if (!error.stale && workspaceVersion === state.workspaceVersion) showMessage(`Cancel failed: ${error.message}`, true);
      }
    } })));
  }
  if (job.execution_state_known !== false) startLogPolling(view, jobId, job.status);
  if (result && result.output_bucket) {
    loadOutputDetails(view, result.output_bucket, result.output_prefix, result);
  } else {
    const note = job.status === "success"
      ? "This job has no retained output in the workspace."
      : "Results appear here when the job succeeds. Worker output is streamed in the Logs tab.";
    $(".readme", view).replaceChildren(el("p", { className: "hint", text: note }));
    for (const selector of [".parameters", ".files"]) $(selector, view).replaceChildren(el("p", { className: "hint", text: note }));
    $(".map-status", view).textContent = note;
  }
  if (["queued", "started", "failure"].includes(job.status)) activateTab("result", "logs", view);
}

function requireRetainedAccess(action) {
  if (!authReady() || !state.workspace) { showMessage(`Sign in and open a workspace to ${action}.`, true); return false; }
  return true;
}

function openLocation(bucket, prefix) {
  if (!requireRetainedAccess("open retained outputs")) return;
  prefix = joinKey(prefix);
  state.currentJob = null;
  for (const item of document.querySelectorAll("#job-list li")) item.classList.remove("active");
  const view = newResultView(`s3://${bucket}/${prefix}`);
  addSummary(view, [["Output location", `s3://${bucket}/${prefix}`]]);
  $("[data-tabs=result] [data-tab=logs]", view).hidden = true;
  loadOutputDetails(view, bucket, prefix, null);
}

function workspaceRelativePrefix(bucket, prefix) {
  if (!state.workspace || bucket !== state.workspace.bucket) return null;
  const root = joinKey(state.workspace.prefix);
  const path = joinKey(prefix);
  return path.startsWith(root + "/") ? path.slice(root.length + 1) : null;
}

function locationLink(bucket, prefix) {
  return el("a", { href: "#", text: `s3://${bucket}/${joinKey(prefix)}`, onclick: (event) => {
    event.preventDefault();
    openLocation(bucket, prefix);
  } });
}

async function submitCompare(event) {
  event.preventDefault();
  if (!requireRetainedAccess("compare results")) return;
  if (!canWriteWorkspace()) { showMessage("Creating a comparison requires HydroShare write access.", true); return; }
  const form = event.target;
  const value = (name) => form.elements[name].value.trim();
  const baseJobId = value("base_job_id");
  const candidateJobId = value("candidate_job_id");
  if (baseJobId === candidateJobId) {
    showMessage("Choose two different workspace jobs to compare.", true);
    return;
  }
  const displayName = value("display_name");
  const button = $("button[type=submit]", form);
  const workspaceVersion = state.workspaceVersion;
  button.disabled = true;
  try {
    const [baseJob, candidateJob] = await Promise.all([
      api(`/jobs/${baseJobId}`), api(`/jobs/${candidateJobId}`),
    ]);
    if (workspaceVersion !== state.workspaceVersion) return;
    for (const job of [baseJob, candidateJob]) {
      if (job.status !== "success" || !job.result || !job.result.output_bucket || !job.result.output_prefix) {
        throw new Error(`Job ${job.job_id} has no successful retained output to compare.`);
      }
      if (workspaceRelativePrefix(job.result.output_bucket, job.result.output_prefix) === null) {
        throw new Error(`Job ${job.job_id} output is outside the active workspace.`);
      }
    }
    const body = {
      base_bucket: baseJob.result.output_bucket,
      base_prefix: baseJob.result.output_prefix,
      candidate_bucket: candidateJob.result.output_bucket,
      candidate_prefix: candidateJob.result.output_prefix,
    };
    if (displayName) body.display_name = displayName;
    const submitted = await api("/jobs/compare", { method: "POST", json: body });
    if (workspaceVersion !== state.workspaceVersion) return;
    showMessage(`Comparison job ${submitted.job_id} submitted (${submitted.status}).`);
    activateTab("main", "results");
    await loadJobs();
    openJob(submitted.job_id);
  } catch (error) {
    if (!error.stale && workspaceVersion === state.workspaceVersion) showMessage(`Comparison failed:\n${error.message}`, true);
  } finally {
    if (workspaceVersion === state.workspaceVersion) button.disabled = false;
  }
}

// ---------- Startup ----------

async function start() {
  try {
    state.config = await api("/ui/config");
  } catch (error) {
    showMessage(`Could not load server configuration: ${error.message}`, true);
    return;
  }
  if (!state.config.authentication_enabled) {
    renderAuth();
    return;
  }
  bindTabs("main");
  $("#sign-in").addEventListener("click", signIn);
  $("#sign-out").addEventListener("click", signOut);
  $("#workspace-open").addEventListener("submit", openWorkspace);
  $("#workspace-create").addEventListener("submit", createWorkspace);
  $("#workspace-switch").addEventListener("click", switchWorkspace);
  $("#example-load").addEventListener("click", () => {
    const example = state.examples.find((item) => item.name === $("#example-select").value);
    if (example) loadExample(example);
  });
  $("#json-toggle").addEventListener("click", () => {
    const editor = $("#json-editor");
    if (editor.hidden) {
      $("#json-text").value = JSON.stringify(state.scenarios, null, 2);
    } else {
      try { setScenarios(JSON.parse($("#json-text").value)); } catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); return; }
    }
    editor.hidden = !editor.hidden;
    $("#reach-set-builder").hidden = !editor.hidden;
    $("#reach-tools").hidden = !editor.hidden;
    $("#json-toggle").textContent = editor.hidden ? "Edit as JSON" : "Edit as table";
    if (!editor.hidden) { closeReachPicker(); closeFlowSelection(); }
    else activateTab("reach-tools", "scenarios");
  });
  $("#json-apply").addEventListener("click", () => {
    try { setScenarios(JSON.parse($("#json-text").value)); showMessage("JSON applied."); }
    catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); }
  });
  $("#reach-set-add").addEventListener("click", () => {
    state.reachSet.push({ HUC: "", ReachID: "" });
    renderReachSet(true);
  });
  $("#reach-set-add-scenario").addEventListener("click", addScenarioFromReachSet);
  setupReachTabs();
  $("#picker-fit").addEventListener("click", () => refreshReachMap(true));
  $("#flow-selection-close").addEventListener("click", closeFlowSelection);
  $("#job-options").addEventListener("submit", submitJob);
  $("#jobs-refresh").addEventListener("click", loadJobs);
  $("#compare-results").addEventListener("submit", submitCompare);
  setupJobOptions();
  renderReachSet();
  renderScenarioEditor();
  const examples = loadExamples();
  await refreshAfterAuth();
}

document.addEventListener("DOMContentLoaded", start);

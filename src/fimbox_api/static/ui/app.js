"use strict";

// FIMBox browser UI. All rendering uses DOM APIs and textContent (no innerHTML of server data).
const TOKEN_KEY = "fimbox_session_token";
const PREVIEW_SUFFIXES = [".json", ".md", ".txt", ".csv", ".log", ".xml", ".yaml", ".yml", ".geojson"];
const RASTER_SUFFIXES = [".cog", ".tif", ".tiff"];
const state = {
  config: null, token: sessionStorage.getItem(TOKEN_KEY), scenarios: [], examples: [], jobPoll: null,
  workspace: null, workspaceVersion: 0, workspaceAction: 0, workspaceLocation: null, userId: null, scope: "",
};
state.workspaces = [];

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
  $("#main-tabs").hidden = !active;
  $("#main-content").hidden = !active;
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
  if ($("#scenario-editor")) renderScenarioEditor();
  if ($("#json-text")) $("#json-text").value = "";
  if ($("#input-description")) {
    $("#input-description").textContent = "Sign in and open a workspace before browsing the input dataset or using the reach map.";
    $("#input-browser").replaceChildren();
  }

  if ($("#compare-results")) $("#compare-results").reset();
  if ($("#reach-picker")) closeReachPicker();
  if (picker.map) {
    picker.map.remove();
    picker.map = null;
    picker.highlight = null;
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
  if ($("#scenario-editor")) renderScenarioEditor();
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
  if (authReady() && state.workspace) {
    loadInputDataset();
    loadJobs();
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
  container.replaceChildren(crumbs, table, more, preview);
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
  if (!authReady() || !state.workspace) {
    description.textContent = "Sign in and open a workspace before browsing the input dataset or using the reach map.";
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
    + `s3://${dataset.bucket}/${dataset.prefix}. It holds dataset.parquet, per-HUC metadata (hucs/<huc>/huc.parquet), `
    + `per-branch REM/catchment COGs, and per-reach rating Parquet files.`;
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

// ---------- Scenario editor ----------

function renderScenarioEditor() {
  const editor = $("#scenario-editor");
  editor.replaceChildren();
  state.scenarios.forEach((scenario, index) => {
    const rows = scenario.Reaches.map((reach, reachIndex) => el("tr", {},
      el("td", {}, el("input", { value: reach.HUC, maxLength: 8, size: 9, pattern: "\\d{8}", required: true,
        oninput: (event) => { reach.HUC = event.target.value.trim(); } })),
      el("td", {}, el("input", { value: reach.ReachID, size: 12, required: true,
        oninput: (event) => { reach.ReachID = event.target.value.trim(); } })),
      el("td", {}, el("input", { type: "number", className: "num", min: 0, step: "any", value: reach.Streamflow, required: true,
        oninput: (event) => { reach.Streamflow = event.target.value === "" ? "" : Number(event.target.value); } })),
      el("td", {}, el("button", { type: "button", className: "small", text: "Select streamflow",
        onclick: () => openFlowSelection(scenario, reach, reachIndex) }),
      " ",
      el("button", { type: "button", className: "small", text: "Pick on map",
        onclick: () => openReachPicker(scenario, reach, reachIndex) }),
      " ",
      el("button", { type: "button", className: "small", text: "Remove", onclick: () => {
        scenario.Reaches.splice(reachIndex, 1);
        renderScenarioEditor();
      } }))));
    editor.append(el("fieldset", {},
      el("legend", {}, "Scenario ",
        el("input", { value: scenario.ScenarioID, required: true, placeholder: "ScenarioID",
          oninput: (event) => { scenario.ScenarioID = event.target.value.trim(); } }),
        el("button", { type: "button", className: "small", text: "Remove scenario", onclick: () => {
          state.scenarios.splice(index, 1);
          renderScenarioEditor();
        } })),
      el("table", {},
        el("tr", {}, el("th", { text: "HUC (8 digits)" }), el("th", { text: "NWM ReachID" }),
          el("th", { text: "Streamflow (m³/s)" }), el("th")),
        rows),
      el("div", { className: "row" }, el("button", { type: "button", className: "small", text: "Add reach", onclick: () => {
        const previous = scenario.Reaches[scenario.Reaches.length - 1];
        scenario.Reaches.push({ HUC: previous ? previous.HUC : "", ReachID: "", Streamflow: 0 });
        renderScenarioEditor();
      } }))));
  });
  if (!state.scenarios.length) editor.append(el("p", { className: "hint", text: "No scenarios yet. Load an example or add a scenario." }));
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
const picker = { map: null, target: null, highlight: null, lookup: 0 };
const flowSelection = { lookup: 0 };

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
  picker.highlight = L.geoJSON(feature, { style: { color: "#d62728", weight: 5 } }).addTo(picker.map);
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
  return map;
}

function targetExists(target) {
  return state.scenarios.some((scenario) => scenario.Reaches.includes(target));
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

function formatTime(value) { return new Date(value).toLocaleString(); }

// Ensemble forecasts (medium/long range) publish members plus a "mean"; others publish "series".
function nwpsPoints(section) {
  const series = section?.series ?? section?.mean;
  if (!Array.isArray(series?.data)) return [];
  const factor = /ft/.test(series.units || "") ? FT3_TO_M3 : 1;
  return series.data
    .filter((point) => Number.isFinite(point.flow) && point.flow >= 0)
    .map((point) => ({ time: point.validTime, flow: point.flow * factor }));
}

function applyStreamflow(flow, reach, target) {
  if (!targetExists(target)) { showMessage("That reach row no longer exists.", true); return; }
  if (reach.huc) target.HUC = reach.huc;
  target.ReachID = reach.reachId;
  target.Streamflow = roundFlow(flow);
  renderScenarioEditor();
  showMessage(`Set reach ${reach.reachId} streamflow to ${roundFlow(flow)} m³/s.`);
}

function flowButton(label, point, reach, target) {
  return el("button", {
    type: "button", className: "small", text: `${label}: ${roundFlow(point.flow)} m³/s`,
    title: point.time ? formatTime(point.time) : undefined,
    onclick: () => applyStreamflow(point.flow, reach, target),
  });
}

const MAX_TIMESTEP_SCENARIOS = 240;
const TIMESTEP_ID = /^\d{8}T\d{4}Z$/;
const HOUR_MS = 3600 * 1000;

// datetime-local inputs are read and written as UTC.
function utcInputValue(time) { return new Date(time).toISOString().slice(0, 16); }

function parseUtcInput(value) { return value ? Date.parse(`${value}:00Z`) : NaN; }

function timestepId(time) { return `${new Date(time).toISOString().slice(0, 16).replace(/[-:]/g, "")}Z`; }

// Replaces a reach row with one scenario per timestep (ScenarioID like 20180915T1800Z),
// merging into existing timestep scenarios so several reaches can share the same times.
function applyTimesteps(points, reach, target, closePanel) {
  if (!targetExists(target)) { showMessage("That reach row no longer exists.", true); return; }
  const huc = reach.huc || String(target.HUC || "");
  if (!/^\d{8}$/.test(huc)) { showMessage("Set the row's HUC8 before making time-step scenarios.", true); return; }
  const owner = state.scenarios.find((scenario) => scenario.Reaches.includes(target));
  owner.Reaches.splice(owner.Reaches.indexOf(target), 1);
  if (!owner.Reaches.length) state.scenarios.splice(state.scenarios.indexOf(owner), 1);
  for (const point of points) {
    const id = timestepId(point.time);
    let scenario = state.scenarios.find((item) => item.ScenarioID === id);
    if (!scenario) { scenario = { ScenarioID: id, Reaches: [] }; state.scenarios.push(scenario); }
    const row = { HUC: huc, ReachID: reach.reachId, Streamflow: roundFlow(point.flow) };
    const existing = scenario.Reaches.findIndex((item) => String(item.ReachID) === reach.reachId);
    if (existing >= 0) scenario.Reaches[existing] = row;
    else scenario.Reaches.push(row);
  }
  const others = state.scenarios.filter((scenario) => !TIMESTEP_ID.test(scenario.ScenarioID));
  const steps = state.scenarios.filter((scenario) => TIMESTEP_ID.test(scenario.ScenarioID))
    .sort((a, b) => a.ScenarioID.localeCompare(b.ScenarioID));
  state.scenarios.splice(0, state.scenarios.length, ...others, ...steps);
  closePanel();
  renderScenarioEditor();
  showMessage(`Set reach ${reach.reachId} in ${points.length} time-step scenario(s) (${steps.length} in total).`);
}

function timestepForm(points, reach, target, closePanel) {
  const times = points.map((point) => Date.parse(point.time));
  const bounds = { min: utcInputValue(times[0]), max: utcInputValue(times[times.length - 1]) };
  const start = el("input", { type: "datetime-local", step: 3600, ...bounds, value: bounds.min, "aria-label": "First time (UTC)" });
  const end = el("input", { type: "datetime-local", step: 3600, ...bounds, value: bounds.max, "aria-label": "Last time (UTC)" });
  const every = el("select", { "aria-label": "Time step" },
    [1, 3, 6, 12, 24].map((hours) => el("option", { value: hours, text: `every ${hours} h` })));
  const count = el("span", { className: "hint" });
  const create = el("button", { type: "button", className: "small", text: "Create scenarios" });
  const selected = () => {
    const first = parseUtcInput(start.value);
    const last = parseUtcInput(end.value);
    const stepMs = Number(every.value) * HOUR_MS;
    if (!(first <= last)) return [];
    return points.filter((point, index) => times[index] >= first && times[index] <= last && (times[index] - first) % stepMs === 0);
  };
  const update = () => {
    const total = selected().length;
    create.disabled = !total || total > MAX_TIMESTEP_SCENARIOS;
    count.textContent = total > MAX_TIMESTEP_SCENARIOS
      ? `${total} timesteps; choose at most ${MAX_TIMESTEP_SCENARIOS} (narrow the range or use a longer step).`
      : `${total} timestep(s) → one scenario each.`;
  };
  [start, end, every].forEach((input) => input.addEventListener("input", update));
  create.addEventListener("click", () => {
    const chosen = selected();
    if (chosen.length && chosen.length <= MAX_TIMESTEP_SCENARIOS) applyTimesteps(chosen, reach, target, closePanel);
  });
  update();
  return el("details", { className: "timesteps" },
    el("summary", { text: "Make time-step scenarios" }),
    el("div", { className: "row" },
      el("label", { text: "From (UTC) " }, start), el("label", { text: "to " }, end), every, create, count),
    el("p", { className: "hint", text: "Replaces this row with one scenario per selected timestep. Repeat for other reaches over the same times to add them to the same scenarios." }));
}

function peakPoint(points) { return points.reduce((best, point) => (point.flow > best.flow ? point : best)); }

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

async function showNwpsFlows(container, series, reach, target, isCurrent, closePanel) {
  container.replaceChildren(el("span", { className: "hint", text: `Loading ${series.label.toLowerCase()} from NOAA NWPS…` }));
  const base = `${NWPS_REACHES_URL}/${encodeURIComponent(reach.reachId)}/streamflow`;
  try {
    const body = await fetchPublicJson(series.param ? `${base}?${query({ series: series.param })}` : base);
    if (!isCurrent()) return;
    const points = nwpsPoints(body[series.key]).sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
    if (!points.length) {
      container.replaceChildren(el("span", {
        className: "hint", text: `NWPS currently has no ${series.label.toLowerCase()} flows for this reach. Choose another source.`,
      }));
      return;
    }
    const buttons = series.param ? [] : [flowButton("Use latest", points[points.length - 1], reach, target)];
    buttons.push(flowButton("Use peak", peakPoint(points), reach, target));
    container.replaceChildren(flowCoverage(points),
      el("div", { className: "row" }, buttons), timestepForm(points, reach, target, closePanel));
  } catch (error) {
    if (isCurrent()) container.replaceChildren(el("span", {
      className: "error", text: `NWPS lookup failed: ${error.message}`,
    }));
  }
}

function historicFlowControls(reach, target, isCurrent, closePanel) {
  const start = el("input", { type: "date", ...RETROSPECTIVE_RANGE, value: "2023-01-01", required: true });
  const end = el("input", { type: "date", ...RETROSPECTIVE_RANGE, value: "2023-01-31", required: true });
  const output = el("div", { className: "row" });
  const steps = el("div");
  const button = el("button", { type: "button", className: "small", text: "Look up", onclick: async () => {
    button.disabled = true;
    steps.replaceChildren();
    output.replaceChildren(el("span", { className: "hint", text: "Reading the NWM retrospective; this can take 10–30 seconds…" }));
    try {
      const body = await api(`/ui/streamflow/retrospective?${query({ reach_id: reach.reachId, start: start.value, end: end.value })}`);
      if (!isCurrent()) return;
      output.replaceChildren(
        el("span", { text: `${body.start.slice(0, 10)} to ${body.end.slice(0, 10)}: min ${roundFlow(body.min)}, mean ${roundFlow(body.mean)} m³/s` }),
        flowButton("Use max", { flow: body.max, time: body.max_time }, reach, target),
        el("span", { className: "hint", text: `peak at ${formatTime(body.max_time)}` }),
        flowButton("Use mean", { flow: body.mean }, reach, target));
      const first = Date.parse(body.start);
      const points = body.flows
        .map((flow, index) => ({ time: new Date(first + index * HOUR_MS).toISOString(), flow }))
        .filter((point) => point.flow !== null);
      steps.replaceChildren(points.length
        ? timestepForm(points, reach, target, closePanel)
        : el("span", { className: "hint", text: "No hourly flows were available for this date range." }));
    } catch (error) {
      if (isCurrent()) output.replaceChildren(el("span", { className: "error", text: error.message }));
    } finally {
      button.disabled = false;
    }
  } });
  return [
    el("h4", { text: `Historic (NWM v3.0 retrospective, ${RETROSPECTIVE_RANGE.min} to ${RETROSPECTIVE_RANGE.max}, up to 92 days)` }),
    el("div", { className: "row" }, el("label", { text: "From " }, start), el("label", { text: "to " }, end), button),
    output,
    steps,
  ];
}

function showStreamflows(reach, target, box, isCurrent, closePanel) {
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
    if (historic) controls.append(...historicFlowControls(reach, target, current, closePanel));
    else showNwpsFlows(controls, series, reach, target, current, closePanel);
  };
  source.addEventListener("change", update);
  box.replaceChildren(
    el("h4", { text: `Streamflow for reach ${reach.reachId}` }),
    el("label", { text: "Streamflow source " }, source), description, controls);
  box.hidden = false;
  update();
}

function openFlowSelection(scenario, reach, reachIndex) {
  const reachId = String(reach.ReachID || "").trim();
  const huc = String(reach.HUC || "").trim();
  if (!/^\d+$/.test(reachId) || !/^\d{8}$/.test(huc)) {
    showMessage("Enter a numeric NWM ReachID and an 8-digit HUC8 in the scenario row before selecting streamflow.", true);
    return;
  }
  const lookup = ++flowSelection.lookup;
  $("#flow-selection-title").textContent = `Scenario ${scenario.ScenarioID || "(unnamed)"}, reach ${reachIndex + 1}`;
  $("#flow-selection").hidden = false;
  showStreamflows(
    { reachId, huc },
    reach,
    $("#flow-selection-content"),
    () => lookup === flowSelection.lookup,
    closeFlowSelection,
  );
  $("#flow-selection").scrollIntoView({ block: "nearest" });
}

function closeFlowSelection() {
  flowSelection.lookup++;
  $("#flow-selection").hidden = true;
  $("#flow-selection-content").replaceChildren();
}

function hideStreamflows() {
  const box = $("#picker-flows");
  box.hidden = true;
  box.replaceChildren();
}

async function pickReachAt(latlng) {
  const result = $("#picker-result");
  const lookup = ++picker.lookup;
  result.replaceChildren(el("span", { className: "hint", text: "Looking up reach and HUC8…" }));
  hideStreamflows();
  try {
    const found = await lookupReach(latlng);
    if (lookup !== picker.lookup) return;
    highlightReach(found.feature, false);
    result.replaceChildren(
      el("span", { text: `ReachID ${found.reachId} in HUC8 ${found.huc} (${found.hucName})` }),
      el("button", { type: "button", className: "primary small", text: "Use this reach", onclick: () => {
        if (!targetExists(picker.target)) { showMessage("That reach row no longer exists.", true); return; }
        picker.target.HUC = found.huc;
        picker.target.ReachID = found.reachId;
        renderScenarioEditor();
        showMessage(`Set reach ${found.reachId} in HUC8 ${found.huc}.`);
      } }));
    showStreamflows(
      { reachId: found.reachId, huc: found.huc },
      picker.target,
      $("#picker-flows"),
      () => lookup === picker.lookup,
      closeReachPicker,
    );
  } catch (error) {
    if (lookup === picker.lookup) result.replaceChildren(el("span", { className: "error", text: error.message }));
  }
}

async function showCurrentReach(reachId) {
  const lookup = ++picker.lookup;
  try {
    const body = await fetchPublicJson(`${NLDI_COMID_URL}/${encodeURIComponent(reachId)}`);
    const feature = body.features?.[0];
    if (lookup !== picker.lookup || !feature) return;
    highlightReach(feature, true);
    $("#picker-result").replaceChildren(el("span", { className: "hint", text: `Current ReachID ${reachId} is highlighted.` }));
    showStreamflows(
      { reachId, huc: String(picker.target?.HUC || "") },
      picker.target,
      $("#picker-flows"),
      () => lookup === picker.lookup,
      closeReachPicker,
    );
  } catch (error) {
    if (lookup === picker.lookup) {
      $("#picker-result").replaceChildren(el("span", { className: "hint", text: `Current ReachID ${reachId} was not found in NHDPlus.` }));
    }
  }
}

function openReachPicker(scenario, reach, reachIndex) {
  if (!authReady() || !state.workspace) {
    showMessage("Sign in and open a workspace before using the reach map.", true);
    return;
  }
  if (!window.L) { showMessage("The map library could not be loaded.", true); return; }
  closeFlowSelection();
  picker.target = reach;
  $("#picker-title").textContent = `Pick a reach for ${scenario.ScenarioID || "this scenario"}, row ${reachIndex + 1}`;
  $("#picker-result").replaceChildren();
  hideStreamflows();
  $("#reach-picker").hidden = false;
  const map = ensurePickerMap();
  map.invalidateSize();
  if (picker.highlight) { picker.highlight.remove(); picker.highlight = null; }
  if (/^\d+$/.test(reach.ReachID)) showCurrentReach(reach.ReachID);
  else picker.lookup++;
  $("#reach-picker").scrollIntoView({ block: "nearest" });
}

function closeReachPicker() {
  picker.target = null;
  picker.lookup++;
  hideStreamflows();
  $("#reach-picker").hidden = true;
}

function applyLoadedScenarios(scenarios, source) {
  try {
    setScenarios(scenarios);
    $("#example-select").value = "";
    showMessage(`Loaded ${state.scenarios.length} scenario(s) from ${source}.`);
  } catch (error) {
    showMessage(`Could not use ${source}: ${error.message}`, true);
  }
}

async function uploadScenarios(event) {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) { showMessage(`${file.name} is larger than 2 MiB.`, true); return; }
  let scenarios;
  try {
    scenarios = JSON.parse(await file.text());
  } catch (error) {
    showMessage(`${file.name} is not valid JSON: ${error.message}`, true);
    return;
  }
  applyLoadedScenarios(scenarios, file.name);
}

async function loadScenariosFromUrl(url) {
  const button = $("#json-url-form button[type=submit]");
  button.disabled = true;
  try {
    const loaded = await api(`/ui/scenarios/load?${query({ url })}`);
    applyLoadedScenarios(loaded.scenarios, url);
  } catch (error) {
    showMessage(`Could not load ${url}: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
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
    setScenarios(preferred.scenarios);
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
    if (example) setScenarios(example.scenarios);
  });
  $("#json-toggle").addEventListener("click", () => {
    const editor = $("#json-editor");
    if (editor.hidden) {
      $("#json-text").value = JSON.stringify(state.scenarios, null, 2);
    } else {
      try { setScenarios(JSON.parse($("#json-text").value)); } catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); return; }
    }
    editor.hidden = !editor.hidden;
    $("#scenario-editor").hidden = !editor.hidden;
    $("#scenario-buttons").hidden = !editor.hidden;
    $("#json-toggle").textContent = editor.hidden ? "Edit as JSON" : "Edit as table";
    if (!editor.hidden) closeReachPicker();
  });
  $("#json-apply").addEventListener("click", () => {
    try { setScenarios(JSON.parse($("#json-text").value)); showMessage("JSON applied."); }
    catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); }
  });
  $("#scenario-add").addEventListener("click", () => {
    state.scenarios.push({ ScenarioID: `scenario-${state.scenarios.length + 1}`, Reaches: [{ HUC: "", ReachID: "", Streamflow: 0 }] });
    renderScenarioEditor();
  });
  $("#json-file").addEventListener("change", uploadScenarios);
  $("#picker-close").addEventListener("click", closeReachPicker);
  $("#flow-selection-close").addEventListener("click", closeFlowSelection);
  $("#json-url-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadScenariosFromUrl($("#json-url").value.trim());
  });
  $("#job-options").addEventListener("submit", submitJob);
  $("#jobs-refresh").addEventListener("click", loadJobs);
  $("#compare-results").addEventListener("submit", submitCompare);
  setupJobOptions();
  renderScenarioEditor();
  // /ui?scenarios_url=<url> opens the submit tab with that input once signed in.
  const linked = new URLSearchParams(location.search).get("scenarios_url");
  const examples = loadExamples();
  await refreshAfterAuth();
  if (linked) {
    $("#json-url").value = linked;
    activateTab("main", "submit");
    await examples;
    if (authReady() && state.workspace) await loadScenariosFromUrl(linked);
    else showMessage("Sign in and open a workspace, then press Load URL to load the linked scenarios.");
  }
}

document.addEventListener("DOMContentLoaded", start);

"use strict";

// FIMBox browser UI. All rendering uses DOM APIs and textContent (no innerHTML of server data).
const TOKEN_KEY = "fimbox_session_token";
const PREVIEW_SUFFIXES = [".json", ".md", ".txt", ".csv", ".log", ".xml", ".yaml", ".yml", ".geojson"];
const RASTER_SUFFIXES = [".cog", ".tif", ".tiff"];
const state = { config: null, token: sessionStorage.getItem(TOKEN_KEY), scenarios: [], examples: [], jobPoll: null };

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
  constructor(message, status, headers) { super(message); this.status = status; this.headers = headers; }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options = { ...options, body: JSON.stringify(options.json) };
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && state.token) {
    setToken(null);
    renderAuth();
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
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function authReady() { return !state.config.authentication_enabled || Boolean(state.token); }

async function renderAuth() {
  const status = $("#auth-status");
  const signIn = $("#sign-in");
  const signOut = $("#sign-out");
  if (!state.config.authentication_enabled) {
    status.textContent = "Local mode (no sign-in required)";
    signIn.hidden = signOut.hidden = true;
    return;
  }
  if (!state.token) {
    status.textContent = "Not signed in";
    signIn.hidden = false;
    signOut.hidden = true;
    return;
  }
  try {
    const session = await api("/auth/session");
    status.textContent = `Signed in as HydroShare user ${session.user_id}`;
    signIn.hidden = true;
    signOut.hidden = false;
  } catch {
    status.textContent = "Not signed in";
    signIn.hidden = false;
    signOut.hidden = true;
  }
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
  loadInputDataset();
  loadJobs();
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
  container.replaceChildren(el("p", { className: "hint", text: `Loading ${key}…` }));
  try {
    const preview = await api(`/ui/objects/preview?${query({ bucket, key })}`);
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
    container.replaceChildren(el("p", { className: "message error", text: `Preview failed: ${error.message}` }));
  }
}

function createBrowser(container, { bucket, root, onRaster, linkHandler }) {
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
    if (!token) {
      renderCrumbs(prefix);
      table.replaceChildren(el("tr", {}, el("td", { className: "hint", text: "Loading…" })));
    }
    more.replaceChildren();
    try {
      const listing = await api(`/ui/objects?${query({ bucket, prefix, token })}`);
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
      el("td", {}, el("button", { type: "button", className: "small", text: "Remove", onclick: () => {
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
  state.scenarios = scenarios.map((scenario) => ({
    ScenarioID: String(scenario.ScenarioID ?? ""),
    Reaches: (scenario.Reaches || []).map((reach) => ({
      HUC: String(reach.HUC ?? ""), ReachID: String(reach.ReachID ?? ""), Streamflow: reach.Streamflow,
    })),
  }));
  renderScenarioEditor();
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
  if (state.config.require_hydroshare_output || state.config.authentication_enabled) form.elements.output_bucket.value = "resource";
  const updateHint = () => {
    const streamed = modes.value.endsWith("streaming");
    const notes = [];
    if (streamed) notes.push("Streaming modes require an output bucket and run one process.");
    if (state.config.require_hydroshare_output) notes.push("This deployment requires a HydroShare output bucket.");
    if (!state.config.authentication_enabled) notes.push("Local mode: S3 output is unavailable and output files are removed after the run.");
    notes.push("Use a new prefix per run to avoid overwriting retained outputs.");
    $("#options-hint").textContent = notes.join(" ");
    form.elements.max_procs.disabled = streamed;
  };
  modes.addEventListener("change", updateHint);
  updateHint();
}

async function submitJob(event) {
  event.preventDefault();
  if (!authReady()) { showMessage("Sign in with HydroShare before submitting jobs.", true); return; }
  if (!$("#json-editor").hidden) {
    try { setScenarios(JSON.parse($("#json-text").value)); } catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); return; }
  }
  const form = event.target;
  const bucket = form.elements.output_bucket.value.trim();
  const maxProcs = form.elements.max_procs.value;
  const body = {
    scenarios: state.scenarios,
    execution_mode: form.elements.execution_mode.value,
    output_bucket: bucket || null,
    output_prefix: bucket ? form.elements.output_prefix.value.trim() : "",
    overwrite: form.elements.overwrite.checked,
  };
  if (maxProcs && !form.elements.max_procs.disabled) body.max_procs = Number(maxProcs);
  const button = $("button[type=submit]", form);
  button.disabled = true;
  try {
    const submitted = await api("/jobs/hand-fim", { method: "POST", json: body });
    showMessage(`Job ${submitted.job_id} submitted (${submitted.status}).`);
    activateTab("main", "results");
    await loadJobs();
    openJob(submitted.job_id);
  } catch (error) {
    showMessage(`Submission failed:\n${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

// ---------- Results ----------

async function loadJobs() {
  const list = $("#job-list");
  if (!authReady()) { list.replaceChildren(el("li", { className: "hint", text: "Sign in to list jobs." })); return; }
  try {
    const { jobs } = await api("/jobs");
    list.replaceChildren(...jobs.slice().reverse().map((job) => {
      const item = el("li", { onclick: () => openJob(job.job_id) },
        el("span", { text: job.job_id.slice(0, 8) + "…", title: job.job_id }),
        el("span", { className: `status ${job.status}`, text: job.status }));
      item.dataset.job = job.job_id;
      item.classList.toggle("active", job.job_id === state.currentJob);
      return item;
    }));
    if (!jobs.length) list.append(el("li", { className: "hint", text: "No jobs in the last 24 hours." }));
  } catch (error) {
    list.replaceChildren(el("li", { className: "hint", text: `Could not list jobs: ${error.message}` }));
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

function renderParity(container, parity) {
  if (!parity || !parity.length) return;
  const table = el("table", {}, el("tr", {}, ...["ScenarioID", "Matches", "Legacy cells", "Streaming cells",
    "Legacy-only", "Streaming-only", "Grid shift (rows, cols)", "Missing reaches"].map((text) => el("th", { text }))));
  for (const item of parity) {
    table.append(el("tr", {},
      el("td", { text: item.ScenarioID }),
      el("td", { className: `status ${item.matches ? "success" : "failure"}`, text: item.matches ? "yes" : "no" }),
      el("td", { className: "num", text: item.legacy_flooded_cells }),
      el("td", { className: "num", text: item.streaming_flooded_cells }),
      el("td", { className: "num", text: item.legacy_only_cells }),
      el("td", { className: "num", text: item.streaming_only_cells }),
      el("td", { text: `${item.grid_shift_rows}, ${item.grid_shift_columns}` }),
      el("td", { text: (item.missing_reaches || []).join(", ") })));
  }
  container.replaceChildren(el("h3", { text: "Legacy/streaming parity" }), table);
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

function createResultMap(view, bucket) {
  const status = $(".map-status", view);
  const container = $(".map", view);
  if (!window.L) { status.textContent = "The map library could not be loaded."; container.hidden = true; return null; }
  if (!state.config.authentication_enabled) {
    status.textContent = "Map display requires HydroShare authentication and S3 output.";
    container.hidden = true;
    return null;
  }
  const map = L.map(container, { minZoom: 2 });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  map.setView([39, -98], 4);
  const control = L.control.layers(null, null, { collapsed: false }).addTo(map);
  const added = new Set();
  const notes = [];
  status.textContent = "Flooded cells are shown in blue; toggle scenarios with the layer control.";
  view.addEventListener("tabchange", (event) => { if (event.detail.name === "map") map.invalidateSize(); });

  async function add(layer, fit) {
    if (added.has(layer.key)) return;
    added.add(layer.key);
    try {
      const info = await api(`/outputs/info?${query({ bucket, key: layer.key })}`);
      const overlay = authTileLayer(bucket, layer.key, info);
      overlay.addTo(map);
      control.addOverlay(overlay, layer.name);
      if (fit) {
        const [west, south, east, north] = info.bounds;
        map.invalidateSize();
        map.fitBounds([[south, west], [north, east]]);
        if (map.getZoom() < info.minzoom) map.setZoom(info.minzoom);
      }
      if (info.minzoom > 2) notes.push(`${layer.name} appears at zoom ≥ ${info.minzoom}.`);
    } catch (error) {
      notes.push(`${layer.name}: ${error.message}`);
    }
    status.textContent = ["Flooded cells are shown in blue; toggle scenarios with the layer control.", ...notes].join(" ");
  }
  return { add };
}

async function loadOutputDetails(view, bucket, prefix, result) {
  const root = joinKey(prefix);
  const readmeKey = joinKey(root, "README.md");
  const scenariosKey = joinKey(root, "scenarios.json");
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
  const ensureMap = () => {
    if (map === undefined) {
      map = createResultMap(view, bucket);
      if (map) layers.forEach((layer, index) => map.add(layer, index === 0));
      if (map && !layers.length) $(".map-status", view).textContent = "No scenario rasters found; use Map on a file in the Files tab.";
    }
    return map;
  };
  const showRaster = (key) => {
    activateTab("result", "map", view);
    const current = ensureMap();
    if (current) current.add({ name: key.slice(root.length).replace(/^\//, ""), key }, true);
  };
  browser = createBrowser($(".files", view), { bucket, root, linkHandler, onRaster: showRaster });
  view.addEventListener("tabchange", (event) => { if (event.detail.name === "map") ensureMap(); });

  const readme = $(".readme", view);
  readme.replaceChildren(el("p", { className: "hint", text: "Loading README…" }));
  api(`/ui/objects/preview?${query({ bucket, key: readmeKey })}`)
    .then((preview) => readme.replaceChildren(renderMarkdown(preview.text, linkHandler)))
    .catch((error) => readme.replaceChildren(el("p", { className: "hint", text: `README.md unavailable: ${error.message}` })));

  let scenarios = [];
  try {
    const preview = await api(`/ui/objects/preview?${query({ bucket, key: scenariosKey })}`);
    scenarios = JSON.parse(preview.text);
    renderParameters($(".parameters", view), scenarios, bucket, scenariosKey);
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
  // Layers discovered after the map opened are added now.
  if (map) layers.forEach((layer, index) => map.add(layer, index === 0));
}

function startLogPolling(view, jobId, status) {
  const pre = $(".logs", view);
  const viewId = state.viewId;
  let offset = 0;
  const poll = async () => {
    try {
      const logs = await api(`/jobs/${jobId}/logs?${query({ offset })}`);
      if (viewId !== state.viewId) return;
      if (logs.logs) {
        pre.append(logs.logs);
        pre.scrollTop = pre.scrollHeight;
      }
      offset = logs.next_offset;
      if (logs.status !== status) { loadJobs(); openJob(jobId); return; }
      if (["queued", "started"].includes(logs.status)) state.jobPoll = setTimeout(poll, 5000);
      else if (!pre.textContent) pre.textContent = "No worker output retained.";
    } catch (error) {
      pre.append(`\n[log polling stopped: ${error.message}]`);
    }
  };
  poll();
}

async function openJob(jobId) {
  state.currentJob = jobId;
  for (const item of document.querySelectorAll("#job-list li")) item.classList.toggle("active", item.dataset.job === jobId);
  const view = newResultView(`Job ${jobId}`);
  const viewId = state.viewId;
  let job;
  try {
    job = await api(`/jobs/${jobId}`);
    if (viewId !== state.viewId) return;
  } catch (error) {
    view.append(el("p", { className: "message error", text: `Could not load job: ${error.message}` }));
    return;
  }
  const result = job.result;
  const location = result && result.output_bucket
    ? `s3://${result.output_bucket}/${joinKey(result.output_prefix)}` : null;
  addSummary(view, [
    ["Status", el("span", { className: `status ${job.status}`, text: job.status })],
    ["Error", job.error],
    ["Execution mode", result && result.execution_mode],
    ["Scenarios", result && (result.outputs || []).map((output) => output.ScenarioID).join(", ")],
    ["Output location", location],
    ["Output files", result && (result.files || []).length],
  ]);
  if (job.status === "queued") {
    $("dl.summary", view).after(el("div", { className: "row" }, el("button", { type: "button", text: "Cancel job", onclick: async () => {
      try { await api(`/jobs/${jobId}`, { method: "DELETE" }); showMessage(`Cancellation requested for ${jobId}.`); openJob(jobId); loadJobs(); }
      catch (error) { showMessage(`Cancel failed: ${error.message}`, true); }
    } })));
  }
  startLogPolling(view, jobId, job.status);
  renderParity($(".parity", view), result && result.parity);
  if (result && result.output_bucket) {
    loadOutputDetails(view, result.output_bucket, result.output_prefix, result);
  } else {
    const note = job.status === "success"
      ? "This job kept its output local; local files are removed after the run. Submit with an output bucket to retain and view files."
      : "Results appear here when the job succeeds. Worker output is streamed in the Logs tab.";
    $(".readme", view).replaceChildren(el("p", { className: "hint", text: note }));
    for (const selector of [".parameters", ".files"]) $(selector, view).replaceChildren(el("p", { className: "hint", text: note }));
    $(".map-status", view).textContent = note;
  }
  if (["queued", "started", "failure"].includes(job.status)) activateTab("result", "logs", view);
}

function openOutput(event) {
  event.preventDefault();
  if (!state.config.authentication_enabled) { showMessage("Retained outputs require HydroShare authentication on this server.", true); return; }
  if (!authReady()) { showMessage("Sign in with HydroShare to open retained outputs.", true); return; }
  const bucket = event.target.elements.bucket.value.trim();
  const prefix = joinKey(event.target.elements.prefix.value);
  state.currentJob = null;
  for (const item of document.querySelectorAll("#job-list li")) item.classList.remove("active");
  const view = newResultView(`s3://${bucket}/${prefix}`);
  addSummary(view, [["Output location", `s3://${bucket}/${prefix}`]]);
  $("[data-tabs=result] [data-tab=logs]", view).hidden = true;
  loadOutputDetails(view, bucket, prefix, null);
}

// ---------- Startup ----------

async function start() {
  bindTabs("main");
  try {
    state.config = await api("/ui/config");
  } catch (error) {
    showMessage(`Could not load server configuration: ${error.message}`, true);
    return;
  }
  $("#sign-in").addEventListener("click", signIn);
  $("#sign-out").addEventListener("click", signOut);
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
  });
  $("#json-apply").addEventListener("click", () => {
    try { setScenarios(JSON.parse($("#json-text").value)); showMessage("JSON applied."); }
    catch (error) { showMessage(`Invalid JSON: ${error.message}`, true); }
  });
  $("#scenario-add").addEventListener("click", () => {
    state.scenarios.push({ ScenarioID: `scenario-${state.scenarios.length + 1}`, Reaches: [{ HUC: "", ReachID: "", Streamflow: 0 }] });
    renderScenarioEditor();
  });
  $("#job-options").addEventListener("submit", submitJob);
  $("#jobs-refresh").addEventListener("click", loadJobs);
  $("#open-output").addEventListener("submit", openOutput);
  setupJobOptions();
  renderScenarioEditor();
  loadExamples();
  await refreshAfterAuth();
}

document.addEventListener("DOMContentLoaded", start);

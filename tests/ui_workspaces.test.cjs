const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(join(__dirname, "../src/fimbox_api/static/ui/app.js"), "utf8");

function browser() {
  const saved = new Map();
  const context = vm.createContext({
    document: { addEventListener() {} },
    location: { origin: "https://fimbox.test" },
    sessionStorage: { getItem() { return "session"; } },
    localStorage: {
      getItem: (key) => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: (key) => saved.delete(key),
    },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.config = { authentication_enabled: true };
    state.userId = "alice";
    state.scope = "read write";
    let message = "";
    let isError = false;
    let rendered = 0;
    showMessage = (text, error = false) => { message = text; isError = error; };
    renderRememberedWorkspaces = () => { rendered += 1; };
    syncWorkspaceGate = () => {};
    loadInputDataset = () => {};
    loadJobs = () => {};
    clearActiveWorkspace = ({ removePreference = false } = {}) => {
      if (removePreference) localStorage.removeItem(workspacePreferenceKey());
      state.workspace = null;
      state.workspaceVersion += 1;
      state.workspaceAction += 1;
    };
  `, context);
  return {
    saved,
    run: (code) => vm.runInContext(code, context),
    json: (code) => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context)),
  };
}

const a = { workspace_id: "a", bucket: "resource", prefix: "rid/data/contents/a", name: "Study A" };
const b = { workspace_id: "b", bucket: "resource", prefix: "rid/data/contents/b", name: "Study B" };
const locationA = `s3://${a.bucket}/${a.prefix}`;
const locationB = `s3://${b.bucket}/${b.prefix}`;

test("successful opens remember server names, deduplicate locations, and order by most recent", async () => {
  const ui = browser();
  ui.run(`api = async () => (${JSON.stringify(a)});`);
  await ui.run(`openWorkspaceLocation("${locationA}", {})`);
  ui.run(`api = async () => (${JSON.stringify(b)});`);
  await ui.run(`openWorkspaceLocation("${locationB}", {})`);
  ui.run(`api = async () => (${JSON.stringify({ ...a, name: "Renamed A" })});`);
  await ui.run(`openWorkspaceLocation("${locationA}", {})`);
  assert.deepEqual(ui.json("state.workspaces"), [
    { location: locationA, name: "Renamed A" }, { location: locationB, name: "Study B" },
  ]);
  assert.equal(ui.run("message"), "");
  assert.equal(ui.run("localStorage.getItem(workspacePreferenceKey())"), locationA);
  ui.run("state.workspaces = []; loadRememberedWorkspaces();");
  assert.equal(ui.json("state.workspaces").length, 2);
});

test("switch retains shortcuts, reopening validates access, and failure keeps the list", async () => {
  const ui = browser();
  ui.run(`activateWorkspace(${JSON.stringify(a)}); switchWorkspace();`);
  assert.equal(ui.run("state.workspace"), null);
  assert.equal(ui.json("state.workspaces").length, 1);
  ui.run(`let requests = [];
    api = async (path, options) => {
      requests.push([path, options.json.location]);
      throw new Error("Access denied");
    };`);
  await ui.run(`openWorkspaceLocation("${locationA}", {})`);
  assert.deepEqual(ui.json("requests"), [["/workspaces/open", locationA]]);
  assert.equal(ui.run("state.workspace"), null);
  assert.equal(ui.json("state.workspaces").length, 1);
  assert.match(ui.run("message"), /Access denied/);
  assert.equal(ui.run("isError"), true);
  ui.run(`api = async () => (${JSON.stringify(b)});`);
  await ui.run(`openWorkspaceLocation("${locationB}", {})`);
  assert.equal(ui.json("state.workspace").workspace_id, "b");
});

test("successful creation remembers a workspace but failed creation does not", async () => {
  const ui = browser();
  ui.run(`
    let reset = false;
    let button = {};
    let event = { preventDefault() {}, target: {
      elements: { location: { value: "${locationA}" }, name: { value: "Study A" } },
      reset() { reset = true; },
    } };
    $ = () => button;
    api = async (path, options) => {
      if (path !== "/workspaces" || options.json.name !== "Study A") throw new Error("Bad request");
      return ${JSON.stringify(a)};
    };
  `);
  await ui.run("createWorkspace(event)");
  assert.equal(ui.json("state.workspaces").length, 1);
  assert.equal(ui.run("reset"), true);
  ui.run('api = async () => { throw new Error("Prefix occupied"); };');
  await ui.run("createWorkspace(event)");
  assert.equal(ui.json("state.workspaces").length, 1);
  assert.match(ui.run("message"), /Prefix occupied/);
});

test("restore revalidates the last workspace and migrates it into the remembered list", async () => {
  const ui = browser();
  ui.run(`localStorage.setItem(workspacePreferenceKey(), "${locationA}");
    api = async (path, options) => {
      if (path !== "/workspaces/open" || options.json.location !== "${locationA}") throw new Error("Bad restore");
      return ${JSON.stringify(a)};
    };`);
  await ui.run("restoreWorkspace()");
  assert.deepEqual(ui.json("state.workspaces"), [{ name: a.name, location: locationA }]);
});

test("forget is browser-only and account lists remain isolated across sign-out", () => {
  const ui = browser();
  ui.run(`activateWorkspace(${JSON.stringify(a)}); state.userId = "bob"; loadRememberedWorkspaces();`);
  assert.deepEqual(ui.json("state.workspaces"), []);
  ui.run(`activateWorkspace(${JSON.stringify(b)}); state.userId = "alice"; loadRememberedWorkspaces();`);
  assert.deepEqual(ui.json("state.workspaces"), [{ name: a.name, location: locationA }]);
  ui.run(`api = () => { throw new Error("Forget must not call the server"); };
    forgetWorkspace(state.workspaces[0]);`);
  assert.deepEqual(ui.json("state.workspaces"), []);
  assert.equal(ui.run("localStorage.getItem(workspacePreferenceKey())"), null);
  assert.match(ui.run("message"), /Shared workspace data is unchanged/);
  ui.run("state.userId = 'bob'; loadRememberedWorkspaces();");
  assert.deepEqual(ui.json("state.workspaces"), [{ name: b.name, location: locationB }]);
  ui.run(`
    $ = () => ({});
    renderScenarioEditor = () => {};
    renderReachSet = () => {};
    showSignedOut();
  `);
  assert.deepEqual(ui.json("state.workspaces"), []);
  ui.run("state.userId = 'bob'; loadRememberedWorkspaces();");
  assert.deepEqual(ui.json("state.workspaces"), [{ name: b.name, location: locationB }]);
});

test("stale open responses cannot activate or remember a workspace after account or selection changes", async () => {
  for (const change of ["state.userId = 'bob'", "switchWorkspace()"]) {
    const ui = browser();
    ui.run("let complete; api = () => new Promise((resolve) => { complete = resolve; });");
    const pending = ui.run(`openWorkspaceLocation("${locationA}", {})`);
    ui.run(`${change}; complete(${JSON.stringify(a)});`);
    await pending;
    assert.equal(ui.run("state.workspace"), null);
    assert.deepEqual(ui.json("state.workspaces"), []);
  }
});

test("storage failures remain visible without pretending workspace activation failed", async () => {
  const ui = browser();
  ui.run(`localStorage.setItem = () => { throw new Error("Storage blocked"); };
    api = async () => (${JSON.stringify(a)});`);
  await ui.run(`openWorkspaceLocation("${locationA}", {})`);
  assert.equal(ui.json("state.workspace").workspace_id, "a");
  assert.match(ui.run("message"), /Workspace opened, but could not save browser preferences: Storage blocked/);
  assert.equal(ui.run("isError"), true);
  ui.run(`localStorage.getItem = () => "not-json"; loadRememberedWorkspaces();`);
  assert.match(ui.run("message"), /Could not restore remembered workspaces/);
});

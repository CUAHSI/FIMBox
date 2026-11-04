const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(join(__dirname, "../src/fimbox_api/static/ui/app.js"), "utf8");

function scenarioState(scenarios) {
  const context = vm.createContext({
    URLSearchParams,
    document: { addEventListener() {} },
    location: { origin: "https://fimbox.test" },
    sessionStorage: { getItem() { return null; } },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.scenarios = ${JSON.stringify(scenarios)};
    let message = "";
    let isError = false;
    let rendered = 0;
    showMessage = (text, error = false) => { message = text; isError = error; };
    renderScenarioEditor = () => { rendered += 1; };
    closeReachPicker = () => {};
    closeFlowSelection = () => {};
  `, context);
  return {
    run: (code) => vm.runInContext(code, context),
    json: (code) => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context)),
  };
}

function flowPanel() {
  const ui = scenarioState([]);
  ui.run(`
    el = (tag, props = {}, ...children) => {
      const node = {
        tag, ...props, children: children.flat(), listeners: {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
        replaceChildren(...items) { this.children = items; },
        append(...items) { this.children.push(...items); },
      };
      if (tag === "select") node.value = node.children[0].value;
      return node;
    };
    let panelCurrent = true;
    const box = el("div");
    const reach = { reachId: "12345", huc: "01020304" };
    let requests = [];
    fetchPublicJson = async (url) => { requests.push(url); return {}; };
    const textOf = (node) => [node.text || "", ...node.children.map(textOf)].join(" ");
    const selectSource = (key) => {
      const select = box.children[1].children[0];
      select.value = key;
      select.listeners.change();
    };
  `);
  return ui;
}

test("source selector displays and requests only the chosen source, including metadata", async () => {
  const ui = flowPanel();
  ui.run("showStreamflows(reach, {}, box, () => panelCurrent, () => {});");
  await ui.run("Promise.resolve()");
  assert.deepEqual(ui.json("requests"), ["https://api.water.noaa.gov/nwps/v1/reaches/12345/streamflow"]);
  assert.match(ui.run("textOf(box.children[2])"), /analysis and assimilation/);
  assert.match(ui.run("textOf(box.children[3])"), /no recent analysis flows/);

  ui.run("selectSource('shortRange');");
  await ui.run("Promise.resolve()");
  assert.equal(ui.json("requests").length, 2);
  assert.match(ui.json("requests")[1], /\?series=short_range$/);
  assert.match(ui.run("textOf(box.children[2])"), /18 hours/);
  assert.doesNotMatch(ui.run("textOf(box.children[2])"), /retrospective simulation/);

  ui.run("selectSource('retrospective');");
  assert.equal(ui.json("requests").length, 2);
  const metadata = ui.run("textOf(box.children[2])");
  assert.match(metadata, /public AWS Zarr archive/);
  assert.match(metadata, /Hourly/);
  assert.match(metadata, /1979-02-01 to 2023-02-01/);
  assert.match(metadata, /92 days/);
  assert.match(ui.run("textOf(box.children[3])"), /Look up/);
  assert.doesNotMatch(ui.run("textOf(box.children[3])"), /Use peak/);
});

test("switching sources ignores old requests even when returning to the same source", async () => {
  const ui = flowPanel();
  ui.run(`
    let pending = [];
    fetchPublicJson = () => new Promise((resolve) => pending.push(resolve));
    showStreamflows(reach, {}, box, () => panelCurrent, () => {});
    selectSource("retrospective");
    selectSource("analysisAssimilation");
    pending[0]({});
  `);
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /Loading recent analysis/);
  ui.run("pending[1]({});");
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /no recent analysis flows/);
});

test("selected forecast reports actual UTC coverage and spacing and hands off flows", async () => {
  const ui = flowPanel();
  ui.run(`
    let applied = null;
    flowButton = (label, point) => el("button", { text: label, onclick: () => { applied = point.flow; } });
    timestepForm = (points) => el("details", { text: points.length + " timesteps" });
    fetchPublicJson = async () => ({ shortRange: { series: { units: "m3/s", data: [
      { validTime: "2020-01-01T03:00:00Z", flow: 9 },
      { validTime: "2020-01-01T00:00:00Z", flow: 2 },
    ] } } });
    showStreamflows(reach, {}, box, () => panelCurrent, () => {});
    selectSource("shortRange");
  `);
  await ui.run("Promise.resolve()");
  const display = ui.run("textOf(box.children[3])");
  assert.match(display, /2020-01-01 00:00 to 2020-01-01 03:00/);
  assert.match(display, /180 minutes between samples/);
  assert.match(display, /2 sample/);
  assert.match(display, /2 timesteps/);
  ui.run("box.children[3].children[1].children[0].onclick();");
  assert.equal(ui.run("applied"), 9);
});

test("source errors stay visible and closed-panel responses are ignored", async () => {
  const ui = flowPanel();
  ui.run(`
    fetchPublicJson = async () => { throw new Error("Service unavailable"); };
    showStreamflows(reach, {}, box, () => panelCurrent, () => {});
  `);
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /NWPS lookup failed: Service unavailable/);
  ui.run(`
    let finish;
    fetchPublicJson = () => new Promise((resolve) => { finish = resolve; });
    selectSource("longRange");
    panelCurrent = false;
    finish({});
  `);
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /Loading long-range forecast/);
});

test("manual streamflow timestep selection hands off rows and merges matching scenarios", () => {
  const reach = { HUC: "01020304", ReachID: "12345", Streamflow: 0 };
  const other = { HUC: "01020304", ReachID: "67890", Streamflow: 4 };
  const ui = scenarioState([
    { ScenarioID: "manual", Reaches: [reach] },
    { ScenarioID: "20200101T0000Z", Reaches: [other] },
  ]);

  ui.run(`
    applyTimesteps(
      [{ time: "2020-01-01T00:00:00.000Z", flow: 12.3456 }],
      { reachId: "12345", huc: "01020304" },
      state.scenarios[0].Reaches[0],
      closeFlowSelection,
    );
  `);

  assert.deepEqual(ui.json("state.scenarios"), [{
    ScenarioID: "20200101T0000Z",
    Reaches: [
      { HUC: "01020304", ReachID: "67890", Streamflow: 4 },
      { HUC: "01020304", ReachID: "12345", Streamflow: 12.346 },
    ],
  }]);
  assert.equal(ui.run("message"), "Set reach 12345 in 1 time-step scenario(s) (1 in total).");
  assert.equal(ui.run("isError"), false);
  assert.equal(ui.run("rendered"), 1);
});

test("manual flow selection leaves the map view and highlighted reach untouched", () => {
  const reach = { HUC: "01020304", ReachID: "12345", Streamflow: 0 };
  const ui = scenarioState([{ ScenarioID: "manual", Reaches: [reach] }]);

  ui.run(`
    const mapSelection = { target: {}, highlight: {}, map: {} };
    const panel = { hidden: true, scrollIntoView() {} };
    const title = { textContent: "" };
    const content = {};
    let flowArgs;
    picker.target = mapSelection.target;
    picker.highlight = mapSelection.highlight;
    picker.map = mapSelection.map;
    $ = (selector) => ({
      "#flow-selection-title": title,
      "#flow-selection": panel,
      "#flow-selection-content": content,
    })[selector];
    showStreamflows = (...args) => { flowArgs = args; };
    openFlowSelection({ ScenarioID: "manual" }, state.scenarios[0].Reaches[0], 0);
  `);

  assert.deepEqual(ui.json("flowArgs[0]"), { reachId: "12345", huc: "01020304" });
  assert.equal(ui.run("flowArgs[1] === state.scenarios[0].Reaches[0]"), true);
  assert.equal(ui.run("flowArgs[3]()"), true);
  assert.equal(ui.run("picker.target === mapSelection.target"), true);
  assert.equal(ui.run("picker.highlight === mapSelection.highlight"), true);
  assert.equal(ui.run("picker.map === mapSelection.map"), true);
  assert.equal(ui.run("panel.hidden"), false);
  assert.equal(ui.run("title.textContent"), "Scenario manual, reach 1");
});

test("manual streamflow timestep selection rejects a non-HUC8 before mutating scenarios", () => {
  const reach = { HUC: "123", ReachID: "12345", Streamflow: 0 };
  const ui = scenarioState([{ ScenarioID: "manual", Reaches: [reach] }]);

  ui.run(`
    applyTimesteps(
      [{ time: "2020-01-01T00:00:00.000Z", flow: 12 }],
      { reachId: "12345", huc: "123" },
      state.scenarios[0].Reaches[0],
      closeFlowSelection,
    );
  `);

  assert.deepEqual(ui.json("state.scenarios"), [{
    ScenarioID: "manual",
    Reaches: [{ HUC: "123", ReachID: "12345", Streamflow: 0 }],
  }]);
  assert.equal(ui.run("message"), "Set the row's HUC8 before making time-step scenarios.");
  assert.equal(ui.run("isError"), true);
  assert.equal(ui.run("rendered"), 0);
});

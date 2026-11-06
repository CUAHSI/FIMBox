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
    const renderScenarioEditorActual = renderScenarioEditor;
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
    const reach = { ReachID: "12345", HUC: "01020304", Streamflow: "" };
    const scenario = { ScenarioID: "test", Reaches: [reach] };
    state.scenarios = [scenario];
    let requests = [];
    fetchPublicJson = async (url) => { requests.push(url); return {}; };
    const textOf = (node) => typeof node === "string" ? node
      : [node.textContent || node.text || "", ...(node.children || []).map(textOf)].join(" ");
    const visit = (node) => [node, ...(node.children || []).flatMap(visit)];
    const selectSource = (key) => {
      const select = box.children[1].children[0];
      select.value = key;
      select.listeners.change();
    };
  `);
  return ui;
}

test("execution-mode selector defaults to the first configured mode and updates concurrency controls", () => {
  const ui = flowPanel();
  ui.run(`
    state.config = { execution_modes: ["streaming-2026", "streaming-2024", "fimserv-2026", "fimserv-2024"] };
    const modes = el("div", { value: "" });
    modes.append = (option) => {
      modes.children.push(option);
      if (!modes.value) modes.value = option.value;
    };
    const form = { elements: { execution_mode: modes, max_procs: {} } };
    const hint = {};
    $ = (selector) => selector === "#job-options" ? form : hint;
    setupJobOptions();
  `);
  assert.deepEqual(ui.json("modes.children.map((option) => option.value)"),
    ["streaming-2026", "streaming-2024", "fimserv-2026", "fimserv-2024"]);
  assert.equal(ui.run("modes.value"), "streaming-2026");
  assert.equal(ui.run("form.elements.max_procs.disabled"), true);
  ui.run('modes.value = "fimserv-2026"; modes.listeners.change()');
  assert.equal(ui.run("form.elements.max_procs.disabled"), false);
});

test("source selector displays and requests only the chosen source, including metadata", async () => {
  const ui = flowPanel();
  ui.run("showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);");
  await ui.run("new Promise((resolve) => resolve())");
  assert.deepEqual(ui.json("requests"), ["https://api.water.noaa.gov/nwps/v1/reaches/12345/streamflow"]);
  assert.match(ui.run("textOf(box.children[2])"), /analysis and assimilation/);
  assert.match(ui.run("textOf(box.children[3])"), /No streamflow samples.*12345/);
  assert.doesNotMatch(ui.run("textOf(box.children[3])"), /Load streamflow/);

  ui.run("selectSource('shortRange');");
  await ui.run("Promise.resolve()");
  assert.equal(ui.json("requests").length, 2);
  assert.match(ui.json("requests")[1], /\?series=short_range$/);
  assert.match(ui.run("textOf(box.children[2])"), /18 hours/);
  assert.doesNotMatch(ui.run("textOf(box.children[2])"), /retrospective simulation/);
  assert.doesNotMatch(ui.run("textOf(box.children[3])"), /Load streamflow/);

  ui.run("selectSource('retrospective');");
  assert.equal(ui.json("requests").length, 2);
  const metadata = ui.run("textOf(box.children[2])");
  assert.match(metadata, /public AWS Zarr archive/);
  assert.match(metadata, /Hourly/);
  assert.match(metadata, /1979-02-01 to 2023-02-01/);
  assert.match(metadata, /92 days/);
  assert.match(ui.run("textOf(box.children[3])"), /Load streamflow/);
  assert.doesNotMatch(ui.run("textOf(box.children[3])"), /Use peak/);
});

test("switching sources ignores old requests even when returning to the same source", async () => {
  const ui = flowPanel();
  ui.run(`
    let pending = [];
    fetchPublicJson = () => new Promise((resolve) => pending.push(resolve));
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("retrospective");
    selectSource("analysisAssimilation");
    pending[0]({});
  `);
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /Loading recent analysis/);
  ui.run("pending[1]({});");
  await ui.run("Promise.resolve()");
  await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /No streamflow samples/);
});

test("scenario source reports coverage and applies one exact UTC time to every reach", async () => {
  const ui = flowPanel();
  ui.run(`
    let flowSelectionClosed = false;
    closeFlowSelection = () => { flowSelectionClosed = true; };
    const second = { HUC: "05060708", ReachID: "67890", Streamflow: 7 };
    scenario.Reaches.push(second);
    fetchPublicJson = async (url) => ({ shortRange: { series: { units: "m3/s", data: [
      { validTime: "2020-01-01T03:00:00Z", flow: url.includes("67890") ? 15 : 9 },
      { validTime: "2020-01-01T00:00:00Z", flow: 2 },
    ] } } });
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("shortRange");
  `);
  for (let i = 0; i < 6; i++) await ui.run("Promise.resolve()");
  const display = ui.run("textOf(box.children[3])");
  assert.match(display, /2020-01-01 00:00 to 2020-01-01 03:00/);
  assert.match(display, /180 minutes between samples/);
  assert.match(display, /2 sample/);
  assert.match(display, /Sample set 1 \(2 reaches\)/);
  assert.equal((display.match(/Available times \(UTC\)/g) || []).length, 1);
  assert.match(display, /Reach 12345 \(HUC 01020304\).*Reach 67890 \(HUC 05060708\)/);
  assert.match(display, /Apply to whole scenario/);
  assert.equal(ui.run("reach.Streamflow"), "");
  ui.run(`
    visit(box).find((node) => node["aria-label"] === "Scenario streamflow time (UTC)").value = "2020-01-01T03:00:00.000Z";
    visit(box).find((node) => node.text === "Apply to whole scenario").onclick();
  `);
  assert.equal(ui.run("reach.Streamflow"), 9);
  assert.equal(ui.run("second.Streamflow"), 15);
  assert.equal(ui.run("flowSelectionClosed"), true);
});

test("sample sets match exact UTC timestamps rather than coverage bounds, counts, or flow values", () => {
  const ui = flowPanel();
  ui.run(`
    const reaches = [reach,
      { HUC: "05060708", ReachID: "67890" },
      { HUC: "09080706", ReachID: "54321" }];
    const points = (hours, flow) => hours.map((hour) => ({
      time: new Date(Date.UTC(2020, 0, 1, hour)).toISOString(), flow,
    }));
    const series = new Map([
      [reach, points([0, 1, 3], 2)],
      [reaches[1], points([0, 2, 3], 2)],
      [reaches[2], [
        { time: "2019-12-31T19:00:00-05:00", flow: 99 },
        ...points([3, 1, 1], 99),
      ]],
    ]);
    const groups = groupedFlowCoverage(reaches, series);
  `);
  assert.equal(ui.run("groups.length"), 2);
  assert.match(ui.run("textOf(groups[0])"), /Sample set 1 \(2 reaches\)/);
  assert.match(ui.run("textOf(groups[0])"), /Reach 12345.*Reach 54321/);
  assert.doesNotMatch(ui.run("textOf(groups[0])"), /Reach 67890/);
  assert.match(ui.run("textOf(groups[0])"), /3 sample/);
  assert.match(ui.run("textOf(groups[1])"), /Sample set 2 \(1 reach\)/);
  assert.match(ui.run("textOf(groups[1])"), /Reach 67890/);
});

test("source errors stay visible and closed-panel responses are ignored", async () => {
  const ui = flowPanel();
  ui.run(`
    fetchPublicJson = async () => { throw new Error("Service unavailable"); };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
  `);
  for (let i = 0; i < 4; i++) await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /Streamflow lookup failed: Reach 12345: Service unavailable/);
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

test("adding one scenario copies every reach from the reach set", () => {
  const ui = scenarioState([{ ScenarioID: "scenario-1", Reaches: [] }]);
  ui.run(`
    state.reachSet = [
      { HUC: "01020304", ReachID: "12345" },
      { HUC: "05060708", ReachID: "67890" },
    ];
    addScenarioFromReachSet();
  `);

  assert.deepEqual(ui.json("state.scenarios"), [
    { ScenarioID: "scenario-1", Reaches: [] },
    { ScenarioID: "scenario-2", Reaches: [
      { HUC: "01020304", ReachID: "12345", Streamflow: "" },
      { HUC: "05060708", ReachID: "67890", Streamflow: "" },
    ] },
  ]);
  assert.equal(ui.run("message"), "Added scenario-2 with all 2 reaches.");
  assert.equal(ui.run("isError"), false);
  assert.equal(ui.run("rendered"), 1);
});

test("reach set builds complete shared-timestamp scenarios", () => {
  const ui = scenarioState([
    { ScenarioID: "manual", Reaches: [{ HUC: "11111111", ReachID: "99998", Streamflow: 7 }] },
    { ScenarioID: "20200101T0000Z", Reaches: [
      { HUC: "11111111", ReachID: "99999", Streamflow: 7 },
    ] },
  ]);
  ui.run(`
    el = (tag, props = {}, ...children) => {
      const node = {
        tag, ...props, children: children.flat(), listeners: {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
      };
      if (tag === "select") node.value = node.children[0].value;
      return node;
    };
    const first = { HUC: "01020304", ReachID: "12345" };
    const second = { HUC: "05060708", ReachID: "67890" };
    state.reachSet = [first, second];
    const series = new Map();
    series.set(first, [
      { time: "2020-01-01T00:00:00.000Z", flow: 1.12345 },
      { time: "2020-01-01T01:00:00.000Z", flow: 2 },
      { time: "2020-01-01T02:00:00.000Z", flow: 3 },
    ]);
    series.set(second, [
      { time: "2020-01-01T00:00:00.000Z", flow: 4 },
      { time: "2020-01-01T02:00:00.000Z", flow: 5 },
    ]);
    const batch = renderTimestepBatch(state.reachSet, series, () => true);
    const visit = (node) => [node, ...node.children.flatMap(visit)];
    let selectedTab;
    activateTab = (group, name) => { selectedTab = { group, name }; };
    visit(batch).find((node) => node.text === "Build timestep scenarios").listeners.click();
  `);

  assert.deepEqual(ui.json("state.scenarios"), [
    { ScenarioID: "manual", Reaches: [
      { HUC: "11111111", ReachID: "99998", Streamflow: 7 },
    ] },
    { ScenarioID: "20200101T0000Z", Reaches: [
      { HUC: "01020304", ReachID: "12345", Streamflow: 1.123 },
      { HUC: "05060708", ReachID: "67890", Streamflow: 4 },
    ] },
    { ScenarioID: "20200101T0200Z", Reaches: [
      { HUC: "01020304", ReachID: "12345", Streamflow: 3 },
      { HUC: "05060708", ReachID: "67890", Streamflow: 5 },
    ] },
  ]);
  assert.equal(ui.run("message"), "Built 2 timestep scenario(s), each with 2 reaches.");
  assert.deepEqual(ui.json("selectedTab"), { group: "reach-tools", name: "scenarios" });
  assert.equal(ui.run("state.reachSet.length"), 2);
});

test("multi-reach timestep selection excludes non-shared timestamps", () => {
  const ui = scenarioState([
    { ScenarioID: "manual", Reaches: [] },
  ]);
  ui.run(`
    el = (tag, props = {}, ...children) => ({ tag, ...props, children: children.flat() });
    const first = { HUC: "01020304", ReachID: "12345", Streamflow: 0 };
    const second = { HUC: "05060708", ReachID: "67890", Streamflow: 0 };
    state.reachSet = [first, second];
    const series = new Map();
    series.set(first, [
      { time: "2020-01-01T00:00:00.000Z", flow: 1 },
    ]);
    series.set(second, [
      { time: "2020-01-01T01:00:00.000Z", flow: 2 },
    ]);
  `);

  assert.equal(ui.run("sharedTimesteps(state.reachSet, series).length"), 0);
  assert.equal(ui.run("renderTimestepBatch(state.reachSet, series, () => true).text"), "No exact UTC timestamps are shared by every reach in the set. Choose different sources or time ranges.");
  assert.equal(ui.run("state.reachSet.length"), 2);
});

test("scenario flow selection leaves the map untouched and invalidates changed scenarios", () => {
  const reach = { HUC: "01020304", ReachID: "12345", Streamflow: 0 };
  const ui = scenarioState([]);

  ui.run(`
    const scenario = { ScenarioID: "test", Reaches: [${JSON.stringify(reach)}] };
    state.scenarios = [scenario];
    const mapSelection = { highlight: {}, map: {} };
    const panel = { hidden: true, scrollIntoView() {} };
    const title = { textContent: "" };
    const content = {};
    let flowArgs;
    picker.highlight = mapSelection.highlight;
    picker.map = mapSelection.map;
    $ = (selector) => ({
      "#flow-selection-title": title,
      "#flow-selection": panel,
      "#flow-selection-content": content,
    })[selector];
    showStreamflows = (...args) => { flowArgs = args; };
    openFlowSelection(scenario);
  `);

  assert.equal(ui.run("flowArgs[0] === scenario"), true);
  assert.equal(ui.run("flowArgs[1][0] === scenario.Reaches[0]"), true);
  assert.equal(ui.run("flowArgs[3]()"), true);
  assert.equal(ui.run("picker.highlight === mapSelection.highlight"), true);
  assert.equal(ui.run("picker.map === mapSelection.map"), true);
  assert.equal(ui.run("panel.hidden"), false);
  assert.equal(ui.run("title.textContent"), "Select streamflow for scenario test");
  ui.run('scenario.Reaches[0].ReachID = "99999"');
  assert.equal(ui.run("flowArgs[3]()"), false);
});

test("map reach selection keeps streamflow controls out of the map", async () => {
  const ui = scenarioState([]);
  ui.run(`
    el = (tag, props = {}) => ({ tag, ...props });
    const result = {
      children: [],
      replaceChildren(...children) { this.children = children; },
    };
    $ = () => result;
    let flowPanelCount = 0;
    lookupReach = async () => ({
      feature: {},
      reachId: "12345",
      huc: "01020304",
      hucName: "Example",
    });
    fetchPublicJson = async () => ({ features: [{}] });
    highlightReach = () => {};
    showStreamflows = () => { flowPanelCount += 1; };
  `);

  await ui.run("pickReachAt({ lat: 1, lng: 2 })");
  assert.equal(ui.run("flowPanelCount"), 0);
});

test("scenario creation rejects an invalid HUC before changing scenarios", () => {
  const reach = { HUC: "123", ReachID: "12345", Streamflow: 0 };
  const ui = scenarioState([]);

  ui.run(`
    state.reachSet = [${JSON.stringify(reach)}];
    addScenarioFromReachSet();
  `);

  assert.deepEqual(ui.json("state.scenarios"), []);
  assert.equal(ui.run("message"), "Every reach needs a unique numeric ReachID and an 8-digit HUC.");
  assert.equal(ui.run("isError"), true);
  assert.equal(ui.run("rendered"), 0);
});

test("adding a reach scrolls the reach list to its new row without scrolling on ordinary renders", () => {
  const ui = flowPanel();
  ui.run(`
    const editor = el("div", { scrollHeight: 1500, scrollTop: 200 });
    $ = (selector) => selector === "#reach-set-editor" ? editor : null;
    state.reachSet = [{ HUC: "01020304", ReachID: "12345" }];
    renderReachSet();
  `);
  assert.equal(ui.run("editor.scrollTop"), 200);
  ui.run(`
    const addButton = { addEventListener(event, callback) { this.click = callback; } };
    const selectEditor = $;
    $ = (selector) => selector === "#reach-set-add" ? addButton : selectEditor(selector);
  `);
  const handler = source.slice(source.indexOf('  $("#reach-set-add").addEventListener'),
    source.indexOf('  $("#reach-set-add-scenario").addEventListener'));
  ui.run(handler);
  ui.run("addButton.click()");
  assert.equal(ui.run("state.reachSet.length"), 2);
  assert.equal(ui.run("editor.scrollTop"), 1500);
  assert.equal(ui.run('visit(editor).filter((node) => node.tag === "tr").length'), 3);
});

test("reach set omits flows and scenario rows have editable independent flows", () => {
  const ui = flowPanel();
  ui.run(`
    const other = { ScenarioID: "other", Reaches: [{ HUC: "01020304", ReachID: "12345", Streamflow: 5 }] };
    state.scenarios.push(other);
    state.reachSet = [{ HUC: "01020304", ReachID: "12345" }];
    const reachEditor = el("div");
    const scenarioEditor = el("div");
    const jsonText = {};
    $ = (selector) => ({
      "#reach-set-editor": reachEditor,
      "#scenario-editor": scenarioEditor,
      "#json-text": jsonText,
    })[selector];
    renderReachSet();
    renderScenarioEditorActual();
  `);
  assert.equal(ui.run('visit(reachEditor).filter((node) => node.type === "number").length'), 0);
  assert.doesNotMatch(ui.run("textOf(reachEditor)"), /Select streamflow|Streamflow/);
  assert.equal(ui.run('visit(reachEditor).filter((node) => node.tag === "input").every((node) => node.onchange !== renderReachSet)'), true);
  assert.equal(ui.run('visit(scenarioEditor).filter((node) => node.text === "Select streamflow").length'), 2);
  ui.run(`
    const input = visit(scenarioEditor).find((node) => node.type === "number");
    input.oninput({ target: { value: "12.5" } });
  `);
  assert.equal(ui.run("scenario.Reaches[0].Streamflow"), 12.5);
  assert.equal(ui.run("other.Reaches[0].Streamflow"), 5);
  assert.equal(ui.run("JSON.parse(jsonText.value)[0].Reaches[0].Streamflow"), 12.5);
});

test("missing streamflow for one reach never partially updates a scenario", async () => {
  const ui = flowPanel();
  ui.run(`
    scenario.Reaches.push({ HUC: "05060708", ReachID: "67890", Streamflow: 8 });
    fetchPublicJson = async (url) => {
      if (url.includes("67890")) throw new Error("Unavailable");
      return { analysisAssimilation: { series: { data: [{ validTime: "2020-01-01T00:00:00Z", flow: 9 }] } } };
    };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
  `);
  for (let i = 0; i < 6; i++) await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box)"), /Reach 67890: Unavailable.*No flows were changed/);
  assert.doesNotMatch(ui.run("textOf(box)"), /Apply to whole scenario/);
  assert.deepEqual(ui.json("scenario.Reaches.map((reach) => reach.Streamflow)"), ["", 8]);
});

test("scenario selection offers only shared exact times and refuses stale or partial application", () => {
  const ui = flowPanel();
  ui.run(`
    let flowSelectionClosed = false;
    closeFlowSelection = () => { flowSelectionClosed = true; };
    const second = { HUC: "05060708", ReachID: "67890", Streamflow: 8 };
    scenario.Reaches.push(second);
    const series = new Map([
      [reach, [{ time: "2020-01-01T00:00:00Z", flow: 2 }]],
      [second, [{ time: "2020-01-01T01:00:00Z", flow: 3 }]],
    ]);
    const times = renderScenarioFlowTimes(scenario, scenario.Reaches, series, () => true);
    applyScenarioFlows(scenario, { reaches: [{ target: reach, point: { flow: 2 } }] }, () => true);
  `);
  assert.match(ui.run("textOf(times)"), /No exact UTC timestamps/);
  assert.deepEqual(ui.json("scenario.Reaches.map((reach) => reach.Streamflow)"), ["", 8]);
  assert.equal(ui.run("isError"), true);
  ui.run(`
    applyScenarioFlows(scenario, {
      time: "2020-01-01T00:00:00Z",
      reaches: scenario.Reaches.map((target) => ({ target, point: { flow: 7 } })),
    }, () => false);
  `);
  assert.deepEqual(ui.json("scenario.Reaches.map((reach) => reach.Streamflow)"), ["", 8]);
  assert.match(ui.run("message"), /scenario changed/);
  assert.equal(ui.run("flowSelectionClosed"), false);
});

test("retrospective group lookup excludes missing samples and applies each reach at a shared UTC time", async () => {
  const ui = flowPanel();
  ui.run(`
    scenario.Reaches.push({ HUC: "05060708", ReachID: "67890", Streamflow: "" });
    let historicRequests = [];
    api = async (url) => {
      historicRequests.push(url);
      return { start: "2023-01-01T00:00:00Z", flows: url.includes("67890") ? [null, 8] : [2, 4] };
    };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("retrospective");
    visit(box).find((node) => node.text === "Load streamflow").listeners.click();
  `);
  for (let i = 0; i < 6; i++) await ui.run("Promise.resolve()");
  assert.equal(ui.run("historicRequests.length"), 2);
  assert.match(ui.json("historicRequests")[0], /reach_id=12345&start=2023-01-01&end=2023-01-31/);
  const options = ui.json('visit(box).find((node) => node["aria-label"] === "Scenario streamflow time (UTC)").children.map((node) => node.value)');
  assert.deepEqual(options, ["2023-01-01T01:00:00.000Z"]);
  ui.run('visit(box).find((node) => node.text === "Apply to whole scenario").onclick()');
  assert.deepEqual(ui.json("scenario.Reaches.map((reach) => reach.Streamflow)"), [4, 8]);
});

test("historical NWM loads reaches in one request, labels products, and only applies complete samples", async () => {
  const ui = flowPanel();
  ui.run(`
    scenario.Reaches.push({ HUC: "05060708", ReachID: "67890", Streamflow: "" });
    const historicalRequests = [];
    api = async (url, options) => {
      historicalRequests.push({ url, options });
      return { units: "m3/s", samples: [
        { time: "2023-02-01T00:00:00Z", source: "nwm_v3_retrospective", hour_issue: null,
          flows_cms: { "12345": 2, "67890": 0 } },
        { time: "2023-02-01T01:00:00Z", source: "nwm_archived_analysis", hour_issue: null,
          flows_cms: { "12345": 3, "67890": null } },
        { time: "2023-02-01T02:00:00Z", source: "nwm_archived_analysis",
          hour_issue: "archive_hour_unavailable", flows_cms: { "12345": null, "67890": null } },
      ] };
    };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("historicalNwm");
  `);
  const inputs = ui.json('visit(box).filter((node) => node.type === "datetime-local").map((node) => node.value)');
  assert.equal(inputs.length, 2);
  assert.equal(inputs[0], inputs[1]);
  assert.match(ui.run("textOf(box.children[2])"), /up to 96 hours/);
  assert.match(ui.run("textOf(box.children[2])"), /100 distinct reaches/);
  ui.run("visit(box).find((node) => node.text === 'Load streamflow').listeners.click();");
  for (let i = 0; i < 8; i++) await ui.run("Promise.resolve()");
  assert.equal(ui.run("historicalRequests.length"), 1);
  assert.equal(ui.run("historicalRequests[0].url"), "/ui/streamflow/historical");
  assert.deepEqual(ui.json("historicalRequests[0].options.json"), {
    reach_ids: ["12345", "67890"],
    start: new Date(inputs[0] + ":00Z").toISOString(),
    end: new Date(inputs[1] + ":00Z").toISOString(),
  });
  const display = ui.run("textOf(box.children[3])");
  assert.match(display, /Coverage for this lookup/);
  assert.match(display, /NWM v3.0 retrospective/);
  assert.match(display, /NWM archived analysis/);
  assert.match(display, /1 unavailable archive hour\(s\); 1 missing reach sample\(s\)/);
  assert.match(display, /2023-02-01T00:00:00Z — NWM v3.0 retrospective/);
  const options = ui.json('visit(box).find((node) => node["aria-label"] === "Scenario streamflow time (UTC)").children.map((node) => node.text)');
  assert.deepEqual(options, ["2023-02-01T00:00:00.000Z — NWM v3.0 retrospective"]);
  ui.run(`
    const batch = renderTimestepBatch([scenario.Reaches[0]], new Map([[scenario.Reaches[0], [
      { time: "2023-02-01T01:00:00Z", flow: 3, source: "nwm_archived_analysis" },
    ]]]), () => true);
  `);
  assert.match(ui.run("textOf(batch)"), /2023-02-01T01:00:00\.000Z — NWM archived analysis/);
  ui.run('visit(box).find((node) => node.text === "Apply to whole scenario").onclick()');
  assert.deepEqual(ui.json("scenario.Reaches.map((reach) => reach.Streamflow)"), [2, 0]);
});

test("historical ranges through 96 hours are merged from non-overlapping 24-sample requests", async () => {
  for (const hours of [0, 23, 24, 72, 96]) {
    const ui = flowPanel();
    ui.run(`
      const historicalRequests = [];
      api = async (url, options) => {
        historicalRequests.push(options.json);
        const first = Date.parse(options.json.start), last = Date.parse(options.json.end);
        return { samples: Array.from({ length: (last - first) / HOUR_MS + 1 }, (_, index) => {
          const time = new Date(first + index * HOUR_MS).toISOString();
          return {
            time, source: time < "2023-02-01T01:00:00.000Z" ? "nwm_v3_retrospective" : "nwm_archived_analysis",
            hour_issue: null, flows_cms: { "12345": time === "2023-02-01T01:00:00.000Z" ? null : index },
          };
        }) };
      };
      showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
      selectSource("historicalNwm");
      const inputs = visit(box).filter((node) => node.type === "datetime-local");
      inputs[0].value = "2023-01-31T01:00";
      inputs[1].value = utcInputValue(Date.parse(inputs[0].value + ":00Z") + ${hours} * HOUR_MS);
    `);
    await ui.run('visit(box).find((node) => node.text === "Load streamflow").listeners.click()');
    const requests = ui.json("historicalRequests");
    const first = Date.parse("2023-01-31T01:00:00Z");
    assert.equal(requests.length, Math.ceil((hours + 1) / 24));
    for (let index = 0; index < requests.length; index++) {
      assert.deepEqual(requests[index], {
        reach_ids: ["12345"],
        start: new Date(first + index * 24 * 3600000).toISOString(),
        end: new Date(first + Math.min(hours, index * 24 + 23) * 3600000).toISOString(),
      });
    }
    const options = ui.json('visit(box).find((node) => node["aria-label"] === "Scenario streamflow time (UTC)").children.map((node) => node.value)');
    assert.equal(options.length, hours + 1 - (hours >= 24 ? 1 : 0));
    assert.equal(options[0], new Date(first).toISOString());
    assert.equal(options.at(-1), new Date(first + hours * 3600000 - (hours === 24 ? 3600000 : 0)).toISOString());
    if (hours >= 24) {
      assert.match(ui.run("textOf(box.children[3])"), /1 missing reach sample/);
      assert.ok(!options.includes("2023-02-01T01:00:00.000Z"));
    }
  }
});

test("historical ranges exceeding 96 hours or not on the hour are rejected before requests", async () => {
  const ui = flowPanel();
  ui.run(`
    const historicalRequests = [];
    api = async (url, options) => { historicalRequests.push(options.json); };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("historicalNwm");
    const inputs = visit(box).filter((node) => node.type === "datetime-local");
    inputs[0].value = "2025-07-04T00:00";
    inputs[1].value = "2025-07-08T01:00";
  `);
  await ui.run('visit(box).find((node) => node.text === "Load streamflow").listeners.click()');
  assert.match(ui.run("textOf(box.children[3])"), /at most 96 hours.*97 hours/);
  ui.run('inputs[1].value = "2025-07-04T01:30"');
  await ui.run('visit(box).find((node) => node.text === "Load streamflow").listeners.click()');
  assert.match(ui.run("textOf(box.children[3])"), /times on the hour/);
  assert.equal(ui.run("historicalRequests.length"), 0);
});

test("historical chunks preserve coverage and shared timestamps for the reach set builder", async () => {
  const ui = flowPanel();
  ui.run(`
    const second = { HUC: "05060708", ReachID: "67890" };
    const selected = [reach, second];
    api = async (url, options) => {
      const start = Date.parse(options.json.start), end = Date.parse(options.json.end);
      return { samples: Array.from({ length: (end - start) / HOUR_MS + 1 }, (_, index) => {
        const time = new Date(start + index * HOUR_MS).toISOString();
        const missing = time === "2023-02-01T01:00:00.000Z";
        return { time, source: time < "2023-02-01T01:00:00.000Z"
          ? "nwm_v3_retrospective" : "nwm_archived_analysis",
          hour_issue: missing ? "archive_hour_unavailable" : null,
          flows_cms: { "12345": missing ? null : 1, "67890": missing ? null : 2 } };
      }) };
    };
    showStreamflows(null, selected, box, () => panelCurrent);
    selectSource("historicalNwm");
    const inputs = visit(box).filter((node) => node.type === "datetime-local");
    inputs[0].value = "2023-01-31T01:00";
    inputs[1].value = "2023-02-01T02:00";
  `);
  await ui.run('visit(box).find((node) => node.text === "Load streamflow").listeners.click()');
  const text = ui.run("textOf(box.children[3])");
  assert.match(text, /1 unavailable archive hour\(s\); 0 missing reach sample\(s\)/);
  assert.match(text, /2023-02-01T00:00:00.000Z — NWM v3.0 retrospective/);
  assert.match(text, /2023-02-01T02:00:00.000Z — NWM archived analysis/);
  const batchTimes = ui.json('visit(box).filter((node) => node.tag === "li").map((node) => node.text)');
  assert.ok(batchTimes.includes("2023-02-01T01:00:00.000Z — NWM archived analysis; archive hour unavailable"));
  assert.ok(!batchTimes.includes("2023-02-01T01:00:00.000Z — NWM archived analysis"));
  assert.equal(ui.run("scenario.Reaches[0].Streamflow"), "");
});

test("a failed historical chunk discards earlier results and leaves scenario flows unchanged", async () => {
  const ui = flowPanel();
  ui.run(`
    reach.Streamflow = 42;
    let requestCount = 0;
    api = async () => {
      if (++requestCount === 2) throw new Error("archive unavailable");
      return { samples: [{ time: "2025-07-04T00:00:00Z", source: "nwm_archived_analysis",
        hour_issue: null, flows_cms: { "12345": 9 } }] };
    };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("historicalNwm");
    const inputs = visit(box).filter((node) => node.type === "datetime-local");
    inputs[0].value = "2025-07-04T00:00";
    inputs[1].value = "2025-07-07T00:00";
  `);
  await ui.run('visit(box).find((node) => node.text === "Load streamflow").listeners.click()');
  assert.equal(ui.run("requestCount"), 2);
  assert.match(ui.run("textOf(box.children[3])"), /archive unavailable.*No flows were changed/);
  assert.equal(ui.run("reach.Streamflow"), 42);
  assert.equal(ui.run('visit(box).some((node) => node["aria-label"] === "Scenario streamflow time (UTC)")'), false);
  assert.equal(ui.run('visit(box).find((node) => node.text === "Load streamflow").disabled'), false);
});

test("changing a historical range during a later chunk stops further requests and discards results", async () => {
  const ui = flowPanel();
  ui.run(`
    let finishChunk;
    let requestCount = 0;
    api = async () => {
      if (++requestCount === 2) return new Promise((resolve) => { finishChunk = resolve; });
      return { samples: [] };
    };
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("historicalNwm");
    const inputs = visit(box).filter((node) => node.type === "datetime-local");
    inputs[0].value = "2025-07-04T00:00";
    inputs[1].value = "2025-07-07T00:00";
    const pending = visit(box).find((node) => node.text === "Load streamflow").listeners.click();
  `);
  await ui.run("Promise.resolve()");
  assert.equal(ui.run("requestCount"), 2);
  assert.match(ui.run("textOf(box.children[3])"), /request 2 of 4/);
  ui.run(`
    inputs[0].value = "2025-07-04T01:00";
    inputs[0].listeners.input();
    finishChunk({ samples: [] });
  `);
  await ui.run("pending");
  assert.equal(ui.run("requestCount"), 2);
  assert.match(ui.run("textOf(box.children[3])"), /Date range changed/);
  assert.equal(ui.run('visit(box).find((node) => node.text === "Load streamflow").disabled'), false);
});

test("changing a historical UTC hour invalidates the pending response", async () => {
  const ui = flowPanel();
  ui.run(`
    let finishHistorical;
    api = () => new Promise((resolve) => { finishHistorical = resolve; });
    showStreamflows(scenario, scenario.Reaches, box, () => panelCurrent);
    selectSource("historicalNwm");
    visit(box).find((node) => node.text === "Load streamflow").listeners.click();
    const input = visit(box).find((node) => node.type === "datetime-local");
    input.value = "2023-02-01T01:00";
    input.listeners.input();
    finishHistorical({ units: "m3/s", samples: [] });
  `);
  for (let i = 0; i < 4; i++) await ui.run("Promise.resolve()");
  assert.match(ui.run("textOf(box.children[3])"), /Date range changed/);
  assert.equal(ui.run('visit(box).some((node) => node["aria-label"] === "Scenario streamflow time (UTC)")'), false);
});

test("timestep limit disables generation above 240 shared timestamps", () => {
  const ui = flowPanel();
  ui.run(`
    const points = Array.from({ length: 241 }, (_, index) => ({
      time: new Date(Date.parse("2020-01-01T00:00:00Z") + index * HOUR_MS).toISOString(),
      flow: index,
    }));
    const batch = renderTimestepBatch([reach], new Map([[reach, points]]), () => true);
  `);
  assert.equal(ui.run('visit(batch).find((node) => node.text === "Build timestep scenarios").disabled'), true);
  assert.match(ui.run("textOf(batch)"), /241 timesteps; choose at most 240/);
  ui.run(`
    const interval = visit(batch).find((node) => node["aria-label"] === "Shared timestep interval");
    interval.value = "3";
    interval.listeners.input();
  `);
  assert.equal(ui.run('visit(batch).find((node) => node.text === "Build timestep scenarios").disabled'), false);
  assert.match(ui.run("textOf(batch)"), /81 shared timestep/);
});

test("submission rejects blank, negative, and nonfinite scenario flows before posting", async () => {
  const ui = flowPanel();
  ui.run(`
    state.workspace = {};
    authReady = () => true;
    canWriteWorkspace = () => true;
    $ = () => ({ hidden: true });
    let posts = 0;
    api = async () => { posts += 1; };
  `);
  for (const value of ['""', "-1", "NaN", "Infinity"]) {
    await ui.run(`
      reach.Streamflow = ${value};
      submitJob({ preventDefault() {}, target: {} });
    `);
    assert.equal(ui.run("isError"), true);
    assert.match(ui.run("message"), /finite nonnegative streamflow for every reach/);
  }
  assert.equal(ui.run("posts"), 0);
});

test("loading an example replaces the reach set with unique reaches across its scenarios", () => {
  const ui = scenarioState([]);
  ui.run(`
    state.reachSet = [{ HUC: "99999999", ReachID: "99999" }];
    const example = { scenarios: [
      { ScenarioID: "first", Reaches: [
        { HUC: "01020304", ReachID: "12345", Streamflow: 4 },
        { HUC: "05060708", ReachID: "67890", Streamflow: 8 },
      ] },
      { ScenarioID: "second", Reaches: [
        { HUC: "01020304", ReachID: "12345", Streamflow: 12 },
        { HUC: "03020201", ReachID: "34567", Streamflow: 16 },
      ] },
    ] };
    let cleared = 0;
    let reachRenders = 0;
    $ = () => ({ replaceChildren() { cleared += 1; } });
    renderReachSet = () => { reachRenders += 1; };
    let selectedTab;
    activateTab = (group, name) => { selectedTab = { group, name }; };
    loadExample(example);
  `);
  assert.deepEqual(ui.json("state.reachSet"), [
    { HUC: "01020304", ReachID: "12345" },
    { HUC: "05060708", ReachID: "67890" },
    { HUC: "03020201", ReachID: "34567" },
  ]);
  assert.deepEqual(ui.json("state.scenarios"), ui.json("example.scenarios"));
  assert.equal(ui.run("cleared"), 1);
  assert.equal(ui.run("reachRenders"), 1);
  assert.equal(ui.run("flowSelection.batchLookup"), 1);
  assert.deepEqual(ui.json("selectedTab"), { group: "reach-tools", name: "scenarios" });
  ui.run('state.reachSet[0].HUC = "11111111"');
  assert.equal(ui.run("state.scenarios[0].Reaches[0].HUC"), "01020304");
  assert.equal(ui.run("example.scenarios[0].Reaches[0].HUC"), "01020304");
});

test("initial example loading also populates the reach set", async () => {
  const ui = scenarioState([]);
  ui.run(`
    const select = { append() {} };
    $ = (selector) => selector === "#example-select" ? select : { replaceChildren() {} };
    el = () => ({});
    renderReachSet = () => {};
    let selectedTab;
    activateTab = (group, name) => { selectedTab = { group, name }; };
    api = async () => [{ name: "single_reach", scenarios: [
      { ScenarioID: "example", Reaches: [{ HUC: "01020304", ReachID: 12345, Streamflow: 4 }] },
    ] }];
  `);
  await ui.run("loadExamples()");
  assert.deepEqual(ui.json("state.reachSet"), [{ HUC: "01020304", ReachID: "12345" }]);
  assert.equal(ui.run("select.value"), "single_reach");
  assert.deepEqual(ui.json("selectedTab"), { group: "reach-tools", name: "scenarios" });
});

function reachMap() {
  const ui = flowPanel();
  ui.run(`
    const status = {};
    const result = el("div");
    $ = (selector) => selector === "#picker-status" ? status : result;
    let fitted = 0;
    let highlighted = null;
    picker.map = { fitBounds() { fitted += 1; } };
    picker.reaches = {
      features: [],
      clearLayers() { this.features = []; },
      addData(feature) { this.features.push(feature); },
      getBounds() { return { isValid: () => this.features.length > 0 }; },
    };
    const featureFor = (id) => ({ type: "Feature", properties: { comid: id },
      geometry: { type: "Point", coordinates: [-77, 35] } });
    fetchPublicJson = async (url) => {
      requests.push(url);
      return { features: [featureFor(url.split("/").at(-1))] };
    };
    highlightReach = (feature) => { highlighted = feature.properties.comid; };
  `);
  return ui;
}

test("map displays the whole reach set, focuses a row, and removes deleted reaches", async () => {
  const ui = reachMap();
  ui.run(`
    state.reachSet = [
      { HUC: "01020304", ReachID: "12345" },
      { HUC: "05060708", ReachID: "67890" },
    ];
  `);
  await ui.run("refreshReachMap(true)");
  assert.deepEqual(ui.json("picker.reaches.features.map((feature) => feature.properties.comid)"), ["12345", "67890"]);
  assert.equal(ui.run("fitted"), 1);
  assert.match(ui.run("status.textContent"), /2 of 2/);
  await ui.run('refreshReachMap(false, "67890")');
  assert.equal(ui.run("highlighted"), "67890");
  assert.equal(ui.run("requests.length"), 2);
  ui.run("state.reachSet.splice(0, 1)");
  await ui.run("refreshReachMap()");
  assert.deepEqual(ui.json("picker.reaches.features.map((feature) => feature.properties.comid)"), ["67890"]);
});

test("map clicks add reaches without changing scenarios and reject duplicate or stale additions", async () => {
  const ui = reachMap();
  ui.run(`
    renderReachSet = () => { rendered += 1; };
    lookupReach = async () => ({
      feature: featureFor("12345"), reachId: "12345", huc: "01020304", hucName: "Example",
    });
    scenario.Reaches[0].Streamflow = 4;
  `);
  await ui.run("pickReachAt({ lat: 1, lng: 2 })");
  ui.run('result.children.find((node) => node.text === "Add to reach set").onclick()');
  assert.deepEqual(ui.json("state.reachSet"), [{ HUC: "01020304", ReachID: "12345" }]);
  assert.equal(ui.run("scenario.Reaches[0].Streamflow"), 4);
  assert.equal(ui.run("rendered"), 1);
  ui.run('result.children.find((node) => node.text === "Add to reach set").onclick()');
  assert.equal(ui.run("state.reachSet.length"), 1);
  assert.match(ui.run("message"), /already in the reach set/);
  ui.run('picker.lookup++; result.children.find((node) => node.text === "Add to reach set").onclick()');
  assert.match(ui.run("message"), /map selection changed/);
});

test("map failures are visible and outdated map responses cannot restore removed reaches", async () => {
  const ui = reachMap();
  ui.run(`
    state.reachSet = [{ HUC: "01020304", ReachID: "12345" }];
    fetchPublicJson = async () => { throw new Error("Service unavailable"); };
  `);
  await ui.run("refreshReachMap()");
  assert.match(ui.run("status.textContent"), /Reach 12345: Service unavailable/);
  assert.equal(ui.run("status.className"), "error");
  ui.run(`
    let finish;
    fetchPublicJson = () => new Promise((resolve) => { finish = resolve; });
    const pending = refreshReachMap();
    state.reachSet = [];
  `);
  await ui.run("refreshReachMap()");
  ui.run('finish({ features: [featureFor("12345")] })');
  await ui.run("pending");
  assert.deepEqual(ui.json("picker.reaches.features"), []);
  assert.match(ui.run("status.textContent"), /Add reaches manually/);
});

test("reach tool tabs load timestep selection on mouse and keyboard entry", () => {
  const ui = flowPanel();
  ui.run(`
    const tabs = [
      { dataset: { tab: "timesteps" }, listeners: {}, focus() { focused = "timesteps"; },
        addEventListener(name, callback) { this.listeners[name] = callback; } },
      { dataset: { tab: "map" }, listeners: {}, focus() { focused = "map"; },
        addEventListener(name, callback) { this.listeners[name] = callback; } },
      { dataset: { tab: "scenarios" }, listeners: {}, focus() { focused = "scenarios"; },
        addEventListener(name, callback) { this.listeners[name] = callback; } },
    ];
    let focused = "";
    let activeTab = "";
    let mapOpened = 0;
    let timestepOpened = 0;
    document.querySelectorAll = () => tabs;
    activateTab = (group, name) => { activeTab = name; };
    openReachPicker = () => { mapOpened += 1; activeTab = "map"; };
    openFlowSelection = () => { timestepOpened += 1; activeTab = "timesteps"; };
    setupReachTabs();
    tabs[1].listeners.click();
  `);
  assert.equal(ui.run("activeTab"), "map");
  assert.equal(ui.run("mapOpened"), 1);
  ui.run('tabs[1].listeners.keydown({ key: "ArrowLeft", preventDefault() {} })');
  assert.equal(ui.run("focused"), "timesteps");
  assert.equal(ui.run("activeTab"), "timesteps");
  assert.equal(ui.run("timestepOpened"), 1);
  ui.run('tabs[0].listeners.keydown({ key: "End", preventDefault() {} })');
  assert.equal(ui.run("focused"), "scenarios");
  assert.equal(ui.run("activeTab"), "scenarios");
  ui.run('tabs[2].listeners.click()');
  assert.equal(ui.run("activeTab"), "scenarios");
  ui.run('tabs[2].listeners.keydown({ key: "ArrowRight", preventDefault() {} })');
  assert.equal(ui.run("activeTab"), "timesteps");
  assert.equal(ui.run("timestepOpened"), 2);
  assert.deepEqual(ui.json("requests"), []);
});

test("timestep tab reuses its selection until the reach set changes", () => {
  const ui = flowPanel();
  ui.run(`
    state.reachSet = [reach];
    let loaded = 0;
    const content = el("div");
    const tools = { scrollIntoView() {} };
    $ = (selector) => selector === "#timestep-content" ? content : tools;
    activateTab = () => {};
    showStreamflows = () => { loaded += 1; };
    openFlowSelection();
    openFlowSelection();
  `);
  assert.equal(ui.run("loaded"), 1);
  ui.run('reach.ReachID = "67890"; openFlowSelection()');
  assert.equal(ui.run("loaded"), 2);
  ui.run("state.reachSet = []; openFlowSelection()");
  assert.equal(ui.run("loaded"), 2);
  assert.match(ui.run("textOf(content)"), /Add at least one reach, with unique numeric ReachIDs/);
});

test("streamflow reach guidance stays in its tab and clears when reach inputs are resolved", () => {
  const ui = flowPanel();
  ui.run(`
    state.reachSet = [];
    const content = el("div");
    const editor = el("div");
    const panel = { hidden: false };
    const tools = { scrollIntoView() {} };
    $ = (selector) => ({
      "#timestep-content": content,
      "#timestep-panel": panel,
      "#reach-set-editor": editor,
    })[selector] || tools;
    activateTab = () => {};
    let loaded = 0;
    showStreamflows = () => {
      loaded += 1;
      content.replaceChildren(el("p", { text: "Source selector" }));
    };
    openFlowSelection();
  `);
  assert.equal(ui.run("message"), "");
  assert.match(ui.run("textOf(content)"), /Add at least one reach, with unique numeric ReachIDs and 8-digit HUCs/);
  ui.run(`
    state.reachSet.push({ HUC: "", ReachID: "" });
    renderReachSet();
    const inputs = visit(editor).filter((node) => node.tag === "input");
    inputs[0].oninput({ target: { value: "01020304" } });
    inputs[1].oninput({ target: { value: "12345" } });
  `);
  assert.equal(ui.run("loaded"), 1);
  assert.equal(ui.run("textOf(content)"), " Source selector");
  assert.equal(ui.run("message"), "");
  ui.run('inputs[0].oninput({ target: { value: "010" } })');
  assert.match(ui.run("textOf(content)"), /Add at least one reach/);
  ui.run(`
    panel.hidden = true;
    inputs[0].oninput({ target: { value: "01020304" } });
  `);
  assert.equal(ui.run("loaded"), 1);
  ui.run("panel.hidden = false; openFlowSelection()");
  assert.equal(ui.run("loaded"), 2);
  assert.doesNotMatch(ui.run("textOf(content)"), /Add at least one reach/);
});

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(join(__dirname, "../src/fimbox_api/static/ui/app.js"), "utf8");

function browser() {
  class Node {
    constructor(tag = "") {
      this.tag = tag;
      this.children = [];
      this.listeners = {};
      this.isConnected = true;
      this.textContent = "";
      this.hidden = false;
      this.checked = false;
      const classes = new Set();
      this.classList = {
        add(name) { classes.add(name); },
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      };
      this.dataset = {};
    }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    replaceChildren(...children) { this.children = children; }
    get firstElementChild() { return this.children.find(child => child instanceof Node) || null; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    setAttribute(name, value) { this[name] = value; }
    after(node) { this.nextSibling = node; }
  }
  const context = vm.createContext({
    Node, URLSearchParams, console,
    document: { addEventListener() {}, createElement: (tag) => new Node(tag) },
    sessionStorage: { getItem() { return "session"; } },
    window: {},
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.config = { authentication_enabled: true };
    let status = new Node(), container = new Node(), view = new Node();
    view.querySelector = (selector) => selector === ".map-status" ? status : container;
    let added = [], fits = [], controls = [], groups = [], tooltips = [], requests = [], mapListeners = {};
    let map = {
      setView() {}, invalidateSize() {}, getZoom() { return 12; }, setZoom() {},
      hasLayer(layer) { return added.includes(layer); },
      fitBounds(bounds) { fits.push(bounds); },
      on(names, callback) { for (const name of names.split(" ")) (mapListeners[name] ||= []).push(callback); },
      fire(type, layer) { for (const callback of mapListeners[type] || []) callback({type, layer}); },
      addLayer(layer) { if (!this.hasLayer(layer)) { added.push(layer); if (layer.listener) layer.listener(); this.fire("layeradd", layer); } },
      removeLayer(layer) { const i = added.indexOf(layer); if (i !== -1) { added.splice(i, 1); this.fire("layerremove", layer); } },
    };
    let bounds = { isValid() { return true; } };
    function control() {
      return { addTo() {
        const element = this.onAdd();
        groups.push({ element, reach: true });
        return this;
      } };
    }
    control.layers = () => {
      let element = new Node(), names = [], layers = [];
      groups.push({ element, names, layers });
      return {
        addTo() { return this; }, getContainer() { return element; },
        addOverlay(layer, name) { controls.push(name); names.push(name); layers.push(layer); },
      };
    };
    let L = {
      map() { return map; },
      control,
      DomEvent: { disableClickPropagation() {}, disableScrollPropagation() {} },
      layerGroup() {
        return {
          on(name, callback) { this.listener = callback; return this; },
          addTo() { map.addLayer(this); return this; },
          addLayer(layer) { this.features = layer; return this; },
        };
      },
      geoJSON(document, options) {
        for (const feature of document.features) {
          if (feature.geometry) options.onEachFeature(feature, { bindTooltip(node) { tooltips.push(node.textContent); } });
        }
        return { addTo() { added.push(this); return this; }, getBounds() { return bounds; } };
      },
    };
    window.L = L;
    osmLayer = () => ({ addTo() {} });
    authTileLayer = () => ({
      opacity: 0.8, setOpacity(value) { this.opacity = value; return this; },
      addTo() { map.addLayer(this); return this; }, bringToFront() {},
      isLoading() { return false; },
    });
  `, context);
  return {
    run: (code) => vm.runInContext(code, context),
    json: (code) => JSON.parse(vm.runInContext(`JSON.stringify(${code})`, context)),
  };
}

test("workspace job list keeps newest-first API ordering", async () => {
  const ui = browser();
  ui.run(`
    let list = new Node(), loading = new Node();
    $ = selector => selector === "#job-list" ? list : selector === "#jobs-loading" ? loading : null;
    state.workspace = { workspace_id: "a" };
    state.currentJob = "mid";
    authReady = () => true;
    updateCompareJobOptions = () => {};
    api = async () => ({jobs: [
      {job_id:"new",status:"success"}, {job_id:"mid",status:"started"}, {job_id:"old",status:"failure"}
    ]});
  `);
  await ui.run("loadJobs()");
  assert.deepEqual(ui.json("list.children.map(item => item.dataset.job)"), ["new", "mid", "old"]);
});

test("workspace jobs loading indicator preserves the list and clears on success or error", async () => {
  const ui = browser();
  ui.run(`
    let list = new Node(), loading = new Node(), pending = [];
    list.append(new Node());
    loading.hidden = true;
    $ = selector => selector === "#job-list" ? list : selector === "#jobs-loading" ? loading : null;
    state.workspace = { workspace_id: "a" };
    authReady = () => true;
    updateCompareJobOptions = () => {};
    api = () => new Promise((resolve, reject) => pending.push({resolve, reject}));
  `);
  const first = ui.run("loadJobs()");
  assert.equal(ui.run("loading.hidden"), false);
  assert.equal(ui.run("list.children.length"), 1);
  ui.run('pending[0].resolve({jobs: [{job_id:"new",status:"success"}]})');
  await first;
  assert.equal(ui.run("loading.hidden"), true);
  assert.equal(ui.run("list.children[0].dataset.job"), "new");
  const second = ui.run("loadJobs()");
  assert.equal(ui.run("loading.hidden"), false);
  ui.run('pending[1].reject(new Error("network unavailable"))');
  await second;
  assert.equal(ui.run("loading.hidden"), true);
  assert.match(ui.run("list.children[0].textContent"), /Could not list jobs: network unavailable/);
});

test("opening a workspace removes the workspace prompt while jobs are loading", async () => {
  const ui = browser();
  ui.run(`
    let list = new Node(), loading = new Node(), complete;
    $ = selector => selector === "#job-list" ? list : selector === "#jobs-loading" ? loading : null;
    authReady = () => true;
    updateCompareJobOptions = () => {};
    api = () => new Promise(resolve => { complete = resolve; });
    state.workspace = null;
    loadJobs();
  `);
  assert.match(ui.run("list.children[0].textContent"), /Open a workspace/);
  ui.run('state.workspace = {workspace_id:"a"}');
  const pending = ui.run("loadJobs()");
  assert.equal(ui.run("list.children.length"), 0);
  assert.equal(ui.run("loading.hidden"), false);
  ui.run("complete({jobs:[]})");
  await pending;
  assert.equal(ui.run("loading.hidden"), true);
  assert.match(ui.run("list.children[0].textContent"), /No jobs in this workspace/);
});

test("workspace job refreshes ignore older results and keep the latest loading indicator", async () => {
  const ui = browser();
  ui.run(`
    let list = new Node(), loading = new Node(), pending = [];
    $ = selector => selector === "#job-list" ? list : selector === "#jobs-loading" ? loading : null;
    state.workspace = { workspace_id: "a" };
    authReady = () => true;
    updateCompareJobOptions = () => {};
    api = () => new Promise(resolve => pending.push(resolve));
  `);
  const older = ui.run("loadJobs()");
  const newer = ui.run("loadJobs()");
  ui.run('pending[0]({jobs:[{job_id:"old",status:"success"}]})');
  await older;
  assert.equal(ui.run("loading.hidden"), false);
  assert.equal(ui.run("list.children.length"), 0);
  ui.run('pending[1]({jobs:[{job_id:"new",status:"success"}]})');
  await newer;
  assert.equal(ui.run("loading.hidden"), true);
  assert.equal(ui.run("list.children[0].dataset.job"), "new");
});

test("workspace job loading indicator resets when no workspace is active", async () => {
  const ui = browser();
  ui.run(`
    let list = new Node(), loading = new Node(), complete;
    $ = selector => selector === "#job-list" ? list : selector === "#jobs-loading" ? loading : null;
    state.workspace = { workspace_id: "a" };
    authReady = () => true;
    updateCompareJobOptions = () => {};
    api = () => new Promise(resolve => { complete = resolve; });
  `);
  const pending = ui.run("loadJobs()");
  ui.run("state.workspaceVersion++; state.workspace = null; loadJobs()");
  assert.equal(ui.run("loading.hidden"), true);
  ui.run('complete({jobs:[{job_id:"old",status:"success"}]})');
  await pending;
  assert.match(ui.run("list.children[0].textContent"), /Open a workspace/);
  assert.equal(ui.run("loading.hidden"), true);
});

test("GeoJSON Map button is offered in result browser without changing raster actions", async () => {
  const ui = browser();
  ui.run(`
    let calls = [];
    api = async () => ({ folders: [], files: [
      {name: "s.reaches.geojson", key: "out/s.reaches.geojson", size_bytes: 10},
      {name: "s.cog", key: "out/s.cog", size_bytes: 10},
      {name: "other.json", key: "out/other.json", size_bytes: 10},
    ] });
    let browserRoot = new Node();
    createBrowser(browserRoot, { bucket: "fim", root: "out",
      onRaster: (key) => calls.push(["raster", key]),
      onGeoJSON: (key) => calls.push(["vector", key]),
    });
  `);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ui.json("browserRoot.children[1].children[0].children.slice(1).map(row => row.children[3].children.filter(x => x instanceof Node).map(x => x.textContent))"), [
    ["View", "Map", "Download"], ["View", "Map", "Download"], ["View", "Download"],
  ]);
  ui.run("browserRoot.children[1].children[0].children[1].children[3].children.find(x => x.textContent === 'Map').listeners.click()");
  assert.deepEqual(ui.json("calls"), [["vector", "out/s.reaches.geojson"]]);
});

test("vector overlays use full GeoJSON, show safe reach labels, fit and cache", async () => {
  const ui = browser();
  ui.run(`
    let data = { type: "FeatureCollection", features: [
      { type: "Feature", geometry: { type: "LineString", coordinates: [[-79,36],[-78,36]] },
        properties: { ReachID: "<b>42</b>", HUC: "03020201", Streamflow: 7, processing_status: "skipped" } },
      { type: "Feature", geometry: null, properties: { ReachID: "43" } },
    ] };
    api = async (path) => { requests.push(path); return data; };
    let resultMap = createResultMap(view, "fim");
    let layer = { name: "s.reaches.geojson", key: "out/s.reaches.geojson" };
  `);
  await Promise.all([ui.run("resultMap.addVector(layer)"), ui.run("resultMap.addVector(layer)")]);
  assert.equal(ui.json("requests").length, 1);
  assert.match(ui.json("requests")[0], /^\/ui\/objects\/geojson\?/);
  assert.equal(ui.run("groups[0].element.children[1].children[0].children[1].textContent"), "s.reaches.geojson");
  assert.equal(ui.json("added").length, 1);
  assert.equal(ui.json("fits").length, 2);
  assert.equal(ui.json("tooltips")[0], "Reach <b>42</b> · HUC 03020201 · 7 m³/s · Skipped by streaming");
  assert.match(ui.run("status.textContent"), /1 feature\(s\) have no geometry/);
});

test("failed, empty and stale vector requests are visible or safely ignored", async () => {
  const ui = browser();
  ui.run(`
    let count = 0;
    api = async () => { if (!count++) throw new Error("storage unavailable");
      return {type: "FeatureCollection", features: [{type: "Feature", geometry: null}]}; };
    bounds = { isValid() { return false; } };
    let resultMap = createResultMap(view, "fim");
    let layer = { name: "reaches", key: "out/reaches.geojson" };
  `);
  await ui.run("resultMap.addVector(layer)");
  assert.match(ui.run("status.textContent"), /storage unavailable/);
  await ui.run("resultMap.addVector(layer)");
  assert.match(ui.run("status.textContent"), /no mapped geometry available/);
  assert.doesNotMatch(ui.run("status.textContent"), /storage unavailable/);
  assert.equal(ui.json("fits").length, 0);
  ui.run("let complete; api = () => new Promise(resolve => { complete = resolve; });");
  const pending = ui.run(`resultMap.addVector({ name: "new", key: "out/new.geojson" })`);
  ui.run(`state.workspaceVersion++; complete({type: "FeatureCollection", features: []});`);
  await pending;
  assert.deepEqual(ui.json("groups[0].element.children[1].children.map(label => label.children[1].textContent)"), ["reaches", "new"]);
  assert.equal(ui.run("added[1].features"), undefined);
});

test("result map registers saved reach layers without a Files-tab click", async () => {
  const ui = browser();
  ui.run(`
    let registered = [], rasters = [];
    createBrowser = () => ({});
    createResultMap = () => ({
      add(layer) { rasters.push(layer.key); },
      registerVector(layer) { registered.push([layer.name, layer.key]); },
      animate() {},
    });
    renderParameters = () => {};
    renderMarkdown = () => new Node();
    api = async () => ({ text: JSON.stringify([{ScenarioID:"s1", Reaches:[]}]) });
    view.querySelector = () => new Node();
    let result = { outputs: [{ScenarioID:"s1"}], files: [
      {key:"out/s1.cog"}, {key:"out/s1.reaches.geojson"}, {key:"out/scenarios.json"},
    ] };
  `);
  await ui.run('loadOutputDetails(view, "fim", "out", result)');
  ui.run('view.listeners.tabchange({detail: {name: "map"}})');
  assert.deepEqual(ui.json("rasters"), ["out/s1.cog"]);
  assert.deepEqual(ui.json("registered"), [["s1", "out/s1.reaches.geojson"]]);
});

test("streaming result discovers nested GeoJSON from result files", async () => {
  const ui = browser();
  ui.run(`
    let registered = [], rasters = [];
    createBrowser = () => ({});
    createResultMap = () => ({
      add(layer) { rasters.push(layer.key); },
      registerVector(layer) { registered.push([layer.name, layer.key]); },
      animate() {}, report() {},
    });
    renderParameters = () => {};
    renderMarkdown = () => new Node();
    api = async () => ({text: JSON.stringify([{ScenarioID:"s1", Reaches:[]}])});
    view.querySelector = () => new Node();
    let result = {scenario_manifests:[{ScenarioID:"s1",key:"out/s1/manifest.json"}],
      files:[{path:"s1/s1.reaches.geojson",key:"out/s1/s1.reaches.geojson"}]};
  `);
  await ui.run('loadOutputDetails(view, "fim", "out", result)');
  ui.run('view.listeners.tabchange({detail:{name:"map"}})');
  assert.deepEqual(ui.json("rasters"), ["out/s1/manifest.json"]);
  assert.deepEqual(ui.json("registered"), [["s1", "out/s1/s1.reaches.geojson"]]);
});

test("retained streaming result discovers nested layers and reports folder failures", async () => {
  const ui = browser();
  ui.run(`
    let registered = [], rasters = [], warnings = [];
    createBrowser = () => ({});
    createResultMap = () => ({
      add(layer) { rasters.push(layer.key); },
      registerVector(layer) { registered.push([layer.name, layer.key]); },
      animate() {}, report(message) { warnings.push(message); },
    });
    renderParameters = () => {};
    renderMarkdown = () => new Node();
    api = async (path) => {
      if (path.includes("README.md")) return {text:""};
      if (path.includes("scenarios.json")) return {text:JSON.stringify([
        {ScenarioID:"s1",Reaches:[]},{ScenarioID:"s2",Reaches:[]}
      ])};
      const q = new URLSearchParams(path.split("?")[1]);
      if (q.get("prefix") === "out") return {files:[],folders:[{name:"s1"},{name:"s2"}]};
      if (q.get("prefix") === "out/s2") throw new Error("storage denied");
      if (q.get("token") === "next") return {
        files:[{name:"s1.reaches.geojson",key:"out/s1/s1.reaches.geojson"}],folders:[],next_token:null,
      };
      return {files:[{name:"manifest.json",key:"out/s1/manifest.json"}],folders:[],next_token:"next"};
    };
    view.querySelector = () => new Node();
  `);
  await ui.run('loadOutputDetails(view, "fim", "out", null)');
  ui.run('view.listeners.tabchange({detail:{name:"map"}})');
  assert.deepEqual(ui.json("rasters"), ["out/s1/manifest.json", "out/s2/manifest.json"]);
  assert.deepEqual(ui.json("registered"), [["s1", "out/s1/s1.reaches.geojson"]]);
  assert.match(ui.json("warnings")[0], /s2: storage denied/);
});

test("map opened early registers retained reach files discovered on later listing pages", async () => {
  const ui = browser();
  ui.run(`
    let registered = [], rasters = [], finish;
    createBrowser = () => ({});
    createResultMap = () => ({
      add(layer) { rasters.push(layer.key); },
      registerVector(layer, visible) { registered.push([layer.name, layer.key, visible]); },
      animate() {},
    });
    renderParameters = () => {};
    renderMarkdown = () => new Node();
    api = (path) => {
      if (path.includes("README.md")) return Promise.resolve({text:""});
      if (path.includes("scenarios.json")) return new Promise(resolve => { finish = resolve; });
      if (path.includes("token=next")) return Promise.resolve({
        files:[{name:"s1.reaches.geojson",key:"out/s1.reaches.geojson"}], folders:[], next_token:null,
      });
      return Promise.resolve({files:[{name:"s1.cog",key:"out/s1.cog"}], folders:[], next_token:"next"});
    };
    view.querySelector = () => new Node();
  `);
  const pending = ui.run('loadOutputDetails(view, "fim", "out", null)');
  ui.run('view.listeners.tabchange({detail: {name: "map"}}); finish({text: JSON.stringify([{ScenarioID:"s1",Reaches:[]}])})');
  await pending;
  assert.deepEqual(ui.json("rasters"), ["out/s1.cog"]);
  assert.deepEqual(ui.json("registered"), [["s1", "out/s1.reaches.geojson", null]]);
});

test("manual GeoJSON Map action uses scenario names only for reach outputs", async () => {
  const ui = browser();
  ui.run(`
    let chosen = [], actions;
    createBrowser = (_container, options) => { actions = options; return {}; };
    createResultMap = () => ({
      add() {}, registerVector() {}, animate() {}, report() {},
      addVector(layer) { chosen.push([layer.name, layer.key]); },
    });
    activateTab = () => {};
    renderParameters = () => {};
    renderMarkdown = () => new Node();
    api = async () => ({text: JSON.stringify([{ScenarioID:"s1", Reaches:[]}])});
    view.querySelector = () => new Node();
  `);
  await ui.run('loadOutputDetails(view, "fim", "out", {outputs:[],files:[]})');
  ui.run(`actions.onGeoJSON("out/s1/s1.reaches.geojson"); actions.onGeoJSON("out/custom/other.geojson");`);
  assert.deepEqual(ui.json("chosen"), [
    ["s1", "out/s1/s1.reaches.geojson"], ["custom/other.geojson", "out/custom/other.geojson"],
  ]);
});

test("registered reach overlays load lazily and do not change raster framing", async () => {
  const ui = browser();
  ui.run(`
    api = async path => { requests.push(path); return {type:"FeatureCollection", features:[]}; };
    let resultMap = createResultMap(view, "fim");
    let first = resultMap.registerVector({name:"first",key:"first.reaches.geojson"});
    let second = resultMap.registerVector({name:"second",key:"second.reaches.geojson"});
  `);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ui.json("groups[0].element.children[1].children.map(label => label.children[1].textContent)"), ["first", "second"]);
  assert.equal(ui.json("requests").length, 0);
  assert.equal(ui.run("first.checkbox.checked || second.checkbox.checked"), false);
  assert.equal(ui.json("fits").length, 0);
  ui.run("second.checkbox.checked = true; second.checkbox.listeners.change()");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.json("requests").length, 1);
  assert.equal(ui.run("second.checkbox.checked"), true);
  ui.run("second.checkbox.checked = false; second.checkbox.listeners.change()");
  assert.equal(ui.run("map.hasLayer(second.overlay)"), false);
  ui.run('resultMap.registerVector({name:"first",key:"first.reaches.geojson"})');
  assert.equal(ui.run("groups[0].element.children[1].children.length"), 2);
});

test("flood and vector overlays are listed in separately titled controls", async () => {
  const ui = browser();
  ui.run(`
    api = async path => path.startsWith("/outputs/info") ?
      {bounds: [-79, 35, -78, 36], minzoom: 2, maxzoom: 12} :
      {type: "FeatureCollection", features: []};
    let resultMap = createResultMap(view, "fim");
    resultMap.registerVector({name: "reach.geojson", key:"reach.geojson"});
  `);
  await ui.run('resultMap.add({name: "scenario", key:"scenario.cog"}, false)');
  await ui.run('resultMap.add({name: "differences", key:"diff.tif"}, false)');
  assert.deepEqual(ui.json("groups.map(g => ({title:g.element.children[0].textContent,names:g.reach ? g.element.children[1].children.map(label => label.children[1].textContent) : g.names}))"), [
    {title: "Reach layers", names: ["reach.geojson"]},
    {title: "Flood extents", names: ["scenario", "differences"]},
  ]);
});

test("reach highlights follow visible flood overlays and late registration", async () => {
  const ui = browser();
  ui.run(`
    api = async path => {
      requests.push(path);
      return {bounds: [-79, 35, -78, 36], minzoom: 2, maxzoom: 12};
    };
    let resultMap = createResultMap(view, "fim");
    let a = {name:"a", scenario:"a", key:"a.cog"};
    let b = {name:"b", scenario:"b", key:"b.cog"};
    let aReach = resultMap.registerVector({name:"a", scenario:"a", key:"a.reaches.geojson"});
  `);
  await ui.run("resultMap.add(a, false)");
  ui.run('let bReach = resultMap.registerVector({name:"b",scenario:"b",key:"b.reaches.geojson"})');
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), true);
  assert.equal(ui.run("aReach.badge.hidden"), false);
  assert.equal(ui.run("aReach.checkbox.checked"), false);
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), false);
  await ui.run("resultMap.add(b, false)");
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), true);
  ui.run("map.removeLayer(groups.find(g => !g.reach).layers[0])");
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), false);
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), true);
  ui.run("map.addLayer(groups.find(g => !g.reach).layers[0])");
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), true);
  assert.equal(ui.run("requests.length"), 2);
  await ui.run('resultMap.add({name:"difference", key:"diff.tif"}, false)');
  ui.run("map.removeLayer(groups.find(g => !g.reach).layers[0])");
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), false);
});

test("early manually opened raster gains scenario matching when discovered", async () => {
  const ui = browser();
  ui.run(`
    api = async () => ({bounds:[-79,35,-78,36], minzoom:2, maxzoom:12});
    let resultMap = createResultMap(view, "fim");
    let reach = resultMap.registerVector({name:"a",scenario:"a",key:"a.reaches.geojson"});
  `);
  await ui.run('resultMap.add({name:"a.cog",key:"a.cog"}, false)');
  assert.equal(ui.run('reach.label.classList.contains("matching-flood")'), false);
  await ui.run('resultMap.add({name:"a",scenario:"a",key:"a.cog"}, false)');
  assert.equal(ui.run('reach.label.classList.contains("matching-flood")'), true);
});

test("animation highlights only visible frames and handles toggled preloads", async () => {
  const ui = browser();
  ui.run(`
    api = async () => ({bounds:[-79,35,-78,36], minzoom:2, maxzoom:12});
    let resultMap = createResultMap(view, "fim");
    let a = {name:"a",scenario:"a",key:"a.cog"}, b = {name:"b",scenario:"b",key:"b.cog"};
    let aReach = resultMap.registerVector({name:"a",scenario:"a",key:"a.reaches.geojson"});
    let bReach = resultMap.registerVector({name:"b",scenario:"b",key:"b.reaches.geojson"});
  `);
  await ui.run("resultMap.add(a, false)");
  await ui.run("resultMap.add(b, false)");
  ui.run("resultMap.animate([a,b]); status.nextSibling.children[3].listeners.click()");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), false);
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), true);
  assert.deepEqual(ui.json("groups.find(g => !g.reach).layers.map(layer => layer.opacity)"), [0, 0.8]);
  ui.run("status.nextSibling.children[1].listeners.click()");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(ui.run('aReach.label.classList.contains("matching-flood")'), true);
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), false);
  ui.run("let preload = groups.find(g => !g.reach).layers[1]; map.removeLayer(preload); map.addLayer(preload)");
  assert.equal(ui.run("preload.opacity"), 0.8);
  assert.equal(ui.run('bReach.label.classList.contains("matching-flood")'), true);
});

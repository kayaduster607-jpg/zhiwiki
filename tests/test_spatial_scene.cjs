/* Exercise real scene lifecycle with a deterministic RAF queue and no browser packages. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const spatialModel = require("../zhiwiki/web/spatial-model.js");

class EventTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  fire(type, values = {}) {
    if (type === "click" && this.disabled) return;
    const event = { target: this, currentTarget: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...values };
    if (this["on" + type]) this["on" + type](event);
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
}

class Element extends EventTarget {
  constructor(tag = "div") {
    super();
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.attributes = new Map();
    this._text = "";
    this.width = 800;
    this.height = 500;
    this.disabled = false;
    this.classes = new Set();
    this.classList = {
      contains: (name) => this.classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classes.has(name) : Boolean(force);
        if (enabled) this.classes.add(name); else this.classes.delete(name);
        return enabled;
      },
    };
  }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(""); }
  append(...items) { items.forEach((item) => { item.parentElement = this; this.children.push(item); }); }
  replaceChildren(...items) {
    this.children.forEach((item) => { item.parentElement = null; });
    this.children = [];
    this.append(...items);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getBoundingClientRect() { return { width: this.width, height: this.height }; }
  setPointerCapture(id) { this.pointerId = id; }
}

function fixture({ createFails = false, drawFails = false, motionEnabled = true, missingModel = false } = {}) {
  const elements = new Map();
  const document = new EventTarget();
  document.hidden = false;
  document.body = new Element("body");
  document.activeElement = document.body;
  document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  document.createElement = (tag) => new Element(tag);
  document.createElementNS = (namespaceURI, tag) => Object.assign(new Element(tag), { namespaceURI });
  const get = document.getElementById;
  const queue = new Map();
  const instances = [];
  const stats = { creates: 0, draws: 0, disposals: 0, cancelled: 0, maxPending: 0 };
  const opened = [];
  let nextFrame = 0, time = 0, intersection, resizing;
  const state = { createFails, drawFails, motionEnabled };
  const options = {
    safe: (action) => (event) => { event.preventDefault(); return action(event); },
    openDoc: (id) => opened.push({ kind: "document", id }),
    openWiki: (id) => opened.push({ kind: "wiki", id }),
  };
  const window = {
    ZhiSpatialModel: spatialModel,
    ZhiEffects: { isEnabled: () => state.motionEnabled },
    ZhiOrbRenderer: {
      create() {
        stats.creates += 1;
        if (state.createFails) throw new Error("WebGL unavailable");
        const renderer = {
          drawCalls: [], disposed: false,
          draw(settings) {
            if (this.disposed) throw new Error("drawing a disposed renderer");
            if (state.drawFails) throw new Error("WebGL draw failed");
            this.drawCalls.push(settings); stats.draws += 1;
          },
          dispose() { if (!this.disposed) stats.disposals += 1; this.disposed = true; },
        };
        instances.push(renderer);
        return renderer;
      },
    },
  };
  if (missingModel) delete window.ZhiSpatialModel;
  const sandbox = {
    document, window, console,
    requestAnimationFrame(callback) {
      queue.set(++nextFrame, callback);
      stats.maxPending = Math.max(stats.maxPending, queue.size);
      return nextFrame;
    },
    cancelAnimationFrame(id) { if (queue.delete(id)) stats.cancelled += 1; },
    ResizeObserver: class {
      constructor(callback) { resizing = callback; }
      observe() {}
    },
    IntersectionObserver: class {
      constructor(callback) { intersection = callback; }
      observe() {}
    },
  };
  vm.createContext(sandbox);
  const filename = path.join(__dirname, "..", "zhiwiki", "web", "spatial-scene.js");
  vm.runInContext(fs.readFileSync(filename, "utf8"), sandbox, { filename });
  const scene = window.ZhiSpatial;
  scene.init(options);
  return {
    scene, get, stats, state, opened, instances, document,
    pending: () => queue.size,
    tick() {
      time += 16;
      const callbacks = [...queue.values()]; queue.clear();
      callbacks.forEach((callback) => callback(time));
    },
    motion(enabled) { state.motionEnabled = enabled; document.fire("zhi:motion"); },
    hidden(hidden) { document.hidden = hidden; document.fire("visibilitychange"); },
    intersect(isIntersecting) { intersection([{ isIntersecting }]); },
    resize(width, height) {
      Object.assign(get("spatial-surface"), { width, height }); resizing();
    },
    focus(element) { document.activeElement = element; element.fire("focus"); },
    blur(element) { document.activeElement = document.body; element.fire("blur"); },
    initAgain: () => scene.init(options),
  };
}

function records(suffix = "1") {
  return {
    docs: [{ id: `doc${suffix}`, title: `原文 ${suffix}`, active: true, version: Number(suffix), chunk_ids: [`chunk${suffix}`] }],
    pages: [{ id: `wiki${suffix}`, title: `知识 ${suffix}`, status: "published", stale: false, version: Number(suffix), source_ids: [`chunk${suffix}`] }],
  };
}

function update(ui, suffix = "1") {
  const data = records(suffix);
  ui.scene.update(data.docs, data.pages, "平面图 · 真实原文引用");
}

function assertRunning(ui) {
  assert.equal(ui.pending(), 1, "one animation frame should be pending");
  ui.tick();
  assert.equal(ui.pending(), 1, "a frame schedules exactly one successor");
  assert.equal(ui.stats.maxPending, 1, "repeated lifecycle events cannot create parallel loops");
}

test("pause and resume keep at most one RAF loop, including repeated initialization", () => {
  const ui = fixture(); update(ui);
  assertRunning(ui);
  ui.initAgain();
  assert.equal(ui.stats.creates, 1);
  ui.get("scene-pause").fire("click");
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("scene-pause").attributes.get("aria-pressed"), "true");
  const stopped = ui.stats.draws; ui.tick();
  assert.equal(ui.stats.draws, stopped);
  ui.get("scene-pause").fire("click");
  assertRunning(ui);
});

test("global motion changes stop animation and preserve an explicit local pause", () => {
  const ui = fixture(); update(ui);
  ui.motion(false);
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("scene-pause").disabled, true);
  ui.get("scene-pause").fire("click");
  assert.equal(ui.get("scene-pause").attributes.get("aria-pressed"), "false");
  ui.motion(true); ui.motion(true);
  assertRunning(ui);
  ui.get("scene-pause").fire("click");
  ui.motion(false); ui.motion(true);
  assert.equal(ui.pending(), 0, "global enable must not erase a local pause");
});

test("hidden application views cancel the loop without drawing, and restoration does not duplicate it", () => {
  const ui = fixture(); update(ui);
  const before = ui.stats.draws;
  ui.scene.setVisible(false); ui.scene.setVisible(false);
  ui.tick();
  assert.equal(ui.pending(), 0);
  assert.equal(ui.stats.draws, before);
  ui.scene.setVisible(true); ui.scene.setVisible(true);
  assertRunning(ui);
});

test("document visibility and offscreen observation independently gate rendering", () => {
  const ui = fixture(); update(ui);
  ui.hidden(true);
  assert.equal(ui.pending(), 0);
  ui.intersect(false); ui.hidden(false);
  assert.equal(ui.pending(), 0, "returning to a tab cannot animate an offscreen scene");
  const stopped = ui.stats.draws;
  ui.tick();
  assert.equal(ui.stats.draws, stopped);
  ui.intersect(true); ui.intersect(true);
  assertRunning(ui);
  ui.intersect(false); ui.hidden(true); ui.intersect(true);
  assert.equal(ui.pending(), 0, "intersection cannot override a hidden document");
  ui.hidden(false);
  assertRunning(ui);
});

test("zero-size view cancels drawing until a positive resize arrives", () => {
  const ui = fixture(); update(ui);
  ui.resize(0, 0);
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("spatial-links").attributes.get("viewBox"), "0 0 1 1");
  ui.resize(800, 500); ui.resize(800, 500);
  assertRunning(ui);
});

test("planar mode preserves the real-source caption and pauses WebGL until toggled back", () => {
  const ui = fixture(); update(ui);
  ui.get("scene-mode").fire("click");
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
  assert.equal(ui.get("knowledge-canvas").tabIndex, -1);
  assert.equal(ui.get("graph-caption").textContent, "平面图 · 真实原文引用");
  assert.equal(ui.pending(), 0);
  const before = ui.stats.draws; ui.tick();
  assert.equal(ui.stats.draws, before);
  ui.get("scene-mode").fire("click");
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "webgl");
  assert.equal(ui.get("knowledge-canvas").tabIndex, 0);
  assertRunning(ui);
});

test("WebGL creation failure keeps a usable planar fallback instead of scheduling failed rendering", () => {
  const ui = fixture({ createFails: true }); update(ui);
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
  assert.equal(ui.get("scene-mode").disabled, true);
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("graph-caption").textContent, "平面图 · 真实原文引用");
  assert.match(ui.get("scene-status").textContent, /平面来源图/);
  ui.get("scene-mode").fire("click");
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
});

test("a missing optional spatial model keeps later data refreshes on the planar fallback", () => {
  const ui = fixture({ missingModel: true });
  assert.doesNotThrow(() => update(ui));
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
  assert.equal(ui.get("graph-caption").textContent, "平面图 · 真实原文引用");
  assert.equal(ui.pending(), 0);
  assert.equal(ui.stats.creates, 0);
});

test("WebGL loss disposes once and restoration creates one fresh renderer and loop", () => {
  const ui = fixture(); update(ui);
  const lost = ui.get("knowledge-canvas").fire("webglcontextlost");
  assert.equal(lost.defaultPrevented, true);
  assert.equal(ui.stats.disposals, 1);
  assert.equal(ui.instances[0].disposed, true);
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
  ui.get("knowledge-canvas").fire("webglcontextrestored");
  assert.equal(ui.stats.creates, 2);
  assert.equal(ui.get("scene-mode").disabled, false);
  assertRunning(ui);
});

test("runtime draw failure cancels the successor frame and falls back without repeated failures", () => {
  const ui = fixture(); update(ui);
  ui.state.drawFails = true;
  ui.tick();
  assert.equal(ui.stats.disposals, 1);
  assert.equal(ui.pending(), 0);
  assert.equal(ui.get("knowledge-scene").dataset.renderer, "planar");
  ui.tick();
  assert.equal(ui.stats.disposals, 1);
});

test("data replacement removes old nodes and edges and click handlers open the actual current IDs", () => {
  const ui = fixture(); update(ui);
  const oldNodes = [...ui.get("spatial-nodes").children];
  const oldEdge = ui.get("spatial-links").children[0];
  assert.equal(ui.get("spatial-links").children.length, 1);
  const next = records("2");
  next.pages[0].source_ids = ["chunk1"];
  ui.scene.update(next.docs, next.pages, "更新来源后");
  const current = ui.get("spatial-nodes").children;
  assert.equal(current.length, 2);
  assert.ok(oldNodes.every((item) => !current.includes(item) && item.parentElement === null));
  assert.equal(oldEdge.parentElement, null);
  assert.equal(ui.get("spatial-links").children.length, 0, "old chunk reference cannot survive a new source revision");
  current.forEach((button) => button.fire("click"));
  assert.deepEqual(ui.opened, [{ kind: "document", id: "doc2" }, { kind: "wiki", id: "wiki2" }]);
  ui.scene.update([], [], "空库");
  assert.equal(ui.get("spatial-nodes").children.length, 0);
  assert.equal(ui.get("spatial-links").children.length, 0);
  assert.match(ui.get("graph-caption").textContent, /加入第一份资料/);
  assertRunning(ui);
});

test("hover and keyboard focus pause moving targets; leaving one does not override the other", () => {
  const ui = fixture(); update(ui);
  const button = ui.get("spatial-nodes").children[0];
  button.fire("pointerenter");
  assert.equal(ui.pending(), 0);
  ui.focus(button); button.fire("pointerleave");
  assert.equal(ui.pending(), 0);
  ui.blur(button);
  assertRunning(ui);
});

test("refreshing source data preserves a keyboard-focused canvas pause", () => {
  const ui = fixture(); update(ui);
  ui.focus(ui.get("knowledge-canvas"));
  assert.equal(ui.pending(), 0);
  update(ui, "2");
  assert.equal(ui.document.activeElement, ui.get("knowledge-canvas"));
  assert.equal(ui.pending(), 0, "data refresh must not resume rotation while the canvas still has focus");
  ui.blur(ui.get("knowledge-canvas"));
  assertRunning(ui);
});

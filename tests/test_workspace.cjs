/* Exercise browser workspace event races with Node's standard-library VM. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.value = "";
    this._text = "";
    this.attributes = new Map();
    this.style = { setProperty() {} };
    this.classList = { toggle() {} };
  }

  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ""; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  querySelectorAll() { return []; }
  focus() {}
  async fire(type) {
    const handler = this["on" + type];
    if (handler) await handler({ currentTarget: this, preventDefault() {} });
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function preview(text) {
  const length = Array.from(text).length;
  return {
    chunk_count: 1, duplicate_chars: 0, source_length: length,
    chunks: [{ text, start_offset: 0, end_offset: length }],
  };
}

function renderedChunks(element) {
  // An explanatory empty state is allowed; source fragment disclosures are not.
  return element.children.filter((child) => child.tagName === "DETAILS");
}

async function workspace(api) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement(tag) { return new Element(tag); },
    createElementNS(namespace, tag) { return new Element(tag); },
    querySelectorAll() { return []; },
  };
  const element = (id) => document.getElementById(id);
  element("chunk-text").value = "Old source";
  element("chunk-size").value = "900";
  element("chunk-overlap").value = "120";
  element("chunk-strategy").value = "fixed";
  const sandbox = { document, window: {}, console };
  vm.createContext(sandbox);
  const filename = path.join(__dirname, "..", "zhiwiki", "web", "workspace.js");
  vm.runInContext(fs.readFileSync(filename, "utf8"), sandbox, { filename });
  await sandbox.window.ZhiWorkspace.init({
    api: (url, body) => url === "/api/examples/catalog" ? Promise.resolve({ documents: [] }) : api(url, body),
    node(tag, text, className) {
      const item = new Element(tag);
      if (text !== undefined) item.textContent = text;
      item.className = className;
      return item;
    },
    safe: (action) => async (event) => { event.preventDefault(); await action(event); },
    notify() {}, getDocuments: () => [], getPages: () => [],
  });
  return { element, submit: () => element("chunk-form").fire("submit") };
}

test("emoji chunk length uses the same code points as the API offsets", async () => {
  const text = "😀".repeat(200);
  const ui = await workspace(async () => preview(text));
  await ui.submit();
  const summary = ui.element("chunk-list").children[0].children[0].textContent;
  assert.equal(summary, "#1 · [0, 200) · 200 字符");
});

for (const [id, event, value] of [
  ["chunk-size", "input", "450"],
  ["chunk-overlap", "input", "50"],
  ["chunk-strategy", "change", "structure"],
]) {
  test(`changing ${id} invalidates an in-flight preview of previous parameters`, async () => {
    const pending = deferred();
    const ui = await workspace(() => pending.promise);
    const request = ui.submit();
    ui.element(id).value = value;
    await ui.element(id).fire(event);
    pending.resolve(preview("Old source"));
    await request;
    assert.equal(renderedChunks(ui.element("chunk-list")).length, 0,
      "An old result must not appear underneath newly selected parameters.");
  });
}

test("selecting another document invalidates an older preview before its document fetch returns", async () => {
  const oldPreview = deferred();
  const newDocument = deferred();
  const ui = await workspace((url, body) => {
    if (url === "/api/documents/new") return newDocument.promise;
    if (body.text === "Old source") return oldPreview.promise;
    return Promise.resolve(preview(body.text));
  });
  const first = ui.submit();
  ui.element("chunk-document").value = "new";
  const second = ui.element("chunk-document").fire("change");
  oldPreview.resolve(preview("Old source"));
  await first;
  const interimCount = renderedChunks(ui.element("chunk-list")).length;
  newDocument.resolve({ text: "New source" });
  await second;
  assert.equal(interimCount, 0, "A selected source and an old source's late preview must not be mixed.");
  assert.equal(ui.element("chunk-text").value, "New source");
});

test("typing after choosing a document preserves the user's newer text", async () => {
  const pending = deferred();
  let previewRequests = 0;
  const ui = await workspace((url) => {
    if (url === "/api/documents/slow") return pending.promise;
    previewRequests += 1;
    return Promise.resolve(preview("Unexpected"));
  });
  ui.element("chunk-document").value = "slow";
  const request = ui.element("chunk-document").fire("change");
  ui.element("chunk-text").value = "My new text 😀";
  await ui.element("chunk-text").fire("input");
  pending.resolve({ text: "Slow old document" });
  await request;
  assert.equal(ui.element("chunk-text").value, "My new text 😀");
  assert.equal(previewRequests, 0);
});

test("loading long emoji text truncates by code points without splitting surrogate pairs", async () => {
  let received;
  const ui = await workspace(async (url, body) => {
    if (url === "/api/documents/emoji") return { text: "😀".repeat(20001) };
    received = body.text;
    return preview(body.text);
  });
  ui.element("chunk-document").value = "emoji";
  await ui.element("chunk-document").fire("change");
  assert.equal(Array.from(received).length, 20000);
  assert.equal(received, "😀".repeat(20000));
});

test("an over-limit Unicode preview is rejected before calling the API", async () => {
  let requests = 0;
  const ui = await workspace(async () => { requests += 1; return preview("Unexpected"); });
  ui.element("chunk-text").value = "😀".repeat(20001);
  await assert.rejects(ui.submit(), /20,000/);
  assert.equal(requests, 0);
});

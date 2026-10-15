/* Run with Node's built-in test runner; no DOM package or browser download. */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.open = false;
    this._text = "";
  }

  set textContent(value) {
    this._text = value;
    this.children = [];
  }

  get textContent() { return this._text; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ""; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function response(value) {
  return { ok: true, json: async () => value };
}

function page(id) {
  return {
    title: `Wiki ${id}`, version: 1, status: "draft", stale: false,
    mode: "offline", claims: [{ text: `Claim ${id}`, evidence_ids: [`source${id}`] }],
    source_ids: [`source${id}`],
  };
}

function evidence(id) {
  return { title: `Source ${id}`, version: 1, locator: id, text: `Text ${id}` };
}

function loadApp(fetch) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element("div"));
      return elements.get(id);
    },
    createElement(tag) { return new Element(tag); },
    querySelectorAll() { return []; },
  };
  const sandbox = { document, fetch, console, AbortController, TextDecoder };
  vm.createContext(sandbox);
  const filename = path.join(__dirname, "..", "zhiwiki", "web", "app.js");
  // Exercise the actual functions while preventing unrelated startup requests.
  const source = fs.readFileSync(filename, "utf8").replace(/start\(\);\s*$/, "");
  vm.runInContext(source, sandbox, { filename });
  return { sandbox, element: (id) => document.getElementById(id) };
}

function assertOnlyPageB(element) {
  assert.equal(element("detail-title").textContent, "Wiki B");
  const summaries = element("detail-body").children.map((block) => block.children[0].textContent);
  assert.deepEqual(summaries, ["Source B · v1 · B"]);
  const actions = element("detail-actions").children;
  assert.deepEqual(actions.filter((item) => item.tagName === "A").map((item) => item.href), ["/api/wiki/B/export"]);
  assert.equal(actions.filter((item) => item.tagName === "BUTTON").length, 1);
}

test("late source A cannot add citations or a publish action to Wiki B", async () => {
  const sourceA = deferred();
  const published = [];
  const { sandbox, element } = loadApp(async (url) => {
    if (url === "/api/wiki/A") return response(page("A"));
    if (url === "/api/wiki/B") return response(page("B"));
    if (url === "/api/evidence/sourceA") return sourceA.promise;
    if (url === "/api/evidence/sourceB") return response(evidence("B"));
    if (url === "/api/wiki/B/publish") { published.push(url); return response({}); }
    if (url === "/api/wiki") return response({ pages: [] });
    throw new Error(`Unexpected request: ${url}`);
  });

  const pendingA = sandbox.openWiki("A");
  await new Promise((resolve) => setImmediate(resolve));
  sandbox.closeDetail();
  await sandbox.openWiki("B");
  sourceA.resolve(response(evidence("A")));
  await pendingA;
  assertOnlyPageB(element);

  // The only visible action must publish the page whose sources were reviewed.
  const publish = element("detail-actions").children.find((item) => item.tagName === "BUTTON");
  await publish.onclick({ preventDefault() {}, currentTarget: publish });
  assert.deepEqual(published, ["/api/wiki/B/publish"]);
});

test("late page A response cannot replace already opened Wiki B", async () => {
  const pageA = deferred();
  const { sandbox, element } = loadApp(async (url) => {
    if (url === "/api/wiki/A") return pageA.promise;
    if (url === "/api/wiki/B") return response(page("B"));
    if (url === "/api/evidence/sourceB") return response(evidence("B"));
    throw new Error(`Unexpected request: ${url}`);
  });

  const pendingA = sandbox.openWiki("A");
  await sandbox.openWiki("B");
  pageA.resolve(response(page("A")));
  await pendingA;
  assertOnlyPageB(element);
});

test("closing the detail modal invalidates pending source responses", async () => {
  const sourceA = deferred();
  const { sandbox, element } = loadApp(async (url) => {
    if (url === "/api/wiki/A") return response(page("A"));
    if (url === "/api/evidence/sourceA") return sourceA.promise;
    throw new Error(`Unexpected request: ${url}`);
  });

  const pendingA = sandbox.openWiki("A");
  await new Promise((resolve) => setImmediate(resolve));
  sandbox.closeDetail();
  sourceA.resolve(response(evidence("A")));
  await pendingA;

  assert.equal(element("detail").open, false);
  assert.deepEqual(element("detail-body").children, []);
  assert.deepEqual(element("detail-actions").children, []);
});

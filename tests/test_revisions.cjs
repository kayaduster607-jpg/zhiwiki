/* Exercise revision editor races using the actual module and Node's VM only. */
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
    this.parentElement = null;
    this.value = "";
    this.disabled = false;
    this.open = false;
    this._text = "";
    this.attributes = new Map();
    this.listeners = new Map();
  }

  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(""); }
  append(...items) {
    for (const item of items) { item.parentElement = this; this.children.push(item); }
  }
  replaceChildren(...items) { this.children = []; this._text = ""; this.append(...items); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(type, action) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(action);
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  querySelectorAll(selector) {
    const matches = (item) => selector.startsWith(".") ?
      String(item.className || "").split(/\s+/).includes(selector.slice(1)) :
      item.tagName.toLowerCase() === selector.toLowerCase();
    return this.children.flatMap((child) => [
      ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  isDisabled() {
    if (this.disabled) return true;
    for (let ancestor = this.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.tagName === "FIELDSET" && ancestor.disabled) return true;
    }
    return false;
  }
  async fire(type) {
    // Model native disabled controls. A form-level submit event is separate;
    // duplicate queued/programmatic submissions still need application guards.
    if (["click", "input", "change"].includes(type) && this.isDisabled()) return false;
    let prevented = false;
    const event = { currentTarget: this, target: this, preventDefault() { prevented = true; } };
    if (this["on" + type]) await this["on" + type](event);
    for (const action of this.listeners.get(type) || []) await action(event);
    if (type === "cancel" && !prevented) this.close();
    return true;
  }
  async userInput(value) {
    if (this.isDisabled()) return false;
    this.value = value;
    await this.fire("input");
    return true;
  }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

function source(id) {
  return { id, title: "Source " + id, version: 1, active: true,
    text: "Verified text " + id, locator: "chars:1-20" };
}

function page(id) {
  return { id, title: "Page " + id, version: 1, links: [],
    claims: [{ text: "Claim " + id, evidence_ids: ["chunk_" + id] }],
    source_ids: ["chunk_" + id] };
}

function editor(api) {
  const elements = new Map();
  const tags = { "revision-dialog": "dialog", "revision-form": "form", "revision-fields": "fieldset",
    "revision-title": "input", "revision-note": "input", "revision-document": "select",
    "save-revision": "button", "add-claim": "button", "close-revision": "button" };
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(tags[id] || "div"));
      return elements.get(id);
    },
  };
  const get = (id) => document.getElementById(id);
  get("revision-dialog").append(get("revision-form"));
  get("revision-form").append(get("revision-fields"));
  for (const id of ["revision-title", "revision-note", "revision-document", "revision-claims",
    "revision-evidence", "save-revision", "add-claim"]) get("revision-fields").append(get(id));
  const sandbox = { window: {}, document, console };
  vm.createContext(sandbox);
  const filename = path.join(__dirname, "..", "zhiwiki", "web", "revisions.js");
  vm.runInContext(fs.readFileSync(filename, "utf8"), sandbox, { filename });
  const requests = [], opened = [], notices = [], refreshed = [];
  sandbox.window.ZhiRevisions.init({
    api(url, body) {
      // Real app.api serializes the body at dispatch; keep a comparable snapshot.
      requests.push({ url, body: body === undefined ? undefined : plain(body) });
      return api(url, body);
    },
    node(tag, text, className) {
      const element = new Element(tag);
      if (text !== undefined) element.textContent = text;
      element.className = className;
      return element;
    },
    safe: (action) => async (event) => { event.preventDefault(); await action(event); },
    closeDetail() {}, getDocuments: () => [{ id: "doc_new", title: "New source", version: 2 }],
    async refreshWiki() { refreshed.push(true); },
    async openWiki(id) { opened.push(id); },
    notify(message) { notices.push(message); },
  });
  return { get, requests, opened, notices, refreshed,
    open: (value) => sandbox.window.ZhiRevisions.open(value),
    close: () => sandbox.window.ZhiRevisions.close(),
    submit: () => get("revision-form").fire("submit"),
    textarea: () => get("revision-claims").querySelectorAll("textarea")[0],
  };
}

test("closing while initial evidence loads keeps the closed editor free of late content", async () => {
  const pending = deferred();
  const ui = editor(() => pending.promise);
  const opening = ui.open(page("A"));
  const before = ui.get("revision-evidence").textContent;
  ui.close();
  pending.resolve(source("chunk_A"));
  await opening;
  assert.equal(ui.get("revision-dialog").open, false);
  assert.equal(ui.get("revision-evidence").textContent, before);
  assert.equal(ui.get("save-revision").disabled, true);
});

test("late evidence from closed page A cannot appear in newly opened page B", async () => {
  const pending = deferred();
  const ui = editor((url) => url.endsWith("chunk_A") ? pending.promise : Promise.resolve(source("chunk_B")));
  const openingA = ui.open(page("A"));
  ui.close();
  await ui.open(page("B"));
  pending.resolve(source("chunk_A"));
  await openingA;
  assert.equal(ui.get("revision-title").value, "Page B");
  assert.equal(ui.textarea().value, "Claim B");
  assert.match(ui.get("revision-evidence").textContent, /Verified text chunk_B/);
  assert.doesNotMatch(ui.get("revision-evidence").textContent, /chunk_A/);
});

test("Escape cancellation invalidates an in-flight additional source request", async () => {
  const pending = deferred();
  const ui = editor((url) => url.startsWith("/api/documents/") ? pending.promise : Promise.resolve(source("chunk_A")));
  await ui.open(page("A"));
  ui.get("revision-document").value = "doc_new";
  const loading = ui.get("revision-document").fire("change");
  const before = ui.get("revision-evidence").textContent;
  await ui.get("revision-dialog").fire("cancel");
  pending.resolve({ chunks: [source("chunk_NEW")] });
  await loading;
  assert.equal(ui.get("revision-dialog").open, false);
  assert.equal(ui.get("revision-evidence").textContent, before);
});

test("pending save locks user edits and submits the reviewed content", async () => {
  const pending = deferred();
  const ui = editor((url) => url.endsWith("/revisions") ? pending.promise : Promise.resolve(source("chunk_A")));
  await ui.open(page("A"));
  await ui.textarea().userInput("Reviewed manual claim");
  await ui.get("revision-note").userInput("Verified before saving");
  const saving = ui.submit();
  assert.equal(await ui.textarea().userInput("Unreviewed mutation"), false);
  assert.equal(await ui.get("revision-title").userInput("Different title"), false);
  assert.equal(await ui.get("revision-note").userInput("Different note"), false);
  assert.equal(await ui.get("add-claim").fire("click"), false);
  assert.equal(ui.get("revision-claims").querySelectorAll("textarea").length, 1);
  pending.resolve({ id: "A_v2", version: 2, status: "draft" });
  await saving;
  const submitted = ui.requests.find((request) => request.url.endsWith("/revisions")).body;
  assert.equal(submitted.claims[0].text, "Reviewed manual claim");
  assert.equal(submitted.title, "Page A");
  assert.equal(submitted.revision_note, "Verified before saving");
});

test("two submit events while a save is pending create only one revision request", async () => {
  const pending = deferred();
  const ui = editor((url) => url.endsWith("/revisions") ? pending.promise : Promise.resolve(source("chunk_A")));
  await ui.open(page("A"));
  const first = ui.submit();
  const second = ui.submit();
  const countWhilePending = ui.requests.filter((request) => request.url.endsWith("/revisions")).length;
  pending.resolve({ id: "A_v2", version: 2, status: "draft" });
  await Promise.all([first, second]);
  assert.equal(countWhilePending, 1, "Disable state alone must not allow duplicate form-level submissions.");
});

test("409 keeps edited content and permits an explicit retry without publishing", async () => {
  let attempts = 0;
  const ui = editor(async (url) => {
    if (!url.endsWith("/revisions")) return source("chunk_A");
    attempts += 1;
    if (attempts === 1) throw new Error("409: 页面已有更新，请核对后重试");
    return { id: "A_v2", version: 2, status: "draft" };
  });
  await ui.open(page("A"));
  await ui.get("revision-title").userInput("My manual revision");
  await ui.textarea().userInput("Keep this edited conclusion");
  await ui.get("revision-note").userInput("Do not discard on conflict");
  await ui.submit();
  assert.equal(ui.get("revision-dialog").open, true);
  assert.equal(ui.get("revision-title").value, "My manual revision");
  assert.equal(ui.textarea().value, "Keep this edited conclusion");
  assert.equal(ui.get("revision-note").value, "Do not discard on conflict");
  assert.match(ui.get("revision-state").textContent, /409/);
  assert.equal(ui.get("save-revision").isDisabled(), false);
  assert.equal(ui.textarea().isDisabled(), false);
  await ui.submit();
  const submissions = ui.requests.filter((request) => request.url.endsWith("/revisions"));
  assert.equal(submissions.length, 2);
  assert.deepEqual(submissions[0].body, submissions[1].body);
  assert.deepEqual(ui.opened, ["A_v2"]);
  assert.equal(ui.get("revision-dialog").open, false);
  assert.equal(ui.requests.some((request) => request.url.endsWith("/publish")), false);
  assert.match(ui.notices[0], /草稿.*单独发布/);
});

test("closing saved page A before its response cannot close or replace editor B", async () => {
  const pending = deferred();
  const ui = editor((url) => {
    if (url.endsWith("/revisions")) return pending.promise;
    return Promise.resolve(source(url.endsWith("chunk_B") ? "chunk_B" : "chunk_A"));
  });
  await ui.open(page("A"));
  const saving = ui.submit();
  ui.close();
  await ui.open(page("B"));
  pending.resolve({ id: "A_v2", version: 2, status: "draft" });
  await saving;
  assert.equal(ui.get("revision-dialog").open, true);
  assert.equal(ui.get("revision-title").value, "Page B");
  assert.equal(ui.textarea().value, "Claim B");
  assert.deepEqual(ui.opened, []);
  assert.deepEqual(ui.notices, []);
});

test("late save conflict from closed page A does not overwrite editor B status", async () => {
  const pending = deferred();
  const ui = editor((url) => url.endsWith("/revisions") ? pending.promise : Promise.resolve(source(url.split("/").pop())));
  await ui.open(page("A"));
  const saving = ui.submit();
  ui.close();
  await ui.open(page("B"));
  const before = ui.get("revision-state").textContent;
  pending.reject(new Error("409: old request conflict"));
  await saving;
  assert.equal(ui.get("revision-state").textContent, before);
  assert.equal(ui.get("save-revision").isDisabled(), false);
});

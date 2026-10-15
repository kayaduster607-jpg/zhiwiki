/* Upload state transitions against app.js; no browser or DOM package needed. */
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
    this._text = "";
    this._value = "";
    this.disabled = false;
    this.files = [];
    this.selectedOptions = [];
    this.dataset = {};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text; }
  set value(value) {
    this._value = value;
    // Match a browser file input: clearing value also clears its FileList.
    if (this.type === "file" && value === "") this.files = [];
  }
  get value() { return this._value; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this._text = ""; }
  addEventListener() {}
  querySelectorAll() { return []; }
  setAttribute() {}
  close() {}
  showModal() {}
  userValue(value) {
    if (this.disabled) return false;
    this.value = value;
    return true;
  }
  userFile(file) {
    if (this.disabled) return false;
    this.files = [file];
    this.value = "C:\\fakepath\\" + file.name;
    return true;
  }
  userOption(id, version) {
    if (this.disabled) return false;
    this.value = id;
    this.selectedOptions = [{ value: id, dataset: { version: String(version) } }];
    return true;
  }
}

class CollectedFormData {
  constructor() { this.entries = new Map(); }
  append(name, value) { this.entries.set(name, value); }
  get(name) { return this.entries.get(name); }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function response(payload, status = 200) {
  return { ok: status < 400, status, json: async () => payload };
}

function uploadUI(uploadRequest, documents = []) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(id.endsWith("-form") ? "form" : "div"));
      return elements.get(id);
    },
    createElement(tag) { return new Element(tag); },
    querySelectorAll() { return []; },
  };
  const get = (id) => document.getElementById(id);
  get("upload").type = "file";
  get("upload-mode").value = "create";
  get("upload-submit").tagName = "BUTTON";
  const requests = [];
  const sandbox = { document, window: {}, console, AbortController, TextDecoder,
    URLSearchParams, FormData: CollectedFormData,
    async fetch(url, options) {
      requests.push({ url, options });
      if (url.startsWith("/api/documents/upload?")) return uploadRequest(url, options);
      if (url === "/api/documents") return response({ documents });
      if (url === "/api/wiki") return response({ pages: [] });
      throw new Error("Unexpected request: " + url);
    },
  };
  vm.createContext(sandbox);
  const filename = path.join(__dirname, "..", "zhiwiki", "web", "app.js");
  const code = fs.readFileSync(filename, "utf8").replace(/start\(\);\s*$/, "");
  vm.runInContext(code, sandbox, { filename });
  sandbox.bindActions();
  return { get, requests, sandbox,
    submit: () => get("upload-form").onsubmit({ currentTarget: get("upload-form"), preventDefault() {} }),
    uploads: () => requests.filter((request) => request.url.startsWith("/api/documents/upload?")),
  };
}

function unlocked(ui) {
  for (const id of ["upload-submit", "upload", "upload-title", "upload-mode", "upload-target"]) {
    assert.equal(ui.get(id).disabled, false, id + " must be usable after the attempt finishes");
  }
}

test("successful create clears file/title so a later update does not inherit the previous name", async () => {
  const ui = uploadUI(async () => response({ version: 2, unchanged: false }));
  ui.get("upload").userFile({ name: "first.md" });
  ui.get("upload-title").userValue("First document title");
  await ui.submit();
  assert.equal(ui.get("upload").value, "");
  assert.deepEqual(ui.get("upload").files, []);
  assert.equal(ui.get("upload-title").value, "");
  unlocked(ui);

  ui.get("upload-mode").userValue("update");
  ui.get("upload-target").userOption("doc_second", 7);
  ui.get("upload").userFile({ name: "second.md" });
  await ui.submit();
  const first = new URL(ui.uploads()[0].url, "http://localhost");
  const second = new URL(ui.uploads()[1].url, "http://localhost");
  assert.equal(first.searchParams.get("title"), "First document title");
  assert.equal(second.searchParams.has("title"), false);
  assert.equal(second.searchParams.get("target_document_id"), "doc_second");
  assert.equal(second.searchParams.get("expected_version"), "7");
});

test("a failed upload preserves file/title/target and allows an explicit retry", async () => {
  let attempts = 0;
  const ui = uploadUI(async () => ++attempts === 1 ?
    response({ detail: "版本冲突，请核对所选资料" }, 409) : response({ version: 4, unchanged: false }));
  const file = { name: "revision.md" };
  ui.get("upload").userFile(file);
  ui.get("upload-title").userValue("Keep my edited title");
  ui.get("upload-mode").userValue("update");
  ui.get("upload-target").userOption("doc_chosen", 3);
  await ui.submit();
  assert.equal(ui.get("upload").files[0], file);
  assert.equal(ui.get("upload-title").value, "Keep my edited title");
  assert.equal(ui.get("upload-mode").value, "update");
  assert.equal(ui.get("upload-target").value, "doc_chosen");
  assert.match(ui.get("notice").textContent, /版本冲突/);
  unlocked(ui);
  await ui.submit();
  assert.equal(ui.uploads().length, 2);
  assert.equal(ui.uploads()[0].url, ui.uploads()[1].url);
  assert.equal(ui.get("upload").value, "");
  assert.equal(ui.get("upload-title").value, "");
});

test("pending upload rejects duplicate form submit and prevents changing all four inputs", async () => {
  const pending = deferred();
  const ui = uploadUI(() => pending.promise);
  const original = { name: "reviewed.md" };
  ui.get("upload").userFile(original);
  ui.get("upload-title").userValue("Reviewed title");
  ui.get("upload-mode").userValue("update");
  ui.get("upload-target").userOption("doc_reviewed", 5);
  const first = ui.submit();
  const duplicate = ui.submit();
  assert.equal(ui.get("upload").userFile({ name: "different.md" }), false);
  assert.equal(ui.get("upload-title").userValue("Different title"), false);
  assert.equal(ui.get("upload-mode").userValue("create"), false);
  assert.equal(ui.get("upload-target").userOption("doc_other", 9), false);
  const sentWhilePending = ui.uploads().length;
  pending.resolve(response({ version: 6, unchanged: false }));
  await Promise.all([first, duplicate]);
  assert.equal(sentWhilePending, 1);
  assert.equal(ui.uploads().length, 1);
  assert.equal(ui.uploads()[0].options.method, "POST");
  assert.equal(ui.uploads()[0].options.body.get("file"), original);
  const query = new URL(ui.uploads()[0].url, "http://localhost").searchParams;
  assert.equal(query.get("expected_version"), "5");
  assert.equal(query.get("target_document_id"), "doc_reviewed");
  unlocked(ui);
});

test("update sends the explicitly selected version rather than silently using fresher metadata", async () => {
  const ui = uploadUI(async () => response({ detail: "所选版本已失效" }, 409),
    [{ id: "doc_newer", version: 8, title: "Newer catalog", source_key: "source:stable" }]);
  ui.get("upload-mode").userValue("update");
  ui.get("upload-target").userOption("doc_selected", 3);
  ui.get("upload").userFile({ name: "updated.md" });
  // A background catalogue refresh must not grant an unseen version approval.
  await ui.sandbox.refreshDocuments();
  await ui.submit();
  const query = new URL(ui.uploads()[0].url, "http://localhost").searchParams;
  assert.equal(query.get("target_document_id"), "doc_selected");
  assert.equal(query.get("expected_version"), "3");
  assert.equal(ui.requests.some((request) => request.url === "/api/documents/doc_selected"), false);
  assert.match(ui.get("notice").textContent, /失效/);
  assert.equal(ui.get("upload").files.length, 1);
});

test("missing update target fails locally without discarding the pending file", async () => {
  const ui = uploadUI(async () => { throw new Error("upload should not be called"); });
  ui.get("upload-mode").userValue("update");
  ui.get("upload").userFile({ name: "pending.md" });
  ui.get("upload-title").userValue("Pending title");
  await ui.submit();
  assert.equal(ui.uploads().length, 0);
  assert.equal(ui.get("upload").files[0].name, "pending.md");
  assert.equal(ui.get("upload-title").value, "Pending title");
  assert.match(ui.get("notice").textContent, /请选择/);
  unlocked(ui);
});

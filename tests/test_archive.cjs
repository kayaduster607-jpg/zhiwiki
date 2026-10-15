/* Navigation, lifecycle and source truth regressions for the archive experience. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const model = require("../zhiwiki/web/archive-model.js");
const documentA = { id:"doc-a", title:"<img onerror=alert(1)>", version:2, active:true, chunk_ids:["a", "b"] };
const pageA = { id:"wiki-a", title:"Verified claim", version:1, status:"published", stale:false,
  claims:[{ text:"A verbatim claim", evidence_ids:["a", "b", "a"] }] };

test("routes accept only the application's finite views", () => {
  for (const view of ["overview", "query", "documents", "wiki", "architecture"]) assert.equal(model.route("#" + view), view);
  for (const hash of ["#<script>", "#query/other", "#%71uery", "#archive-evidence", undefined]) assert.equal(model.route(hash), null);
});
test("proof deduplicates references and original documents without changing text", () => {
  const proof = model.proof([documentA], [pageA]);
  assert.equal(proof.referenceCount, 2); assert.equal(proof.sources.length, 1);
  assert.equal(proof.claim.text, pageA.claims[0].text);
});
test("draft and stale pages never become homepage evidence", () => {
  assert.equal(model.proof([documentA], [{...pageA, status:"draft"}, {...pageA, stale:true}]), null);
});
test("old, missing or ambiguous original references fail closed", () => {
  assert.equal(model.proof([{...documentA, active:false}], [pageA]), null);
  assert.equal(model.proof([{...documentA, chunk_ids:["a"]}], [pageA]), null);
  assert.equal(model.proof([documentA, {...documentA, id:"doc-b"}, {...documentA, id:"doc-c"}], [pageA]), null);
});
test("a later fully supported claim can be shown without fabricating the failed one", () => {
  const page = {...pageA, claims:[{text:"Missing", evidence_ids:["missing"]}, ...pageA.claims]};
  assert.equal(model.proof([documentA], [page]).claim.text, "A verbatim claim");
});

function fixture(options = {}) {
  const elements = new Map(), documentEvents = new Map(), windowEvents = new Map(), frames = new Map();
  const observers = [], pushes = [], opened = [], notices = []; let frameId = 0, submitted = 0;
  function classes() {
    const items = new Set();
    return { contains:x=>items.has(x), remove:x=>items.delete(x), toggle(x, force) {
      const value = force === undefined ? !items.has(x) : force;
      value ? items.add(x) : items.delete(x); return value;
    }};
  }
  class Element {
    constructor(tag = "div") {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.value = "";
      this.hidden = false; this.disabled = false; this.attributes = {}; this.classList = classes();
      this.style = { setProperty() {}, removeProperty() {} }; this.events = new Map();
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
    querySelectorAll() { return []; }
    querySelector() { return element("heading"); }
    addEventListener(key, handler) { this.events.set(key, handler); }
    focus() { document.activeElement = this; }
    getBoundingClientRect() { return {width:1200, height:800}; }
    getContext() { if (options.canvasFails) throw new Error("no canvas"); return null; }
    requestSubmit() { submitted++; }
    scrollIntoView() {}
  }
  function element(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }
  const document = { body:element("body"), hidden:false, getElementById:element,
    querySelector:selector=>selector === "dialog[open]" ? null : element(selector),
    querySelectorAll:()=>[], createElement:tag=>new Element(tag),
    addEventListener:(key, handler)=>documentEvents.set(key, handler) };
  const window = { ZhiArchiveModel:model, ZhiEffects:{isEnabled:()=>options.motion !== false},
    location:{hash:options.hash || ""}, history:{
      pushState(state, title, hash) { pushes.push(hash); window.location.hash = hash; },
      replaceState(state, title, hash) { window.location.hash = hash; } },
    matchMedia:()=>({matches:false}), innerWidth:1200, devicePixelRatio:2, scrollY:0,
    addEventListener:(key, handler)=>windowEvents.set(key, handler) };
  const sandbox = {window, document, ResizeObserver:class { observe() {} },
    IntersectionObserver:class { constructor(callback) { observers.push(callback); } observe() {} },
    requestAnimationFrame:callback=>{ frames.set(++frameId, callback); return frameId; },
    cancelAnimationFrame:id=>frames.delete(id) };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../zhiwiki/web/archive.js"), "utf8"), sandbox);
  const api = window.ZhiExperience;
  const context = {showView:name=>api.setView(name), getDocuments:()=>[documentA], getPages:()=>[pageA],
    openDoc:id=>opened.push(id), openWiki:id=>opened.push(id), safe:fn=>fn, notify:msg=>notices.push(msg)};
  api.init(context);
  return {api, context, element, document, window, frames, observers, pushes, opened, notices,
    documentEvents, windowEvents, submitted:()=>submitted, options};
}

test("deep links and browser back preserve user input without pushing another history entry", () => {
  const f = fixture({hash:"#query"});
  assert.equal(f.document.body.dataset.view, "query");
  f.element("question").value = "Unsubmitted work";
  f.api.setView("documents"); assert.deepEqual(f.pushes, ["#documents"]);
  f.window.location.hash = "#query"; f.windowEvents.get("popstate")();
  assert.equal(f.document.body.dataset.view, "query");
  assert.equal(f.element("question").value, "Unsubmitted work");
  assert.equal(f.pushes.length, 1);
});
test("entry submission delegates to the existing form exactly once", () => {
  const f = fixture(); f.element("entry-question").value = "  Warranty?  ";
  f.element("entry-form").onsubmit({preventDefault(){}});
  assert.equal(f.element("question").value, "Warranty?");
  assert.equal(f.submitted(), 1); assert.equal(f.document.body.dataset.view, "query");
});
test("an active investigation cannot be overwritten by a second entry submission", () => {
  const f = fixture(); f.element("question").value = "First"; f.element("ask").disabled = true;
  f.element("entry-question").value = "Second"; f.element("entry-form").onsubmit({preventDefault(){}});
  assert.equal(f.element("question").value, "First"); assert.equal(f.submitted(), 0); assert.equal(f.notices.length, 1);
});
test("motion maintains one loop and stops for hidden, offscreen and inactive views", () => {
  const f = fixture(); assert.equal(f.frames.size, 1);
  f.api.setView("overview"); assert.equal(f.frames.size, 1);
  f.document.hidden = true; f.documentEvents.get("visibilitychange")(); assert.equal(f.frames.size, 0);
  f.document.hidden = false; f.documentEvents.get("visibilitychange")(); assert.equal(f.frames.size, 1);
  f.observers[0]([{isIntersecting:false}]); assert.equal(f.frames.size, 0);
  f.observers[0]([{isIntersecting:true}]); assert.equal(f.frames.size, 1);
  f.api.setView("wiki"); assert.equal(f.frames.size, 0);
});
test("reduced motion and canvas failure leave navigation and proof accessible", () => {
  const f = fixture({motion:false, canvasFails:true});
  assert.equal(f.frames.size, 0); f.api.update();
  const source = f.element("proof-sources").children[0];
  assert.equal(source.children[0].children[0].textContent, documentA.title);
  source.onclick(); assert.deepEqual(f.opened, ["doc-a"]);
  f.api.setView("query"); assert.equal(f.document.body.dataset.view, "query");
});
test("mobile menu Escape returns focus and view navigation closes the menu", () => {
  const f = fixture(); f.element("entry-menu").onclick();
  assert.equal(f.element("entry-menu").attributes["aria-expanded"], "true");
  f.documentEvents.get("keydown")({key:"Escape"});
  assert.equal(f.document.activeElement, f.element("entry-menu"));
  f.element("entry-menu").onclick(); f.api.setView("documents");
  assert.equal(f.element("entry-menu").attributes["aria-expanded"], "false");
});
test("offline reader preserves source and full output as plain text", () => {
  const f = fixture();
  const result = {answer:"Raw output <script>", citations:[{title:"Source", text:"Original <img>"}]};
  f.api.presentOffline(result, f.element("answer"));
  const parts = f.element("answer").children;
  assert.equal(parts[1].children[1].textContent, "Original <img>");
  assert.equal(parts[2].children[1].textContent, result.answer);
});

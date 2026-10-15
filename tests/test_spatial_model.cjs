/* Run with node --test; all geometry and citation checks use standard libraries. */
"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { build, project, rotate, CAMERA_Z, VERTICAL_FOV } = require("../zhiwiki/web/spatial-model.js");

function document(id, options = {}) {
  return { id, title: `资料 ${id}`, active: true, version: 1, chunk_ids: [`${id}:1`], ...options };
}

function wiki(id, sourceIds, options = {}) {
  return { id, title: `Wiki ${id}`, status: "published", stale: false, version: 1, source_ids: sourceIds, ...options };
}

function close(actual, expected) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} ≈ ${expected}`);
}

test("only real current chunk ownership creates document-level citation edges", () => {
  const model = build([
    document("a", { chunk_ids: ["a:1", "a:2"] }), document("b"), document("old", { active: false }),
  ], [wiki("wiki", ["a:1", "a:2", "a:1", "missing", "old:1"])]);
  assert.deepEqual(model.edges, [{ from: "wiki", to: "a" }]);
  assert.deepEqual(model.counts, {
    documents: 2, wikis: 1, displayedDocuments: 2, displayedWikis: 1, relationships: 1, displayedRelationships: 1,
  });
  assert.equal(model.displayTotal, 3);
  assert.equal(model.total, 3);
});

test("stale, draft and archived Wiki pages never become spatial nodes or edges", () => {
  const model = build([document("a")], [
    wiki("valid", ["a:1"]), wiki("stale", ["a:1"], { stale: true }),
    wiki("draft", ["a:1"], { status: "draft" }), wiki("archived", ["a:1"], { status: "archived" }),
  ]);
  assert.deepEqual(model.nodes.map((item) => item.id), ["a", "valid"]);
  assert.deepEqual(model.edges, [{ from: "valid", to: "a" }]);
});

test("visible Wiki citations take priority over recent unrelated documents", () => {
  const docs = Array.from({ length: 12 }, (_, index) => document(`d${index}`, {
    created_at: `2026-09-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
  }));
  const model = build(docs, [wiki("w", ["d0:1", "d1:1"])]);
  assert.equal(model.counts.displayedDocuments, 8);
  assert.deepEqual(model.nodes.filter((item) => item.kind === "document").map((item) => item.id),
    ["d0", "d1", "d11", "d10", "d9", "d8", "d7", "d6"]);
  assert.equal(model.counts.documents, 12);
});

test("round-robin source selection covers four Wiki pages before the eight-document limit", () => {
  const docs = Array.from({ length: 12 }, (_, index) => document(`d${index}`));
  const pages = [wiki("w0", docs.slice(0, 9).map((doc) => doc.chunk_ids[0])),
    wiki("w1", ["d9:1"]), wiki("w2", ["d10:1"]), wiki("w3", ["d11:1"]), wiki("w4", ["d8:1"])];
  const model = build(docs, pages);
  assert.equal(model.counts.displayedWikis, 4);
  assert.equal(model.counts.wikis, 5);
  assert.equal(model.displayTotal, 12);
  assert.equal(model.total, 17);
  for (const id of ["w0", "w1", "w2", "w3"]) assert.ok(model.edges.some((edge) => edge.from === id));
  const displayedIds = new Set(model.nodes.map((item) => item.id));
  assert.ok(model.edges.every((edge) => displayedIds.has(edge.from) && displayedIds.has(edge.to)));
});

test("rebuilding after a source version changes cannot retain an old citation edge", () => {
  const page = wiki("w", ["old:1"]);
  assert.equal(build([document("old")], [page]).edges.length, 1);
  const changed = build([document("old", { active: false }), document("new", { version: 2 })], [page]);
  assert.equal(changed.edges.length, 0);
  assert.equal(changed.nodes.find((item) => item.kind === "document").version, 2);
  assert.deepEqual(build([document("new", { version: 2 })], [wiki("w2", ["new:1"], { version: 2 })]).edges,
    [{ from: "w2", to: "new" }]);
});

test("empty or malformed optional collections are safe and duplicate row IDs are not displayed twice", () => {
  assert.deepEqual(build().nodes, []);
  assert.deepEqual(build([], []).edges, []);
  assert.equal(build(null, undefined).displayTotal, 0);
  const result = build([null, document("a"), document("a"), { id: 9 }, document("b", { chunk_ids: null })],
    [wiki("w", null), wiki("w", ["a:1"])]);
  assert.equal(result.nodes.length, 3);
  assert.equal(result.edges.length, 0);
});

test("ambiguous chunk ownership fails closed instead of claiming either document is the source", () => {
  const result = build([document("a", { chunk_ids: ["shared"] }), document("b", { chunk_ids: ["shared"] })],
    [wiki("w", ["shared"])]);
  assert.deepEqual(result.edges, []);
});

test("deterministic geometry stays on its documented shells and never mutates API records", () => {
  const docs = Array.from({ length: 10 }, (_, index) => document(`d${index}`));
  const pages = Array.from({ length: 4 }, (_, index) => wiki(`w${index}`, [`d${index}:1`]));
  const original = JSON.stringify({ docs, pages });
  const first = build(docs, pages);
  assert.deepEqual(build(docs, pages), first);
  for (const item of first.nodes) {
    const radius = Math.hypot(...item.point);
    assert.ok(item.kind === "document" ? radius >= 1.65 && radius <= 2.15 : radius >= 1.35 && radius <= 1.75);
    assert.equal(project(item.point, { width: 800, height: 500 }).visible, true);
  }
  first.nodes[0].chunk_ids.push("external-change");
  assert.equal(JSON.stringify({ docs, pages }), original);
});

test("perspective uses camera z=6, a 45-degree vertical FOV and screen center origin", () => {
  const view = { width: 800, height: 600 };
  assert.deepEqual(project([0, 0, 0], view), {
    x: 400, y: 300, depth: 6, scale: 600 / (2 * Math.tan(Math.PI / 8)) / 6, visible: true,
  });
  assert.equal(CAMERA_Z, 6);
  assert.equal(VERTICAL_FOV, Math.PI / 4);
  assert.ok(project([1, 1, 1], view).scale > project([1, 1, -1], view).scale);
  assert.ok(project([1, 1, 0], view).x > 400);
  assert.ok(project([1, 1, 0], view).y < 300);
});

test("rotation order is Y then X, using radians and column vectors", () => {
  const turned = rotate([1, 2, 3], Math.PI / 2, Math.PI / 2);
  [3, 1, 2].forEach((expected, index) => close(turned[index], expected));
  const expected = project([3, 1, 2], { width: 800, height: 600 });
  const actual = project([1, 2, 3], { width: 800, height: 600, yaw: Math.PI / 2, pitch: Math.PI / 2 });
  ["x", "y", "depth", "scale"].forEach((key) => close(actual[key], expected[key]));
});

test("uninitialized viewport, invalid points, clipping and extreme input never expose non-finite coordinates", () => {
  const inputs = [
    [[1, 2, 3], {}], [[1, 2, 3], { width: 0, height: 500 }], [[1, 2, 3], { width: -1, height: 500 }],
    [[NaN, 0, 0], { width: 800, height: 600 }], [null, { width: 800, height: 600 }],
    [[1e308, 1e308, 1e308], { width: 800, height: 600 }], [[0, 0, 7], { width: 800, height: 600 }],
    [[100, 0, 0], { width: 800, height: 600 }],
  ];
  for (const [point, view] of inputs) {
    const result = project(point, view);
    assert.equal(result.visible, false);
    assert.ok([result.x, result.y, result.depth, result.scale].every(Number.isFinite));
  }
});

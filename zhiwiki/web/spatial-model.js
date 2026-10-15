/* Pure spatial presentation model. No inferred similarity or synthetic edges. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ZhiSpatialModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DOCUMENT_LIMIT = 8;
  const WIKI_LIMIT = 4;
  const CAMERA_Z = 6;
  const VERTICAL_FOV = Math.PI / 4;

  function uniqueRows(value, predicate) {
    const seen = new Set();
    return (Array.isArray(value) ? value : []).filter((row) => {
      if (!row || typeof row.id !== "string" || !row.id || seen.has(row.id) || !predicate(row)) return false;
      seen.add(row.id);
      return true;
    });
  }

  function ids(value) {
    return [...new Set((Array.isArray(value) ? value : []).filter((id) => typeof id === "string" && id))];
  }

  function recentFirst(rows) {
    // Preserve input order when timestamps tie or are absent; never mutate API rows.
    const timestamp = (row) => Date.parse(row.created_at) || 0;
    return rows.map((row, index) => ({ row, index }))
      .sort((a, b) => timestamp(b.row) - timestamp(a.row) || a.index - b.index)
      .map((item) => item.row);
  }

  function chunkOwners(documents) {
    const owners = new Map();
    documents.forEach((doc) => ids(doc.chunk_ids).forEach((id) => {
      // The store gives each chunk one owner. Fail closed on ambiguous external input.
      owners.set(id, owners.has(id) && owners.get(id) !== doc.id ? null : doc.id);
    }));
    return owners;
  }

  function sourceDocuments(page, owners) {
    return [...new Set(ids(page.source_ids).map((id) => owners.get(id)).filter(Boolean))];
  }

  function selectDocuments(documents, pages, owners) {
    const selected = new Set();
    const references = pages.map((page) => sourceDocuments(page, owners));
    const longest = Math.max(0, ...references.map((items) => items.length));
    // Round-robin citations give each visible Wiki a chance to display its sources.
    for (let index = 0; index < longest && selected.size < DOCUMENT_LIMIT; index += 1) {
      for (const row of references) {
        if (row[index] && selected.size < DOCUMENT_LIMIT) selected.add(row[index]);
      }
    }
    for (const doc of documents) {
      if (selected.size >= DOCUMENT_LIMIT) break;
      selected.add(doc.id);
    }
    const byId = new Map(documents.map((doc) => [doc.id, doc]));
    return [...selected].map((id) => byId.get(id));
  }

  function onRadius(point, radius) {
    const length = Math.hypot(...point);
    return point.map((value) => value * radius / length);
  }

  function documentPoint(index) {
    // A staggered shell is a visual layout only; distance has no semantic meaning.
    const angles = [-155, -110, -62, -18, 24, 65, 112, 158];
    const angle = angles[index] * Math.PI / 180;
    return onRadius([Math.cos(angle), Math.sin(angle) * 0.78, index % 2 ? -0.28 : 0.32], index % 2 ? 2.12 : 1.94);
  }

  function wikiPoint(index) {
    const points = [[-0.88, 0.5, 0.95], [0.92, 0.48, -0.95], [-0.92, -0.48, -0.95], [0.88, -0.5, 0.95]];
    return onRadius(points[index], index % 2 ? 1.64 : 1.46);
  }

  function node(row, kind, point) {
    return {
      id: row.id,
      title: typeof row.title === "string" ? row.title : "",
      kind,
      version: row.version,
      point,
      ...(kind === "document" ? { chunk_ids: ids(row.chunk_ids) } : { source_ids: ids(row.source_ids) }),
    };
  }

  function build(documents, pages) {
    const current = recentFirst(uniqueRows(documents, (doc) => doc.active !== false && doc.active !== 0));
    const published = recentFirst(uniqueRows(pages, (page) => page.status === "published" && !page.stale));
    const visibleWikis = published.slice(0, WIKI_LIMIT);
    const owners = chunkOwners(current);
    const visibleDocuments = selectDocuments(current, visibleWikis, owners);
    const visibleIds = new Set(visibleDocuments.map((doc) => doc.id));
    const nodes = visibleDocuments.map((doc, index) => node(doc, "document", documentPoint(index)))
      .concat(visibleWikis.map((page, index) => node(page, "wiki", wikiPoint(index))));
    const edges = [];
    visibleWikis.forEach((page) => sourceDocuments(page, owners).forEach((id) => {
      if (visibleIds.has(id)) edges.push({ from: page.id, to: id });
    }));
    return {
      nodes,
      edges,
      total: current.length + published.length,
      displayTotal: nodes.length,
      counts: {
        documents: current.length,
        wikis: published.length,
        displayedDocuments: visibleDocuments.length,
        displayedWikis: visibleWikis.length,
        relationships: published.reduce((sum, page) => sum + sourceDocuments(page, owners).length, 0),
        displayedRelationships: edges.length,
      },
    };
  }

  function finite(value, fallback) {
    return Number.isFinite(value) ? value : fallback;
  }

  function rotate(point, yaw, pitch) {
    const [x, y, z] = point;
    const cy = Math.cos(finite(yaw, 0));
    const sy = Math.sin(finite(yaw, 0));
    const cp = Math.cos(finite(pitch, 0));
    const sp = Math.sin(finite(pitch, 0));
    // Column vectors: R = Rx(pitch) * Ry(yaw), so Y rotation happens first.
    const x1 = cy * x + sy * z;
    const z1 = -sy * x + cy * z;
    return [x1, cp * y - sp * z1, sp * y + cp * z1];
  }

  function project(point, view) {
    const settings = view || {};
    const width = Math.max(0, finite(settings.width, 0));
    const height = Math.max(0, finite(settings.height, 0));
    const hidden = { x: width / 2, y: height / 2, depth: CAMERA_Z, scale: 0, visible: false };
    if (!width || !height || !Array.isArray(point) || point.length !== 3 || !point.every(Number.isFinite)) return hidden;
    const [x, y, z] = rotate(point, settings.yaw, settings.pitch);
    const depth = CAMERA_Z - z;
    const focalLength = height / (2 * Math.tan(VERTICAL_FOV / 2));
    const scale = focalLength / Math.max(depth, 0.1);
    const screenX = width / 2 + x * scale;
    const screenY = height / 2 - y * scale;
    if (![screenX, screenY, depth, scale].every(Number.isFinite)) return hidden;
    return {
      x: screenX, y: screenY, depth, scale,
      visible: depth > 0.1 && screenX >= 0 && screenX <= width && screenY >= 0 && screenY <= height,
    };
  }

  return Object.freeze({ build, project, rotate, CAMERA_Z, VERTICAL_FOV });
});

/* Pure helpers keep navigation and source selection testable without a browser. */
(function (root, factory) {
  const value = factory();
  if (typeof module === "object" && module.exports) module.exports = value;
  else root.ZhiArchiveModel = value;
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";
  const views = new Set(["overview", "query", "documents", "wiki", "architecture"]);

  function route(hash) {
    const value = String(hash || "").replace(/^#/, "");
    return views.has(value) ? value : null;
  }

  function sourceOwners(documents) {
    const owners = new Map();
    for (const doc of documents) {
      if (doc.active === false) continue;
      for (const id of new Set(doc.chunk_ids || [])) {
        // Duplicate ownership is ambiguous: never guess which original supports a claim.
        owners.set(id, owners.has(id) ? null : doc);
      }
    }
    return owners;
  }

  function proof(documents, pages) {
    const owners = sourceOwners(documents);
    for (const page of pages) {
      if (page.status !== "published" || page.stale) continue;
      for (const claim of page.claims || []) {
        const ids = [...new Set(claim.evidence_ids || [])];
        if (!claim.text || !ids.length || ids.some((id) => !owners.get(id))) continue;
        const sources = [...new Map(ids.map((id) => [owners.get(id).id, owners.get(id)])).values()];
        return { page, claim, sources, referenceCount: ids.length };
      }
    }
    return null;
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, Number.isFinite(value) ? value : minimum));
  }

  return { route, proof, clamp };
});

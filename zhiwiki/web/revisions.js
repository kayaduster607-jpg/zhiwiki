/* 人工修订只创建新草稿。所有原文与结论用 DOM 文本节点显示。 */
"use strict";
window.ZhiRevisions = (() => {
  const get = (id) => document.getElementById(id);
  let context;
  let generation = 0;
  let sourceGeneration = 0;
  let current = null;
  let activeClaim = 0;
  const evidence = new Map();

  function close() {
    generation += 1;
    sourceGeneration += 1;
    current = null;
    get("revision-dialog").close();
  }

  function updateReferenceLabels() {
    get("revision-claims").querySelectorAll(".reference-button").forEach((button, index) => {
      button.textContent = "选择原文引用 · " + current.claims[index].evidence_ids.length + " 项";
      button.setAttribute("aria-pressed", String(index === activeClaim));
    });
  }

  function renderClaims() {
    get("revision-claims").replaceChildren();
    current.claims.forEach((claim, index) => {
      const row = context.node("section", undefined, "revision-claim");
      const head = context.node("div", undefined, "section-head");
      head.append(context.node("h3", "结论 " + (index + 1)));
      const remove = context.node("button", "移除此条", "text-button");
      remove.type = "button";
      remove.disabled = current.claims.length === 1;
      remove.onclick = () => {
        current.claims.splice(index, 1);
        activeClaim = Math.min(activeClaim, current.claims.length - 1);
        renderClaims(); renderEvidence();
      };
      head.append(remove);
      const input = context.node("textarea");
      input.value = claim.text; input.rows = 3; input.maxLength = 10000;
      input.required = true; input.setAttribute("aria-label", "结论 " + (index + 1) + " 内容");
      input.oninput = () => { claim.text = input.value; };
      const reference = context.node("button", undefined, "secondary reference-button");
      reference.type = "button";
      reference.onclick = () => {
        activeClaim = index; updateReferenceLabels(); renderEvidence();
        // 手机布局中引用区位于结论下方，让选择动作直接带到对应原文。
        get("revision-source-heading").scrollIntoView({ block: "start" });
      };
      row.append(head, input, reference);
      get("revision-claims").append(row);
    });
    updateReferenceLabels();
  }

  function renderEvidence() {
    if (!current) return;
    get("revision-source-heading").textContent = "为结论 " + (activeClaim + 1) + " 选择原文";
    get("revision-evidence").replaceChildren();
    const claim = current.claims[activeClaim];
    for (const source of evidence.values()) {
      const row = context.node("div", undefined, "revision-evidence");
      const label = context.node("label");
      const checkbox = context.node("input");
      checkbox.type = "checkbox";
      checkbox.checked = claim.evidence_ids.includes(source.id);
      checkbox.disabled = !source.active && !checkbox.checked;
      checkbox.onchange = () => {
        claim.evidence_ids = checkbox.checked ?
          [...new Set([...claim.evidence_ids, source.id])] : claim.evidence_ids.filter((id) => id !== source.id);
        if (!source.active) checkbox.disabled = true;
        updateReferenceLabels();
      };
      label.append(checkbox, context.node("span", source.title + " · v" + source.version));
      const location = context.node("p", source.locator + (source.active ? " · 当前有效" : " · 已失效，请移除并替换"), source.active ? "hint" : "hint budget-over");
      const details = context.node("details");
      details.append(context.node("summary", "展开原文"), context.node("div", source.text, "prewrap"));
      row.append(label, location, details);
      get("revision-evidence").append(row);
    }
    if (!evidence.size) get("revision-evidence").append(context.node("p", "选择上方资料以载入可引用的片段。", "hint"));
  }

  function populateDocuments() {
    const placeholder = context.node("option", "从当前资料加入更多片段");
    placeholder.value = "";
    get("revision-document").replaceChildren(placeholder);
    for (const doc of context.getDocuments()) {
      const option = context.node("option", doc.title + " · v" + doc.version);
      option.value = doc.id;
      get("revision-document").append(option);
    }
  }

  async function loadDocument() {
    const epoch = generation, sourceEpoch = ++sourceGeneration;
    const id = get("revision-document").value;
    if (!id) return;
    const doc = await context.api("/api/documents/" + id);
    if (epoch !== generation || sourceEpoch !== sourceGeneration || !current) return;
    for (const chunk of doc.chunks) evidence.set(chunk.id, chunk);
    renderEvidence();
  }

  async function open(page) {
    context.closeDetail();
    const epoch = ++generation;
    current = { id: page.id, version: page.version, links: [...page.links],
      claims: page.claims.map((claim) => ({ text: claim.text, evidence_ids: [...claim.evidence_ids] })) };
    activeClaim = 0; evidence.clear();
    get("revision-title").value = page.title;
    get("revision-note").value = "";
    get("revision-fields").disabled = false;
    get("revision-state").textContent = "基于 v" + page.version + " 创建新草稿，原版本保持可追溯。";
    get("save-revision").disabled = true;
    renderClaims(); renderEvidence(); populateDocuments();
    get("revision-dialog").showModal();
    try {
      // 初始引用必须全部载入，失效来源也要显示出来供人工替换。
      for (const id of page.source_ids) {
        const source = await context.api("/api/evidence/" + id);
        if (epoch !== generation || !current) return;
        evidence.set(source.id, source);
      }
      renderEvidence();
    } catch (error) {
      if (epoch === generation) get("revision-state").textContent = "引用加载失败：" + error.message;
      return;
    }
    if (epoch === generation) get("save-revision").disabled = false;
  }

  function validateClaims() {
    for (const claim of current.claims) {
      if (!claim.text.trim() || !claim.evidence_ids.length) throw new Error("每条结论都需要内容和至少一条有效原文引用。");
      if (claim.evidence_ids.some((id) => !evidence.get(id) || !evidence.get(id).active)) {
        throw new Error("存在失效原文引用，请先移除并选择当前版本的片段。");
      }
    }
  }

  async function save() {
    // Enter/脚本触发 submit 也要遵守加载与保存锁，避免重复创建版本。
    if (!current || get("save-revision").disabled) return;
    validateClaims();
    const epoch = generation;
    get("save-revision").disabled = true;
    get("revision-fields").disabled = true;
    try {
      const page = await context.api("/api/wiki/" + current.id + "/revisions", {
        expected_version: current.version, title: get("revision-title").value,
        claims: current.claims, links: current.links, revision_note: get("revision-note").value || null });
      await context.refreshWiki();
      if (epoch !== generation) return;
      close();
      await context.openWiki(page.id);
      context.notify("人工修订已保存为 v" + page.version + " 草稿。请核对来源后单独发布。");
    } catch (error) {
      if (epoch === generation) get("revision-state").textContent = error.message;
    } finally {
      if (epoch === generation) {
        get("save-revision").disabled = false;
        get("revision-fields").disabled = false;
      }
    }
  }

  function init(options) {
    context = options;
    get("close-revision").onclick = close;
    get("revision-dialog").addEventListener("cancel", () => { generation += 1; sourceGeneration += 1; current = null; });
    get("revision-form").onsubmit = context.safe(save);
    get("revision-document").onchange = context.safe(loadDocument);
    get("add-claim").onclick = () => {
      if (!current || current.claims.length >= 100) return;
      current.claims.push({ text: "", evidence_ids: [] });
      activeClaim = current.claims.length - 1;
      renderClaims(); renderEvidence();
    };
  }

  return { init, open, close };
})();

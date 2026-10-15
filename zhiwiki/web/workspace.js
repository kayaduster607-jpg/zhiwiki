/* 工作区呈现层：只读取公开 API 数据；不把来源文本作为 HTML 执行。 */
"use strict";
window.ZhiWorkspace = (() => {
  const byId = (id) => document.getElementById(id);
  let context;
  let catalog = new Map();
  let wikiStatus = "";
  let chunkGeneration = 0;
  let documentGeneration = 0;
  let uploadGeneration = 0;

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(svg.namespaceURI, "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }

  function empty(title, description, symbol = "file") {
    const block = context.node("div", undefined, "empty-state");
    block.append(icon(symbol), context.node("h3", title), context.node("p", description));
    return block;
  }

  function metadata(doc) {
    return catalog.get(doc.source_key) || { category: "自有资料", summary: "导入的原始资料，点击标题查看全文与版本。", updated_date: "", fictional: false };
  }

  function fileTile() {
    const tile = context.node("span", undefined, "file-tile");
    tile.append(icon("file"));
    return tile;
  }

  function documentCard(doc, index) {
    const meta = metadata(doc);
    const card = context.node("article", undefined, "document-card");
    card.style.setProperty("--index", Math.min(index, 8));
    const top = context.node("div", undefined, "card-top");
    top.append(fileTile(), context.node("span", meta.category, "category-badge"));
    const heading = context.node("h2");
    const title = context.node("button", doc.title, "title-button");
    title.onclick = context.safe(() => context.openDoc(doc.id));
    heading.append(title);
    const foot = context.node("div", undefined, "card-footer");
    foot.append(context.node("span", (meta.fictional ? "虚构样例 · " : "") + doc.chunk_ids.length + " 个片段"),
      context.node("span", "v" + doc.version, "version"));
    card.append(top, heading, context.node("p", meta.summary, "card-description"), foot);
    return card;
  }

  function renderDocuments() {
    const docs = context.getDocuments();
    const query = byId("document-search").value.trim().toLowerCase();
    const category = byId("document-category").value;
    const visible = docs.filter((doc) => {
      const meta = metadata(doc);
      return (!category || meta.category === category) &&
        [doc.title, doc.source_key, meta.summary].join(" ").toLowerCase().includes(query);
    });
    byId("documents-list").replaceChildren(...visible.map(documentCard));
    if (!visible.length) byId("documents-list").append(empty(docs.length ? "没有匹配的资料" : "给知识空间加入第一份资料", docs.length ? "试试其他关键词或分类。" : "添加本地资料，或载入 16 份虚构业务样例。"));
  }

  function renderSourcePicker() {
    const previous = new Set(Array.from(document.querySelectorAll("#wiki-sources input:checked")).map((input) => input.value));
    byId("wiki-sources").replaceChildren();
    for (const doc of context.getDocuments()) {
      const label = context.node("label");
      const checkbox = context.node("input");
      checkbox.type = "checkbox"; checkbox.value = doc.id; checkbox.checked = previous.has(doc.id);
      checkbox.onchange = updateBudget;
      const text = context.node("span", doc.title);
      text.append(context.node("small", doc.chunk_ids.length + " 个片段 · v" + doc.version));
      label.append(checkbox, text);
      byId("wiki-sources").append(label);
    }
    updateBudget();
  }

  function updateBudget() {
    const ids = new Set(Array.from(document.querySelectorAll("#wiki-sources input:checked")).map((input) => input.value));
    const chunks = context.getDocuments().filter((doc) => ids.has(doc.id)).reduce((sum, doc) => sum + doc.chunk_ids.length, 0);
    byId("selection-budget").textContent = "已选 " + ids.size + " 份 · " + chunks + " 个片段";
    byId("selection-budget").classList.toggle("budget-over", ids.size > 12 || chunks > 20);
    return { documents: ids.size, chunks };
  }

  function refreshSelectors() {
    const selected = byId("document-category").value;
    const categories = [...new Set(context.getDocuments().map((doc) => metadata(doc).category))].sort();
    const all = context.node("option", "全部分类"); all.value = "";
    byId("document-category").replaceChildren(all);
    for (const category of categories) {
      const option = context.node("option", category); option.value = category; byId("document-category").append(option);
    }
    if (categories.includes(selected)) byId("document-category").value = selected;
    const selectedDoc = byId("chunk-document").value;
    const placeholder = context.node("option", "选择资料，或直接输入下方文本"); placeholder.value = "";
    byId("chunk-document").replaceChildren(placeholder);
    for (const doc of context.getDocuments()) {
      const option = context.node("option", doc.title); option.value = doc.id; byId("chunk-document").append(option);
    }
    if (context.getDocuments().some((doc) => doc.id === selectedDoc)) byId("chunk-document").value = selectedDoc;
    const selectedUpload = byId("upload-target").value;
    const uploadPlaceholder = context.node("option", "请选择要更新的资料"); uploadPlaceholder.value = "";
    byId("upload-target").replaceChildren(uploadPlaceholder);
    for (const doc of context.getDocuments()) {
      const option = context.node("option", doc.title + " · v" + doc.version + " · " + doc.id.slice(-6));
      option.value = doc.id; option.dataset.version = String(doc.version);
      byId("upload-target").append(option);
    }
    if (context.getDocuments().some((doc) => doc.id === selectedUpload)) {
      byId("upload-target").value = selectedUpload;
    } else {
      // 所选版本已更新或撤下，清理旧预览；迟到读取也不能重新显示它。
      uploadGeneration += 1;
      byId("upload-target").value = "";
      byId("upload-preview").textContent = "";
    }
  }

  function wikiCard(page, index) {
    const card = context.node("article", undefined, "wiki-card");
    card.dataset.status = page.stale ? "stale" : page.status;
    card.style.setProperty("--index", Math.min(index, 8));
    const top = context.node("div", undefined, "card-top");
    top.append(icon("book"), context.node("span", context.statusText(page.stale ? "stale" : page.status), "badge"));
    const title = context.node("button", page.title, "title-button");
    title.onclick = context.safe(() => context.openWiki(page.id));
    const heading = context.node("h2"); heading.append(title);
    const description = page.claims.length ? page.claims[0].text : "打开页面查看结论及原文引用。";
    const foot = context.node("div", undefined, "card-footer");
    foot.append(context.node("span", page.source_ids.length + " 个原文引用 · " + (page.mode === "sample" ? "虚构演示" : page.mode === "offline" ? "离线摘录" : page.mode === "manual" ? "人工修订" : "模型草稿")),
      context.node("span", "v" + page.version, "version"));
    card.append(top, heading, context.node("p", description, "card-description"), foot);
    return card;
  }

  function renderWiki() {
    const visible = context.getPages().filter((page) => !wikiStatus || (wikiStatus === "stale" ? page.stale : page.status === wikiStatus && !page.stale));
    byId("wiki-list").replaceChildren(...visible.map(wikiCard));
    if (!visible.length) byId("wiki-list").append(empty("这里还没有" + (wikiStatus ? context.statusText(wikiStatus) : "") + "知识页面", "选择来源资料整理草稿，或载入演示空间。", "book"));
  }

  function renderRecent() {
    const docs = context.getDocuments().slice(0, 4);
    const rows = docs.map((doc) => {
      const button = context.node("button", undefined, "recent-row");
      const description = context.node("div");
      description.append(context.node("strong", doc.title), context.node("small", metadata(doc).category + " · " + doc.chunk_ids.length + " 个片段"));
      button.append(fileTile(), description, context.node("span", "v" + doc.version));
      button.onclick = context.safe(() => context.openDoc(doc.id));
      return button;
    });
    byId("recent-documents").replaceChildren(...rows);
    if (!rows.length) byId("recent-documents").append(empty("等待第一份资料", "进入原始资料，添加自己的内容或载入演示空间。"));
  }

  function svgNode(tag, attributes, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function graphPoint(doc, x, y, wiki = false) {
    // 展示标题省略共同的虚构公司前缀；无障碍名称仍保留完整原始标题。
    const titleCharacters = Array.from(doc.title.replace(/^星舟(?:商城)?/, ""));
    const group = svgNode("g", { class: "graph-node" + (wiki ? " wiki-node" : ""), transform: "translate(" + x + " " + y + ")", tabindex: 0, role: "button", "aria-label": "打开" + (wiki ? "Wiki：" : "资料：") + doc.title });
    group.append(svgNode("title", {}, doc.title), svgNode("circle", { class: "node-ring", r: wiki ? 22 : 18 }),
      svgNode("text", { class: "node-glyph", y: 6 }, wiki ? "W" : "文"),
      svgNode("text", { y: wiki ? 39 : 34 }, titleCharacters.slice(0, 8).join("") + (titleCharacters.length > 8 ? "…" : "")));
    const activate = context.safe(() => wiki ? context.openWiki(doc.id) : context.openDoc(doc.id));
    group.onclick = activate;
    group.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") activate(event); };
    return group;
  }

  function renderGraph() {
    const svg = byId("knowledge-map");
    svg.replaceChildren();
    svg.append(svgNode("ellipse", { cx: 280, cy: 163, rx: 210, ry: 128, class: "graph-track" }),
      svgNode("ellipse", { cx: 280, cy: 163, rx: 118, ry: 82, class: "graph-ring" }));
    const docs = context.getDocuments().slice(0, 8);
    const pages = context.getPages().filter((page) => page.status === "published" && !page.stale).slice(0, 4);
    const positions = [[93,92],[218,38],[350,42],[468,99],[479,232],[350,280],[200,282],[73,220]];
    const wikiPositions = [[169,141],[389,140],[210,216],[349,218]];
    const documentPoints = new Map();
    docs.forEach((doc, index) => doc.chunk_ids.forEach((id) => documentPoints.set(id, positions[index])));
    // 只有 Wiki 真正引用的来源片段才产生引用连线；不是推测的语义相似度。
    pages.forEach((page, index) => {
      const [x, y] = wikiPositions[index];
      const unique = new Set();
      for (const id of page.source_ids) {
        const point = documentPoints.get(id);
        if (!point || unique.has(point)) continue;
        unique.add(point);
        const path = "M" + point[0] + "," + point[1] + " Q280,163 " + x + "," + y;
        svg.append(svgNode("path", { d: path, class: "graph-edge" }), svgNode("path", { d: path, class: "graph-flow", style: "animation-delay:-" + index * 1.7 + "s" }));
      }
    });
    svg.append(svgNode("circle", { cx: 280, cy: 163, r: 51, class: "graph-halo" }),
      svgNode("circle", { cx: 280, cy: 163, r: 40, class: "graph-center" }),
      svgNode("text", { x: 280, y: 161, class: "graph-center-label" }, "知证"),
      svgNode("text", { x: 280, y: 179, class: "graph-center-sub" }, "KNOWLEDGE"));
    docs.forEach((doc, index) => svg.append(graphPoint(doc, ...positions[index])));
    pages.forEach((page, index) => svg.append(graphPoint(page, ...wikiPositions[index], true)));
    if (!docs.length) svg.append(svgNode("text", { x: 280, y: 263, class: "graph-empty" }, "加入资料，点亮你的知识网络"));
    byId("graph-caption").textContent = docs.length ? "显示 " + docs.length + "/" + context.getDocuments().length + " 份资料 · 连线为原文引用" : "等待加入原始资料";
    if (window.ZhiSpatial) window.ZhiSpatial.update(context.getDocuments(), context.getPages(), byId("graph-caption").textContent);
  }

  function renderOverview() {
    const docs = context.getDocuments(), pages = context.getPages();
    const counts = { "count-docs": docs.length, "count-chunks": docs.reduce((sum, doc) => sum + doc.chunk_ids.length, 0),
      "count-published": pages.filter((page) => page.status === "published" && !page.stale).length,
      "count-stale": pages.filter((page) => page.stale && page.status !== "archived").length };
    for (const [id, value] of Object.entries(counts)) window.ZhiEffects.count(byId(id), value);
    byId("nav-doc-count").textContent = String(docs.length);
    byId("nav-wiki-count").textContent = String(pages.length);
    renderRecent(); renderGraph();
    if (window.ZhiExperience) window.ZhiExperience.update();
  }

  function renderChunks(result) {
    byId("chunk-summary").textContent = result.chunk_count + " 片段 · " + result.duplicate_chars + " 重复字符";
    byId("chunk-map").replaceChildren();
    byId("chunk-list").replaceChildren();
    result.chunks.forEach((chunk, index) => {
      const row = context.node("div", undefined, "chunk-bar-row");
      const bar = context.node("div", undefined, "chunk-bar");
      bar.style.setProperty("--start", chunk.start_offset / result.source_length * 100 + "%");
      bar.style.setProperty("--width", (chunk.end_offset - chunk.start_offset) / result.source_length * 100 + "%");
      bar.style.setProperty("--index", Math.min(index, 10));
      row.title = "片段 " + (index + 1) + "：[" + chunk.start_offset + ", " + chunk.end_offset + ")";
      row.append(bar); byId("chunk-map").append(row);
      const detail = context.node("details", undefined, "chunk-item"); detail.open = index === 0;
      detail.append(context.node("summary", "#" + (index + 1) + " · [" + chunk.start_offset + ", " + chunk.end_offset + ") · " + (chunk.end_offset - chunk.start_offset) + " 字符"),
        context.node("div", chunk.text, "prewrap"));
      if (chunk.heading_path && chunk.heading_path.length) detail.append(context.node("p", "章节：" + chunk.heading_path.join(" / "), "hint"));
      byId("chunk-list").append(detail);
    });
  }

  async function previewChunks() {
    const generation = ++chunkGeneration;
    if (Array.from(byId("chunk-text").value).length > 20000) throw new Error("预览文本最多 20,000 个 Unicode 字符，请缩短后重试。");
    try {
      const result = await context.api("/api/chunk-preview", { text: byId("chunk-text").value,
        size: Number(byId("chunk-size").value), overlap: Number(byId("chunk-overlap").value), strategy: byId("chunk-strategy").value });
      if (generation === chunkGeneration) renderChunks(result);
    } catch (error) {
      if (generation === chunkGeneration) throw error;
    }
  }

  function invalidatePreview() {
    chunkGeneration += 1;
    byId("chunk-summary").textContent = "输入已变更，请重新预览";
    byId("chunk-map").replaceChildren();
    byId("chunk-list").replaceChildren(empty("等待重新预览", "当前参数尚未执行，点击左侧按钮查看结果。", "nodes"));
  }

  function syncRanges() {
    invalidatePreview();
    const size = Number(byId("chunk-size").value);
    byId("chunk-overlap").max = String(Math.min(500, size / 2));
    byId("chunk-overlap").value = String(Math.min(Number(byId("chunk-overlap").value), size / 2));
    byId("chunk-size-value").textContent = size + " 字符";
    byId("chunk-overlap-value").textContent = byId("chunk-overlap").value + " 字符";
  }

  async function chooseChunkDocument() {
    const generation = ++documentGeneration;
    invalidatePreview();
    const id = byId("chunk-document").value;
    if (!id) return;
    const doc = await context.api("/api/documents/" + id);
    if (generation !== documentGeneration) return;
    // 与 Python 一样按 Unicode 码点截断，避免在 emoji 的代理对中间截断。
    const characters = Array.from(doc.text);
    byId("chunk-text").value = characters.slice(0, 20000).join("");
    if (characters.length > 20000) context.notify("切分实验仅载入前 20,000 字符，原始资料未改动。");
    await previewChunks();
  }

  function bindPanels() {
    for (const [button, panel, close] of [["toggle-import", "import-panel", "close-import"], ["toggle-wiki-editor", "wiki-editor", "close-wiki-editor"]]) {
      byId(button).setAttribute("aria-expanded", "false");
      byId(button).setAttribute("aria-controls", panel);
      byId(button).onclick = () => {
        byId(panel).hidden = !byId(panel).hidden;
        byId(button).setAttribute("aria-expanded", String(!byId(panel).hidden));
        if (!byId(panel).hidden) byId(panel).querySelector("input").focus();
      };
      byId(close).onclick = () => { byId(panel).hidden = true; byId(button).setAttribute("aria-expanded", "false"); byId(button).focus(); };
    }
  }

  async function refreshIndexStatus() {
    const stats = await context.api("/api/system/retrieval");
    byId("index-status").replaceChildren();
    const metrics = [["有效索引片段", stats.items], ["缓存复用", stats.hits], ["索引构建", stats.builds],
      ["与资料版本一致", stats.current ? "是" : stats.ready ? "待刷新" : "尚未检索"]];
    for (const [label, value] of metrics) {
      const block = context.node("div");
      block.append(context.node("strong", value), context.node("span", label));
      byId("index-status").append(block);
    }
  }

  async function previewUploadTarget() {
    const epoch = ++uploadGeneration;
    const id = byId("upload-target").value;
    byId("upload-preview").textContent = "";
    if (!id) return;
    const doc = await context.api("/api/documents/" + id);
    if (epoch !== uploadGeneration || byId("upload-target").value !== id) return;
    byId("upload-preview").textContent = (doc.active ? "当前原文预览：\n" : "该版本已失效，请刷新资料列表。\n") +
      Array.from(doc.text).slice(0, 400).join("");
  }

  async function init(options) {
    context = options;
    if (window.ZhiSpatial) window.ZhiSpatial.init({ openDoc: context.openDoc, openWiki: context.openWiki, safe: context.safe });
    bindPanels();
    byId("refresh-index").onclick = context.safe(refreshIndexStatus);
    byId("upload-mode").onchange = () => {
      uploadGeneration += 1;
      byId("upload-target-box").hidden = byId("upload-mode").value !== "update";
      byId("upload-target").required = byId("upload-mode").value === "update";
    };
    byId("upload-target").onchange = context.safe(previewUploadTarget);
    document.querySelectorAll("[data-go]").forEach((button) => { button.onclick = () => context.showView(button.dataset.go); });
    document.querySelectorAll("[data-question]").forEach((button) => { button.onclick = () => { context.showView("query"); byId("question").value = button.dataset.question; byId("question").focus(); }; });
    byId("document-search").oninput = renderDocuments;
    byId("document-category").onchange = renderDocuments;
    byId("wiki-filters").querySelectorAll("button").forEach((button) => {
      button.onclick = () => {
        wikiStatus = button.dataset.status;
        byId("wiki-filters").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
        renderWiki();
      };
    });
    byId("chunk-form").onsubmit = context.safe(previewChunks);
    byId("chunk-document").onchange = context.safe(chooseChunkDocument);
    byId("chunk-size").oninput = syncRanges;
    byId("chunk-overlap").oninput = syncRanges;
    byId("chunk-strategy").onchange = invalidatePreview;
    // 编辑原文会使进行中的预览过期，迟到结果不能覆盖下一次实验。
    byId("chunk-text").oninput = () => { invalidatePreview(); documentGeneration += 1; };
    const data = await context.api("/api/examples/catalog");
    catalog = new Map(data.documents.map((item) => [item.source_key, item]));
  }

  return { init, renderDocuments, renderSourcePicker, refreshSelectors, renderWiki, renderOverview, updateBudget };
})();

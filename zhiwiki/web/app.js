/* 浏览器只展示公开证据和执行状态，所有资料内容使用 textContent。 */
"use strict";
const $ = (id) => document.getElementById(id);
let currentDocuments = [];
let currentPages = [];
let controller = null;
let detailGeneration = 0;
let activeWikiId = null;
const workspace = () => typeof window !== "undefined" ? window.ZhiWorkspace : null;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
}

async function api(path, payload, options = {}) {
  const init = { ...options };
  if (payload !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(payload);
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `请求失败（${response.status}），请检查输入。`);
  }
  return response.json();
}

function showView(name) {
  const names = { overview: "工作台", query: "知识调查", documents: "原始资料", wiki: "Wiki 知识", architecture: "架构与切分" };
  if (!names[name]) return;
  for (const button of document.querySelectorAll("nav button[data-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === name));
  }
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `${name}-view`;
  if ($("page-name")) $("page-name").textContent = names[name];
  // 页面切换从标题开始，避免从长答案切到资料页后仍停留在旧滚动位置。
  notify("");
  if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "instant" });
  if (typeof window !== "undefined" && window.ZhiExperience) window.ZhiExperience.setView(name);
}

function beginDetail() {
  detailGeneration += 1;
  activeWikiId = null;
  return detailGeneration;
}

function closeDetail() {
  beginDetail();
  $("detail").close();
}

function showDetail(title, state, body, generation) {
  if (generation !== detailGeneration) return;
  $("detail-title").textContent = title;
  $("detail-state").textContent = state;
  $("detail-body").textContent = body;
  $("detail-actions").replaceChildren();
  if (!$("detail").open) $("detail").showModal();
}

function statusText(value) {
  return { completed: "已完成", insufficient_evidence: "依据不足", budget_exceeded: "已达预算", provider_error: "模型不可用", invalid_answer: "回答未通过校验", draft: "待核对草稿", published: "已发布", stale: "来源已更新", archived: "历史版本" }[value] || value;
}

function renderResult(result) {
  queryPhase("answer", "调查完成");
  if ($("query-idle")) $("query-idle").hidden = true;
  if ($("query-loading")) $("query-loading").hidden = true;
  $("answer-box").hidden = false;
  $("answer").textContent = result.answer;
  if (result.mode === "offline" && window.ZhiExperience) window.ZhiExperience.presentOffline(result, $("answer"));
  $("answer-status").textContent = statusText(result.status);
  const usage = result.usage || {};
  $("result-meta").textContent = `${result.mode === "offline" ? "离线检索摘录" : "真实模型模式"} · ${result.elapsed_ms} ms · 模型 ${usage.model_calls || 0} 次 · 工具 ${usage.tool_calls || 0} 次`;
  $("citations").replaceChildren();
  $("evidence-empty").hidden = result.citations.length > 0;
  if ($("evidence-count")) $("evidence-count").textContent = String(result.citations.length);
  for (const evidence of result.citations) {
    const box = node("div", undefined, "citation");
    box.append(node("strong", evidence.title), node("p", `v${evidence.version} · ${evidence.locator}`));
    const details = node("details");
    details.append(node("summary", "查看引用内容"), node("div", evidence.text, "prewrap"));
    box.append(details);
    // Wiki 是二级来源，直接展开后端已经核验的原文链。
    for (const source of evidence.source_evidence || []) {
      const original = node("details");
      original.append(node("summary", `原文：${source.title} · v${source.version} · ${source.locator}`),
        node("div", source.text, "prewrap"));
      box.append(original);
    }
    $("citations").append(box);
  }
}

function queryPhase(phase, description) {
  if ($("loading-phase")) $("loading-phase").textContent = description;
  document.querySelectorAll("#execution-route [data-phase]").forEach((element) => {
    element.classList.toggle("active", element.dataset.phase === phase);
  });
}

function appendEvent(type, payload) {
  // 展示真实工具事件及可展开参数，不把动画当作模型的推理内容。
  const names = { search: "查找资料", search_documents: "查找原文", search_wiki: "查找 Wiki", read_evidence: "读取原文", read_document: "读取资料", wiki: "查找 Wiki" };
  const toolName = payload.name || payload.tool || "";
  let label = type + " · " + (payload.phase || payload.status || "");
  if (type === "tool") {
    label = (names[toolName] || toolName || "工具调用") + (payload.phase === "start" ? " · 开始" : " · 返回");
    queryPhase(toolName.startsWith("read") ? "evidence" : "search", label);
  } else if (type === "model") {
    label = payload.phase === "request" ? "请求模型 · 组织下一步" : "模型已返回";
    queryPhase("question", label);
  } else if (type === "status") label = payload.phase === "started" ? "调查已开始" : "状态 · " + (payload.phase || "");
  const item = node("li");
  item.append(node("span", label));
  const details = node("details");
  details.append(node("summary", "查看事件数据"), node("pre", JSON.stringify(payload, null, 2)));
  item.append(details);
  $("events").append(item);
}

function receiveEvent(block) {
  let type = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return;
  const payload = JSON.parse(data.join("\n"));
  if (type === "final") {
    renderResult(payload);
    notify(payload.mode === "offline" ? "离线检索已完成。此结果为原文摘录，不代表真实模型推理效果。" : "调查已结束，请结合引用核对结论。");
  } else if (type === "error") {
    notify(payload.message || "查询失败", true);
  } else {
    appendEvent(type, payload);
  }
}

async function ask(event) {
  event.preventDefault();
  controller = new AbortController();
  $("ask").disabled = true;
  $("cancel").hidden = false;
  $("events").replaceChildren();
  $("answer-box").hidden = true;
  $("citations").replaceChildren();
  $("evidence-empty").hidden = false;
  $("evidence-count").textContent = "0";
  $("query-idle").hidden = true;
  $("query-loading").hidden = false;
  queryPhase("question", "开始调查…");
  notify("正在查找资料与核对证据…");
  try {
    const response = await fetch("/api/query/stream", { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal, body: JSON.stringify({ question: $("question").value, strategy: $("strategy").value }) });
    if (!response.ok) {
      const detail = await response.json();
      throw new Error(typeof detail.detail === "string" ? detail.detail : "查询参数有误");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        receiveEvent(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) break;
    }
  } catch (error) {
    notify(error.name === "AbortError" ? "已停止接收，正在进行的模型请求会在超时边界内结束。" : error.message, error.name !== "AbortError");
  } finally {
    controller = null;
    $("ask").disabled = false;
    $("cancel").hidden = true;
    $("query-loading").hidden = true;
    if ($("answer-box").hidden) { $("query-idle").hidden = false; queryPhase("", ""); }
  }
}

async function openDoc(id) {
  const generation = beginDetail();
  const detail = await api(`/api/documents/${id}`);
  showDetail(detail.title, `v${detail.version} · ${detail.source_key} · ${detail.chunks.length} 个片段`, detail.text, generation);
}

async function refreshDocuments() {
  currentDocuments = (await api("/api/documents")).documents;
  if (workspace()) {
    workspace().refreshSelectors(); workspace().renderDocuments(); workspace().renderSourcePicker(); workspace().renderOverview();
    return;
  }
  $("documents-list").replaceChildren();
  $("wiki-sources").replaceChildren();
  if (!currentDocuments.length) $("documents-list").append(node("p", "还没有资料。可以导入文件，或载入虚构样例。", "hint"));
  for (const doc of currentDocuments) {
    const row = node("div", undefined, "item");
    const title = node("button", doc.title, "title-button");
    title.onclick = safe(async () => {
      const generation = beginDetail();
      const detail = await api(`/api/documents/${doc.id}`);
      showDetail(detail.title, `v${detail.version} · ${detail.source_key}`, detail.text, generation);
    });
    row.append(title, node("p", `v${doc.version} · ${doc.source_key}`, "hint"));
    $("documents-list").append(row);
    const label = node("label");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.value = doc.id;
    label.append(checkbox, node("span", doc.title));
    $("wiki-sources").append(label);
  }
}

async function openWiki(id) {
  // 每次详情请求都有独立代次；迟到响应不得改变其他页面或发布按钮。
  const generation = beginDetail();
  const page = await api(`/api/wiki/${id}`);
  if (generation !== detailGeneration) return;
  const content = page.claims.map((claim) => `${claim.text}\n来源：${claim.evidence_ids.join(", ")}`).join("\n\n");
  showDetail(page.title, `v${page.version} · ${statusText(page.status)}${page.stale ? " · 需重新核对来源" : ""} · ${page.mode}`, content, generation);
  if (page.revision_note) $("detail-body").append(node("p", "修订说明：" + page.revision_note, "hint"));
  activeWikiId = id;
  // 展示原始片段，用户可在发布前核对每条结论。
  for (const sourceId of page.source_ids) {
    const evidence = await api(`/api/evidence/${sourceId}`);
    if (generation !== detailGeneration || !$("detail").open) return;
    const block = node("details");
    block.append(node("summary", `${evidence.title} · v${evidence.version} · ${evidence.locator}`), node("div", evidence.text, "prewrap"));
    $("detail-body").append(block);
  }
  const exportLink = node("a", "导出 Markdown");
  exportLink.href = `/api/wiki/${id}/export`;
  $("detail-actions").append(exportLink);
  if (typeof window !== "undefined" && window.ZhiRevisions) {
    const edit = node("button", "人工修订", "secondary");
    edit.onclick = safe(() => window.ZhiRevisions.open(page));
    $("detail-actions").append(edit);
  }
  if (page.status === "draft" && !page.stale) {
    const publish = node("button", "已核对来源，发布此版本");
    publish.onclick = safe(async () => {
      if (generation !== detailGeneration || activeWikiId !== id || !$("detail").open) return;
      await api(`/api/wiki/${id}/publish`, {});
      if (generation === detailGeneration) closeDetail();
      await refreshWiki();
      notify("Wiki 已发布，可参与知识检索。");
    });
    $("detail-actions").append(publish);
  }
}

async function refreshWiki() {
  const { pages } = await api("/api/wiki");
  currentPages = pages;
  if (workspace()) { workspace().renderWiki(); workspace().renderOverview(); return; }
  $("wiki-list").replaceChildren();
  if (!pages.length) $("wiki-list").append(node("p", "选择来源资料，为一个主题生成首份草稿。", "hint"));
  for (const page of pages) {
    const row = node("div", undefined, "item");
    const title = node("button", page.title, "title-button");
    title.onclick = safe(() => openWiki(page.id));
    row.append(title, node("p", `v${page.version} · ${statusText(page.status)}${page.stale ? " · 来源已变化" : ""}`, "hint"));
    $("wiki-list").append(row);
  }
}

function safe(action) {
  return async (event) => {
    if (event) event.preventDefault();
    const button = event && event.currentTarget;
    if (button && button.tagName === "BUTTON") button.disabled = true;
    try { await action(event); } catch (error) { notify(error.message, true); }
    finally { if (button) button.disabled = false; }
  };
}

function bindActions() {
  document.querySelectorAll("nav button[data-view]").forEach((button) => { button.onclick = () => showView(button.dataset.view); });
  $("close-detail").onclick = closeDetail;
  $("detail").addEventListener("cancel", beginDetail);
  $("query-form").onsubmit = ask;
  $("cancel").onclick = () => controller && controller.abort();
  $("seed").onclick = safe(async () => {
    const result = await api("/api/examples", {});
    await Promise.all([refreshDocuments(), refreshWiki()]);
    const conflicts = (result.skipped_pages || []).filter((item) => item.reason !== "unchanged").length;
    notify(`已载入 ${result.documents.length} 份虚构资料、${result.pages.length} 个演示 Wiki。${conflicts ? "部分已有页面或来源发生变化，已保留其当前内容。" : "重复载入不会创建相同版本。"}`);
  });
  $("refresh-documents").onclick = safe(refreshDocuments);
  $("document-form").onsubmit = safe(async () => {
    const result = await api("/api/documents", { title: $("doc-title").value, source_key: $("source-key").value, text: $("doc-text").value });
    await Promise.all([refreshDocuments(), refreshWiki()]);
    notify(result.unchanged ? "内容未变化，保留原版本。" : `资料已保存为 v${result.version}，关联页面状态已更新。`);
  });
  $("upload-form").onsubmit = safe(async () => {
    const submit = $("upload-submit");
    if (submit.disabled) return;
    submit.disabled = true;
    const inputs = [$("upload"), $("upload-title"), $("upload-mode"), $("upload-target")];
    inputs.forEach((input) => { input.disabled = true; });
    try {
      const form = new FormData();
      form.append("file", $("upload").files[0]);
      const parameters = new URLSearchParams({ mode: $("upload-mode").value });
      if ($("upload-title").value.trim()) parameters.set("title", $("upload-title").value.trim());
      if ($("upload-mode").value === "update") {
        const selected = $("upload-target").selectedOptions[0];
        if (!selected || !selected.value) throw new Error("请选择需要更新的资料。");
        // 使用选择时的版本，禁止提交前偷偷换成更新后的目标。
        parameters.set("target_document_id", selected.value);
        parameters.set("expected_version", selected.dataset.version);
      }
      const result = await api("/api/documents/upload?" + parameters, undefined, { method: "POST", body: form });
      // 成功后清掉文件与可选标题，防止下次切换更新目标时误带入上一份名称。
      $("upload").value = "";
      $("upload-title").value = "";
      await Promise.all([refreshDocuments(), refreshWiki()]);
      notify(result.unchanged ? "文件内容未变化，保留当前版本。" : "资料已保存为 v" + result.version + "。新增文件独立保存，更新资料沿用原来源。");
    } finally {
      submit.disabled = false;
      inputs.forEach((input) => { input.disabled = false; });
    }
  });
  $("wiki-form").onsubmit = safe(async () => {
    const document_ids = Array.from(document.querySelectorAll("#wiki-sources input:checked")).map((input) => input.value);
    if (!document_ids.length || document_ids.length > 12) throw new Error("请选择 1 至 12 份来源资料。");
    if (workspace() && workspace().updateBudget().chunks > 20) throw new Error("所选资料超过 20 个片段，请减少来源后生成草稿。");
    const submit = $("wiki-form").querySelector("button");
    submit.disabled = true;
    notify("正在整理草稿…");
    try {
      const page = await api("/api/wiki/draft", { title: $("wiki-title").value, slug: $("wiki-slug").value, document_ids });
      await refreshWiki();
      await openWiki(page.id);
      notify("草稿已生成，请逐条核对结论与原文后发布。");
    } finally { submit.disabled = false; }
  });
  $("lint").onclick = safe(async () => {
    const { issues } = await api("/api/wiki/lint");
    $("lint-results").textContent = issues.length ? issues.map((item) => `${item.code}：${item.message}`).join("\n") : "当前没有发现来源或页面状态问题。此检查不判断语义矛盾。";
  });
}

async function start() {
  bindActions();
  if (typeof window !== "undefined" && window.ZhiExperience) window.ZhiExperience.init({
    showView, openDoc, openWiki, safe, notify, getDocuments: () => currentDocuments, getPages: () => currentPages });
  try {
    if (workspace()) await workspace().init({ api, node, safe, showView, openWiki, openDoc, statusText, notify,
      getDocuments: () => currentDocuments, getPages: () => currentPages });
    if (typeof window !== "undefined" && window.ZhiRevisions) window.ZhiRevisions.init({
      api, node, safe, notify, closeDetail, openWiki, refreshWiki, getDocuments: () => currentDocuments });
    const state = await api("/api/health");
    if (window.ZhiExperience) window.ZhiExperience.setMode(state);
    $("mode").textContent = state.mode === "offline" ? "离线检索 · 原文摘录" : `模型已配置 · ${state.model}`;
    $("sidebar-status").textContent = state.mode === "offline" ? "BM25 已就绪 · 离线模式" : "OpenAI 兼容接口已配置";
    await Promise.all([refreshDocuments(), refreshWiki()]);
  } catch (error) { notify(error.message, true); }
}
start();

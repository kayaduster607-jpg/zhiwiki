/* Original archive interaction layer; business mutations remain in app.js. */
"use strict";
window.ZhiExperience = (() => {
  const get = (id) => document.getElementById(id);
  let context, initialized = false, activeView = "overview", restoring = false;
  let frame = 0, previousTime = 0, time = 0, heroHeight = 800;
  let inViewport = true, width = 1, height = 1, canvas, drawing;
  let targetX = 0, targetY = 0, currentX = 0, currentY = 0;
  let resizeObserver, sceneObserver, world, coarse;

  const model = () => window.ZhiArchiveModel;
  const motion = () => window.ZhiEffects ? window.ZhiEffects.isEnabled() : !document.body.classList.contains("motion-off");
  const canAnimate = () => activeView === "overview" && inViewport && !document.hidden && motion();

  function closeMenu(returnFocus = false) {
    const wasOpen = document.body.classList.contains("archive-menu-open");
    document.body.classList.remove("archive-menu-open");
    get("entry-menu").setAttribute("aria-expanded", "false");
    get("entry-menu").setAttribute("aria-label", "打开首页导航");
    if (wasOpen && returnFocus) get("entry-menu").focus();
  }

  function setView(name) {
    if (!initialized || !model().route(name)) return;
    const changed = activeView !== name;
    activeView = name;
    document.body.dataset.view = name;
    closeMenu();
    if (changed && !restoring) window.history.pushState({ zhiView: name }, "", "#" + name);
    get("entry-navigation").querySelectorAll("[data-go]").forEach((button) => {
      button.setAttribute("aria-current", button.dataset.go === name ? "page" : "false");
    });
    if (changed) {
      const heading = get(name + "-view").querySelector("h1");
      if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    }
    syncMotion();
  }

  function restoreRoute() {
    const name = model().route(window.location.hash) || "overview";
    restoring = true;
    context.showView(name);
    restoring = false;
  }

  function enterQuestion(question, submit) {
    // Reuse the existing form validation and request handler; never create a second SSE path.
    context.showView("query");
    if (get("ask").disabled) {
      context.notify("当前调查仍在进行，请等待完成或先停止。");
      return;
    }
    if (question !== undefined) get("question").value = question.trim();
    get("question").focus({ preventScroll: true });
    if (submit && get("question").value) get("query-form").requestSubmit();
  }

  function bindEntry() {
    get("entry-form").onsubmit = (event) => {
      event.preventDefault();
      enterQuestion(get("entry-question").value, true);
    };
    document.querySelectorAll("[data-entry-question]").forEach((button) => {
      button.onclick = () => {
        get("entry-question").value = button.dataset.entryQuestion;
        get("entry-question").focus();
      };
    });
    get("quick-investigate").onclick = () => enterQuestion(undefined, false);
    get("entry-menu").onclick = () => {
      const open = document.body.classList.toggle("archive-menu-open");
      get("entry-menu").setAttribute("aria-expanded", String(open));
      get("entry-menu").setAttribute("aria-label", open ? "关闭首页导航" : "打开首页导航");
    };
    get("explore-archive").onclick = (event) => {
      event.preventDefault();
      get("archive-evidence").scrollIntoView({ behavior: motion() ? "smooth" : "instant", block: "start" });
    };
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { closeMenu(true); return; }
      if (document.querySelector("dialog[open]")) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); enterQuestion(undefined, false);
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && activeView === "query" && !get("ask").disabled) {
        event.preventDefault(); get("query-form").requestSubmit();
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!event.target.closest(".topbar")) closeMenu();
    });
    window.addEventListener("popstate", restoreRoute);
    // Links pasted into the address bar also resolve to the same finite view set.
    window.addEventListener("hashchange", () => {
      const name = model().route(window.location.hash);
      if (name && name !== activeView) restoreRoute();
    });
  }

  function sourceButton(source) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "proof-source";
    const label = document.createElement("span");
    const title = document.createElement("strong"), detail = document.createElement("small");
    title.textContent = source.title;
    detail.textContent = "当前原文 · v" + source.version + " · 点击查看";
    label.append(title, detail); button.append(label);
    button.setAttribute("aria-label", "查看结论原文：" + source.title);
    button.onclick = context.safe(() => context.openDoc(source.id));
    return button;
  }

  function update() {
    if (!initialized) return;
    const proof = model().proof(context.getDocuments(), context.getPages());
    get("proof-sources").replaceChildren();
    get("proof-open").hidden = !proof;
    get("proof-open").onclick = null;
    if (!proof) {
      get("proof-state").textContent = "等待有效知识";
      get("proof-title").textContent = "从原文，开始积累";
      get("proof-claim").textContent = "添加资料并发布一份带引用的 Wiki 后，这里会展示真实结论与支撑它的原文。";
      return;
    }
    get("proof-title").textContent = proof.page.title;
    get("proof-state").textContent = "已发布 · v" + proof.page.version;
    get("proof-claim").textContent = proof.claim.text;
    get("proof-sources").append(...proof.sources.map(sourceButton));
    get("proof-open").onclick = context.safe(() => context.openWiki(proof.page.id));
  }

  function setMode(state) {
    get("entry-mode").textContent = state.mode === "offline" ?
      "当前为离线检索 · 返回原文摘录，不调用模型" : "模型接口已配置 · 回答附带来源引用";
  }

  function presentOffline(result, target) {
    // The display stays verbatim: group the retrieved text instead of inventing a synthesis.
    target.replaceChildren();
    const intro = document.createElement("p");
    intro.className = "excerpt-intro";
    intro.textContent = result.citations.length ?
      "以下为检索到的原文。展开资料阅读，或在右侧核对版本与引用；尚未由模型综合回答。" : result.answer;
    target.append(intro);
    for (const [index, evidence] of result.citations.entries()) {
      const details = document.createElement("details"); details.className = "answer-excerpt";
      details.open = index === 0;
      const summary = document.createElement("summary");
      summary.textContent = evidence.title;
      const text = document.createElement("div"); text.className = "prewrap";
      text.textContent = evidence.text;
      details.append(summary, text); target.append(details);
    }
    if (result.citations.length) {
      const original = document.createElement("details"); original.className = "raw-answer";
      const summary = document.createElement("summary"); summary.textContent = "查看完整离线输出";
      const text = document.createElement("div"); text.className = "prewrap"; text.textContent = result.answer;
      original.append(summary, text); target.append(original);
    }
  }

  function stopFrame() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0; previousTime = 0;
  }

  function syncMotion() {
    stopFrame();
    if (!motion()) {
      currentX = currentY = targetX = targetY = 0;
      ["--scene-x", "--scene-y", "--scene-scale", "--plane-x", "--plane-y", "--word-shift"].forEach((key) => world.style.removeProperty(key));
      if (drawing) drawing.clearRect(0, 0, width, height);
    }
    if (canAnimate()) frame = requestAnimationFrame(tick);
  }

  function resize() {
    const bounds = document.querySelector(".archive-hero").getBoundingClientRect();
    width = Math.max(1, bounds.width); height = heroHeight = Math.max(1, bounds.height);
    if (canvas && drawing) {
      const ratio = Math.min(1.25, window.devicePixelRatio || 1, Math.sqrt(900000 / (width * height)));
      canvas.width = Math.max(1, Math.floor(width * ratio));
      canvas.height = Math.max(1, Math.floor(height * ratio));
      drawing.setTransform(ratio, 0, 0, ratio, 0, 0);
    }
    if (window.innerWidth > 600) closeMenu();
    syncMotion();
  }

  function drawDust() {
    if (!drawing) return;
    drawing.clearRect(0, 0, width, height);
    drawing.fillStyle = "#f5d9b0";
    const count = coarse.matches ? 18 : 36;
    for (let index = 0; index < count; index++) {
      const x = ((index * .6180339 + time * .0013) % 1) * width;
      const y = ((index * .4142135 - time * (.003 + index % 4 * .0003)) % 1 + 1) % 1 * height;
      drawing.globalAlpha = .12 + .2 * (Math.sin(index + time * .5) + 1) / 2;
      drawing.beginPath(); drawing.arc(x, y, index % 3 === 0 ? 1.25 : .65, 0, Math.PI * 2); drawing.fill();
    }
    drawing.globalAlpha = 1;
  }

  function tick(now) {
    frame = 0;
    if (!canAnimate()) return;
    const delta = previousTime ? Math.min(.05, (now - previousTime) / 1000) : 0;
    previousTime = now; time += delta;
    const smoothing = 1 - Math.exp(-delta * 6);
    currentX += (targetX - currentX) * smoothing;
    currentY += (targetY - currentY) * smoothing;
    const progress = model().clamp(window.scrollY / heroHeight, 0, 1);
    world.style.setProperty("--scene-x", (currentX * -12).toFixed(2) + "px");
    world.style.setProperty("--scene-y", (currentY * -8 + progress * 55).toFixed(2) + "px");
    world.style.setProperty("--scene-scale", String(1 + progress * .13));
    world.style.setProperty("--plane-x", (currentY * -6).toFixed(2) + "deg");
    world.style.setProperty("--plane-y", (currentX * 9 - progress * 20).toFixed(2) + "deg");
    world.style.setProperty("--word-shift", (progress * 75).toFixed(2) + "px");
    drawDust();
    frame = requestAnimationFrame(tick);
  }

  function bindScene() {
    world = document.querySelector(".archive-world");
    canvas = get("archive-dust");
    try { drawing = canvas.getContext("2d", { alpha: true }); } catch (_) { drawing = null; }
    coarse = window.matchMedia("(pointer:coarse)");
    const hero = document.querySelector(".archive-hero");
    hero.addEventListener("pointermove", (event) => {
      if (coarse.matches || !motion()) return;
      targetX = model().clamp(event.clientX / width * 2 - 1, -1, 1);
      targetY = model().clamp(event.clientY / heroHeight * 2 - 1, -1, 1);
    });
    hero.addEventListener("pointerleave", () => { targetX = targetY = 0; });
    window.addEventListener("scroll", () => document.body.classList.toggle("archive-scrolled", window.scrollY > 45), { passive: true });
    document.addEventListener("zhi:motion", syncMotion);
    document.addEventListener("visibilitychange", syncMotion);
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(resize); resizeObserver.observe(hero);
    } else window.addEventListener("resize", resize);
    if (typeof IntersectionObserver !== "undefined") {
      sceneObserver = new IntersectionObserver((entries) => {
        inViewport = entries[0].isIntersecting;
        document.body.classList.toggle("archive-offscreen", !inViewport);
        syncMotion();
      });
      sceneObserver.observe(hero);
    }
    resize();
  }

  function init(options) {
    if (initialized || !model()) return;
    context = options; initialized = true;
    bindEntry(); bindScene();
    const name = model().route(window.location.hash) || "overview";
    window.history.replaceState({ zhiView: name }, "", "#" + name);
    restoring = true; context.showView(name); restoring = false;
  }

  return { init, setView, update, setMode, presentOffline };
})();

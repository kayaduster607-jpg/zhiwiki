/* 将真实来源投影到 3D 展示层；不发查询、不改变任何知识数据。 */
"use strict";
window.ZhiSpatial = (() => {
  const get = (id) => document.getElementById(id);
  const initial = { yaw: -.16, pitch: .13 };
  let context, renderer, model, resizeObserver, intersectionObserver;
  let width = 0, height = 0, frame = 0, lastTime = 0, elapsed = 0;
  let yaw = initial.yaw, pitch = initial.pitch;
  let visible = true, intersecting = true, planar = false, failed = false;
  let paused = false, focused = false, hovered = false, drag = null;
  let selectedId = null, fallbackCaption = "", initialized = false;
  const buttons = new Map(), paths = new Map();

  function motionEnabled() {
    return window.ZhiEffects ? window.ZhiEffects.isEnabled() : !document.body.classList.contains("motion-off");
  }

  function canDraw() {
    return renderer && !planar && visible && intersecting && !document.hidden && width > 0 && height > 0;
  }

  function canAnimate() {
    return canDraw() && motionEnabled() && !paused && !focused && !hovered && !drag;
  }

  function stopFrame() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0; lastTime = 0;
  }

  function syncLoop() {
    stopFrame();
    draw();
    if (canAnimate()) frame = requestAnimationFrame(tick);
    updateControls();
  }

  function tick(now) {
    frame = 0;
    if (!canAnimate()) { lastTime = 0; return; }
    const delta = lastTime ? Math.min((now - lastTime) / 1000, .05) : 0;
    lastTime = now;
    // 有界绕行保留可读的节点间距；完整旋转由用户拖动控制。
    yaw += (Math.sin((elapsed + delta) * .24) - Math.sin(elapsed * .24)) * .3;
    elapsed += delta;
    draw();
    if (canAnimate()) frame = requestAnimationFrame(tick);
  }

  function viewOptions() { return { width, height, yaw, pitch }; }

  function positionedNodes() {
    if (!model) return [];
    // 主场景聚焦四份原文和两页 Wiki，并如实展示计数；列表仍包含全部资料。
    const compact = width < 500;
    const allowed = new Set([
      ...model.nodes.filter((item) => item.kind === "document").slice(0, 4),
      ...model.nodes.filter((item) => item.kind === "wiki").slice(0, 2),
    ].map((item) => item.id));
    const factor = compact ? Math.min(1, width / 490) : 1;
    const slots = {
      document: [[-1.75,.9,.18],[1.75,.9,-.18],[-1.75,-.9,-.18],[1.75,-.9,.18]],
      wiki: [[0,1.91,.12],[0,-1.91,-.12]],
    };
    const indices = { document: 0, wiki: 0 };
    return model.nodes.map((item) => {
      const index = indices[item.kind]++;
      const point = [...(slots[item.kind][index] || item.point)];
      if (compact && item.kind === "document") point[1] *= .78;
      return { item, shown: allowed.has(item.id),
        position: window.ZhiSpatialModel.project(point.map((value) => value * factor), viewOptions()) };
    });
  }

  function placeNodes(points) {
    const compact = width < 500;
    for (const { item, position, shown } of points) {
      const button = buttons.get(item.id);
      button.hidden = !shown || !position.visible;
      if (button.hidden) continue;
      const scale = Math.max(.72, Math.min(1.06, 6 / position.depth)) * (compact ? .88 : 1);
      // 只更新合成层；不逐帧读取节点大小、不改变排版宽高。
      button.style.transform = `translate(${position.x}px,${position.y}px) translate(-50%,-50%) scale(${scale})`;
      button.style.opacity = String(Math.max(.88, Math.min(1, 1.22 - position.depth / 20)));
      button.style.zIndex = String(Math.round((12 - position.depth) * 10));
      button.dataset.depth = position.depth.toFixed(3);
    }
  }

  function placeEdges(points) {
    const projected = new Map(points.filter((item) => item.shown && item.position.visible).map((item) => [item.item.id, item.position]));
    for (const edge of model.edges) {
      const path = paths.get(edge.from + "|" + edge.to);
      const from = projected.get(edge.from), to = projected.get(edge.to);
      path.style.display = from && to ? "" : "none";
      if (!from || !to) continue;
      const centerX = (from.x + to.x) / 2;
      const centerY = (from.y + to.y) / 2 - 15;
      path.setAttribute("d", `M${from.x},${from.y} Q${centerX},${centerY} ${to.x},${to.y}`);
      path.classList.toggle("is-related", edge.from === selectedId || edge.to === selectedId);
    }
  }

  function draw() {
    if (!canDraw()) return;
    try {
      renderer.draw({ ...viewOptions(), time: elapsed });
      if (model) {
        const points = positionedNodes();
        placeNodes(points); placeEdges(points);
      }
    } catch (error) { fallback("立体显示暂不可用，已切换到平面来源图。"); }
  }

  function describeScene() {
    if (!model || planar) { get("graph-caption").textContent = fallbackCaption; return; }
    const shown = positionedNodes().filter((node) => node.shown).length;
    get("graph-caption").textContent = model.total ?
      `展示 ${shown}/${model.total} 个知识节点 · 连线为原文引用` : "加入第一份资料，建立你的知识空间";
  }

  function updateControls() {
    get("knowledge-scene").dataset.renderer = planar ? "planar" : "webgl";
    get("scene-mode").textContent = planar ? "立体视图" : "平面视图";
    get("scene-mode").setAttribute("aria-pressed", String(planar));
    get("scene-mode").disabled = failed;
    get("scene-pause").textContent = paused ? "继续旋转" : "暂停旋转";
    get("scene-pause").setAttribute("aria-pressed", String(paused));
    get("scene-pause").disabled = !motionEnabled();
    get("scene-instructions").textContent = planar ? "点击节点，查看原文与知识" :
      motionEnabled() ? "拖动旋转 · 点击节点探索" : "动效已关闭 · 仍可拖动查看";
    get("knowledge-canvas").tabIndex = planar ? -1 : 0;
    describeScene();
  }

  function fallback(message) {
    stopFrame();
    if (renderer) renderer.dispose();
    renderer = null; planar = true; failed = true;
    updateControls();
    get("scene-status").textContent = message;
  }

  function createRenderer() {
    try {
      renderer = window.ZhiOrbRenderer.create(get("knowledge-canvas"));
      failed = false; planar = false;
      get("scene-status").textContent = "立体知识空间已就绪。";
      syncLoop();
    } catch (error) { fallback("当前浏览器使用平面来源图，所有资料仍可查看。"); }
  }

  function resize() {
    const bounds = get("spatial-surface").getBoundingClientRect();
    width = Math.round(bounds.width); height = Math.round(bounds.height);
    get("spatial-links").setAttribute("viewBox", `0 0 ${Math.max(width, 1)} ${Math.max(height, 1)}`);
    syncLoop();
  }

  function setVisible(value) {
    visible = Boolean(value);
    if (initialized) resize();
  }

  function setHighlight(id) {
    selectedId = id;
    const related = new Set([id]);
    if (model && id) model.edges.forEach((edge) => {
      if (edge.from === id || edge.to === id) { related.add(edge.from); related.add(edge.to); }
    });
    buttons.forEach((button, key) => button.classList.toggle("is-related", related.has(key)));
    syncLoop();
  }

  function makeNode(item) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "spatial-node"; button.dataset.kind = item.kind;
    button.setAttribute("aria-label", "打开" + (item.kind === "wiki" ? "Wiki：" : "资料：") + item.title);
    button.title = item.title;
    const symbol = document.createElement("span"); symbol.className = "node-symbol";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(svg.namespaceURI, "use");
    use.setAttribute("href", item.kind === "wiki" ? "#i-book" : "#i-file"); svg.append(use); symbol.append(svg);
    const copy = document.createElement("span"), title = document.createElement("strong"), meta = document.createElement("small");
    title.textContent = item.title.replace(/^星舟(?:商城)?/, "");
    meta.textContent = (item.kind === "wiki" ? "Wiki" : "原始资料") + " · v" + item.version;
    copy.append(title, meta); button.append(symbol, copy);
    button.onclick = context.safe(() => item.kind === "wiki" ? context.openWiki(item.id) : context.openDoc(item.id));
    button.onpointerenter = () => { hovered = true; setHighlight(item.id); };
    button.onpointerleave = () => { hovered = false; if (!focused) setHighlight(null); else syncLoop(); };
    button.onfocus = () => { focused = true; setHighlight(item.id); };
    button.onblur = () => { focused = false; if (!hovered) setHighlight(null); else syncLoop(); };
    return button;
  }

  function update(documents, pages, caption) {
    if (!initialized) return;
    fallbackCaption = caption;
    // 图形脚本加载失败不能阻断工作台数据刷新，继续保留现有平面来源图。
    if (!window.ZhiSpatialModel) { describeScene(); return; }
    model = window.ZhiSpatialModel.build(documents, pages);
    get("spatial-nodes").replaceChildren(); get("spatial-links").replaceChildren();
    buttons.clear(); paths.clear(); selectedId = null;
    focused = document.activeElement === get("knowledge-canvas"); hovered = false;
    for (const item of model.nodes) {
      const button = makeNode(item); buttons.set(item.id, button); get("spatial-nodes").append(button);
    }
    for (const edge of model.edges) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "spatial-link"); paths.set(edge.from + "|" + edge.to, path);
      get("spatial-links").append(path);
    }
    syncLoop();
  }

  function reset() {
    yaw = initial.yaw; pitch = initial.pitch; elapsed = 0;
    get("scene-status").textContent = "视角已复位。";
    syncLoop();
  }

  function bindPointer() {
    const canvas = get("knowledge-canvas");
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || planar) return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, yaw, pitch };
      canvas.setPointerCapture(event.pointerId); stopFrame();
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      yaw = drag.yaw + (event.clientX - drag.x) * .007;
      pitch = Math.max(-.65, Math.min(.65, drag.pitch + (event.clientY - drag.y) * .004));
      draw();
    });
    const release = () => { drag = null; syncLoop(); };
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("lostpointercapture", release);
    canvas.addEventListener("keydown", (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Home") { reset(); return; }
      yaw += event.key === "ArrowLeft" ? -.12 : event.key === "ArrowRight" ? .12 : 0;
      pitch = Math.max(-.65, Math.min(.65, pitch + (event.key === "ArrowUp" ? -.08 : event.key === "ArrowDown" ? .08 : 0)));
      draw();
    });
    canvas.addEventListener("focus", () => { focused = true; syncLoop(); });
    canvas.addEventListener("blur", () => { focused = false; syncLoop(); });
    canvas.addEventListener("webglcontextlost", (event) => {
      event.preventDefault(); fallback("立体显示已暂停，当前使用平面来源图。");
    });
    canvas.addEventListener("webglcontextrestored", createRenderer);
  }

  function init(options) {
    if (initialized) return;
    context = options; initialized = true;
    if (!window.ZhiSpatialModel || !window.ZhiOrbRenderer) { fallback("当前使用平面来源图。"); return; }
    get("scene-pause").onclick = () => { paused = !paused; syncLoop(); };
    get("scene-reset").onclick = reset;
    get("scene-mode").onclick = () => { planar = !planar; syncLoop(); };
    document.addEventListener("zhi:motion", syncLoop);
    document.addEventListener("visibilitychange", syncLoop);
    bindPointer();
    resizeObserver = new ResizeObserver(resize); resizeObserver.observe(get("spatial-surface"));
    intersectionObserver = new IntersectionObserver((entries) => {
      intersecting = entries[0].isIntersecting; syncLoop();
    }, { threshold: .08 });
    intersectionObserver.observe(get("knowledge-scene"));
    createRenderer(); resize();
  }

  return { init, update, setVisible };
})();

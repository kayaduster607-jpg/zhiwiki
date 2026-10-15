/* 动效独立于业务状态：系统偏好、手动开关和页面可见性共同控制。 */
"use strict";
(() => {
  const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
  const counters = new Map();
  let enabled = !preference.matches;

  function applyPreference() {
    document.body.classList.toggle("motion-off", !enabled);
    document.body.classList.toggle("motion-on", enabled);
    const button = document.getElementById("motion-toggle");
    button.setAttribute("aria-pressed", String(enabled));
    button.title = enabled ? "关闭动画" : "开启动画";
    button.setAttribute("aria-label", button.title);
    document.dispatchEvent(new CustomEvent("zhi:motion", { detail: { enabled } }));
  }

  function count(element, value) {
    if (!element) return;
    if (counters.has(element)) cancelAnimationFrame(counters.get(element));
    const start = Number(element.textContent) || 0;
    if (!enabled || document.hidden || start === value) {
      element.textContent = String(value);
      return;
    }
    const began = performance.now();
    function step(now) {
      const fraction = Math.min(1, (now - began) / 600);
      element.textContent = String(Math.round(start + (value - start) * (1 - Math.pow(1 - fraction, 3))));
      if (fraction < 1 && enabled && !document.hidden) counters.set(element, requestAnimationFrame(step));
      else { element.textContent = String(value); counters.delete(element); }
    }
    counters.set(element, requestAnimationFrame(step));
  }

  function visibility() {
    document.body.classList.toggle("page-hidden", document.hidden);
  }

  document.getElementById("motion-toggle").onclick = () => { enabled = !enabled; applyPreference(); };
  preference.addEventListener("change", (event) => { enabled = !event.matches; applyPreference(); });
  document.addEventListener("visibilitychange", visibility);
  // 视口外的几何动画暂停，滚动回来后沿原进度继续。
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) entry.target.classList.toggle("scene-paused", !entry.isIntersecting);
  });
  document.querySelectorAll(".motion-scene,.signal-orbit,.sidebar-orbit,.architecture-flow").forEach((scene) => observer.observe(scene));
  applyPreference();
  visibility();
  window.ZhiEffects = { count, isEnabled: () => enabled };
})();

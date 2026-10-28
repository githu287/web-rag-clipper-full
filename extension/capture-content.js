// Browser-side capture helpers. No network requests or page event activation.
"use strict";
(function () {
  if (globalThis.webRagContentCapture) globalThis.webRagContentCapture.cancel();
  let active = null;

  function cancelled() {
    return new Error("已取消提取");
  }

  function visible(element) {
    if (!element || !element.isConnected) return false;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) return false;
    let node = element;
    while (node && node.nodeType === 1) {
      if (webRagPageExtractor.isElementHidden(node, node.ownerDocument.defaultView)) return false;
      node = node.parentElement;
    }
    return true;
  }

  function describe(mode) {
    const frames = [];
    function visit(root) {
      root.querySelectorAll("iframe,frame").forEach(function (frame) {
        const url = frame.src || "";
        if (!/^https?:/.test(url) || !visible(frame)) return;
        let node = frame;
        let noise = false;
        while (node && node !== document.body) {
          noise = noise || webRagPageExtractor.isNoiseName(`${node.id} ${node.className}`);
          node = node.parentElement;
        }
        if (mode !== "article" || !noise) frames.push(url);
      });
      root.querySelectorAll("*").forEach(function (element) {
        if (element.shadowRoot) visit(element.shadowRoot);
      });
    }
    if (document.body) visit(document.body);
    let frameVisible = true;
    try { if (window.frameElement) frameVisible = visible(window.frameElement); } catch (_error) {}
    return { url: document.URL, title: document.title, frames: Array.from(new Set(frames)), visible: frameVisible };
  }

  function waitForContent(signal) {
    return new Promise(function (resolve, reject) {
      const start = Date.now();
      let lastChange = start;
      const observer = new MutationObserver(function () { lastChange = Date.now(); });
      observer.observe(document.documentElement, { childList: true, characterData: true, subtree: true });
      function finish(error, settled) {
        clearInterval(timer);
        observer.disconnect();
        signal.removeEventListener("abort", onAbort);
        if (error) reject(error);
        else resolve({ wait_ms: Date.now() - start, content_settled: settled });
      }
      function onAbort() { finish(cancelled()); }
      const timer = setInterval(function () {
        const now = Date.now();
        // Script/template source must not make a loading shell look complete.
        const textLength = (document.body && document.body.innerText || "").trim().length;
        const settled = document.readyState !== "loading" && textLength >= 180 &&
          now - start >= 800 && now - lastChange >= 400;
        if (settled || now - start >= 3000) finish(null, settled);
      }, 100);
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    });
  }

  function pickRegion(signal) {
    return new Promise(function (resolve, reject) {
      const host = document.createElement("div");
      host.setAttribute("data-web-rag-picker", "");
      host.style.cssText = "all:initial!important;position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;";
      const shadow = host.attachShadow({ mode: "closed" });
      const border = document.createElement("div");
      border.style.cssText = "position:fixed;border:2px solid #2563eb;background:rgba(37,99,235,.08);box-sizing:border-box;display:none;";
      const tip = document.createElement("div");
      tip.textContent = "点击网页区域进行剪藏 · ↑ 扩大范围 · Esc 取消";
      tip.style.cssText = "position:fixed;top:8px;left:8px;max-width:90vw;padding:8px 12px;border-radius:6px;background:#1e3a8a;color:white;font:14px/1.5 sans-serif;";
      shadow.append(border, tip);
      document.documentElement.appendChild(host);
      let chosen = null;
      function draw() {
        if (!chosen || !visible(chosen)) return;
        const rect = chosen.getBoundingClientRect();
        Object.assign(border.style, { display: "block", left: rect.left + "px", top: rect.top + "px",
          width: rect.width + "px", height: rect.height + "px" });
      }
      function target(event) {
        return event.composedPath().find(function (node) { return node.nodeType === 1 && node !== host; });
      }
      function move(event) {
        const node = target(event);
        if (!node || node.closest("input,textarea,select")) return;
        chosen = node.closest("p,article,main,section,div,table,pre,ul,ol,blockquote,h1,h2,h3,body") || node;
        draw();
      }
      function finish(error) {
        document.removeEventListener("pointermove", move, true);
        document.removeEventListener("click", click, true);
        document.removeEventListener("pointerdown", block, true);
        document.removeEventListener("pointerup", block, true);
        document.removeEventListener("keydown", key, true);
        window.removeEventListener("scroll", draw, true);
        window.removeEventListener("resize", draw);
        signal.removeEventListener("abort", onAbort);
        host.remove();
        if (error) reject(error); else resolve(chosen);
      }
      function click(event) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!chosen) move(event);
        if (!chosen || !visible(chosen)) return;
        finish();
      }
      function block(event) { event.preventDefault(); event.stopImmediatePropagation(); }
      function key(event) {
        if (event.key === "Escape") {
          event.preventDefault(); event.stopImmediatePropagation(); finish(cancelled());
        } else if (event.key === "ArrowUp" && chosen && chosen.parentElement && chosen !== document.body) {
          event.preventDefault(); event.stopImmediatePropagation(); chosen = chosen.parentElement; draw();
        }
      }
      function onAbort() { finish(cancelled()); }
      document.addEventListener("pointermove", move, true);
      document.addEventListener("click", click, true);
      document.addEventListener("pointerdown", block, true);
      document.addEventListener("pointerup", block, true);
      document.addEventListener("keydown", key, true);
      window.addEventListener("scroll", draw, true);
      window.addEventListener("resize", draw);
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    });
  }

  async function capture(options) {
    if (active) active.abort();
    const controller = new AbortController();
    controller.requestId = options && options.requestId;
    active = controller;
    const startUrl = document.URL;
    const mode = options && options.mode || "article";
    // The extension page may close before it can send CANCEL. Keep an
    // independent deadline in the web page so a picker cannot stay forever.
    const deadline = setTimeout(function () { controller.abort(); }, mode === "region" ? 60000 : 12000);
    try {
      if (webRagPageExtractor.hasAccessBarrier(document)) {
        throw new Error("当前页面显示访问限制，请在网页中完成登录或验证、确认正文可见后再提取");
      }
      let waiting = {};
      let root;
      if (mode === "region") root = await pickRegion(controller.signal);
      else if (mode !== "selection") waiting = await waitForContent(controller.signal);
      if (controller.signal.aborted) throw cancelled();
      if (document.URL !== startUrl) throw new Error("页面已变化，请重新提取");
      const result = webRagPageExtractor.extract(document, { mode, root });
      Object.assign(result.diagnostics, waiting);
      return { ok: true, url: document.URL, title: document.title || "", raw_text: result.text, diagnostics: result.diagnostics };
    } finally {
      clearTimeout(deadline);
      if (active === controller) active = null;
    }
  }

  globalThis.webRagContentCapture = {
    capture, describe,
    cancel: function (requestId) { if (active && (!requestId || requestId === active.requestId)) active.abort(); },
  };
})();

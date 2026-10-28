// Extension-page pagination orchestration; original tab/URL remains unchanged.
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPaginationCapture = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function pause(ms, signal) {
    return new Promise(function (resolve, reject) {
      function abort() { clearTimeout(timer); signal.removeEventListener("abort", abort); reject(new Error("已取消提取")); }
      const timer = setTimeout(function () { signal.removeEventListener("abort", abort); resolve(); }, ms);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    });
  }
  function connect(onLost) {
    const port = chrome.runtime.connect({ name: "web-rag-pagination" });
    let sequence = 0, closed = false;
    const pending = new Map();
    port.onMessage.addListener(function (message) {
      if (message.event === "claimed") { onLost(); return; }
      const item = pending.get(message.id);
      if (!item) return;
      pending.delete(message.id); clearTimeout(item.timer);
      if (message.error) item.reject(new Error(message.error)); else item.resolve(message);
    });
    port.onDisconnect.addListener(function () {
      if (!closed) onLost();
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("分页连接已关闭")); }
      pending.clear();
    });
    return {
      call: function (message) {
        return new Promise(function (resolve, reject) {
          const id = ++sequence;
          const timer = setTimeout(function () { pending.delete(id); reject(new Error("分页标签页响应超时")); }, 5000);
          pending.set(id, { resolve, reject, timer });
          try { port.postMessage({ id, ...message }); }
          catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
        });
      },
      close: function () { if (closed) return; closed = true; try { port.disconnect(); } catch (_error) {} },
    };
  }
  async function waitLoaded(tabId, url, signal) {
    const start = Date.now();
    while (Date.now() - start < 12000) {
      if (signal.aborted) throw new Error("已取消提取");
      const tab = await chrome.tabs.get(tabId);
      if (tab.status === "complete") {
        if (webRagPagination.key(tab.url) !== webRagPagination.key(url)) throw new Error("分页发生重定向，已停止合并");
        return;
      }
      await pause(150, signal);
    }
    throw new Error("分页加载超时");
  }
  async function expand(sourceTabId, first, options) {
    const controller = new AbortController();
    let stop = "complete", timedOut = false, lost = false;
    const abort = function () { controller.abort(); };
    options.signal.addEventListener("abort", abort, { once: true });
    if (options.signal.aborted) abort();
    const maxPages = Math.min(5, Math.max(1, Math.floor(Number(options.limits && options.limits.maxPages) || 5)));
    const maxMs = Math.min(60000, Math.max(100, Number(options.limits && options.limits.maxMs) || 60000));
    const timer = setTimeout(function () { timedOut = true; abort(); }, maxMs);
    const pages = [first.url], warnings = new Set(first.diagnostics.quality_warnings || []);
    const images = new Map((first.images || []).map(function (image) { return [image.url, image]; }));
    const fingerprints = new Set([String(first.raw_text || "").replace(/\s+/g, " ")]);
    const visited = new Set([webRagPagination.key(first.url)]);
    let text = first.raw_text || "", previous = first, lease = null;
    async function guard() {
      if (options.signal.aborted) throw new Error("已取消提取");
      if (options.guard) await options.guard();
      const source = await chrome.tabs.get(sourceTabId);
      if (webRagPagination.key(source.url) !== webRagPagination.key(first.url)) throw new Error("页面已变化，请重新提取");
    }
    try {
      while (true) {
        await guard();
        if (timedOut || lost) { stop = timedOut ? "time_limit" : "interrupted"; break; }
        const metadata = previous.pagination || {};
        if (metadata.status !== "ready" || !metadata.next_url) {
          stop = metadata.status === "ambiguous" || metadata.status === "unsupported" ? metadata.status : "complete";
          break;
        }
        const target = webRagPagination.nextURL(previous.url, metadata.next_url);
        if (!target) { stop = "unsupported"; break; }
        if (visited.has(webRagPagination.key(target))) { stop = "loop"; break; }
        if (pages.length >= maxPages) { stop = "page_limit"; break; }
        const firstPrefix = pages.length === 1 ? "分页来源（第 1 页）：" + first.url + "\n\n" : "";
        const nextPrefix = "\n\n---\n\n分页来源（第 " + (pages.length + 1) + " 页）：" + target + "\n\n";
        if (text.length + firstPrefix.length + nextPrefix.length >= 500000) { stop = "size_limit"; break; }
        if (!lease) lease = connect(function () { lost = true; abort(); });
        options.onProgress({ phase: "pagination", current: pages.length + 1, collected: pages.length, max_pages: maxPages });
        let tempId;
        try {
          if (controller.signal.aborted) throw new Error("已取消提取");
          const created = await lease.call({ action: "create", sourceTabId, sourceUrl: first.url, previousUrl: previous.url, url: target });
          tempId = created.tabId;
          if (!Number.isInteger(tempId)) throw new Error("分页标签页创建失败");
          await waitLoaded(tempId, target, controller.signal);
          await guard();
          const next = await webRagPageCapture.capture(tempId, { mode: "article", signal: controller.signal,
            collectImages: !!options.collectImages, collectPagination: true });
          await guard();
          if (controller.signal.aborted) throw new Error("已取消提取");
          if (!webRagPagination.sameArticle(first, next)) { stop = "different_article"; break; }
          const fingerprint = String(next.raw_text || "").replace(/\s+/g, " ");
          const addsImage = (next.images || []).some(function (image) { return !images.has(image.url); });
          if (fingerprints.has(fingerprint) && !addsImage) { stop = "repeated_content"; break; }
          fingerprints.add(fingerprint); visited.add(webRagPagination.key(next.url));
          if (pages.length === 1) text = firstPrefix + text;
          const addition = nextPrefix + next.raw_text;
          if (text.length + addition.length > 500000) { warnings.add("truncated"); stop = "size_limit"; }
          text = (text + addition).slice(0, 500000);
          pages.push(next.url);
          for (const image of next.images || []) if (images.size < 33 && !images.has(image.url)) images.set(image.url, image);
          for (const warning of next.diagnostics.quality_warnings || []) warnings.add(warning);
          previous = next;
          if (stop === "size_limit") break;
        } catch (_error) {
          await guard(); // User cancellation/navigation must reject, not accept partial content.
          stop = timedOut ? "time_limit" : lost ? "interrupted" : "page_failed";
          break;
        } finally {
          if (Number.isInteger(tempId)) await lease.call({ action: "release", tabId: tempId }).catch(function () {});
        }
      }
      await guard();
      if (stop !== "complete") warnings.add("pagination_incomplete");
      if (stop === "size_limit") warnings.add("truncated");
      if (pages.length === 1 && stop === "complete") warnings.add("pagination_none");
      return Object.assign({}, first, { raw_text: text, images: Array.from(images.values()), diagnostics: Object.assign({}, first.diagnostics, {
        pagination_enabled: true, pagination_page_count: pages.length, pagination_stop_reason: stop,
        pagination_urls: pages, extracted_char_count: text.length, truncated: first.diagnostics.truncated || stop === "size_limit",
        quality_warnings: Array.from(warnings),
      }) });
    } finally {
      clearTimeout(timer); options.signal.removeEventListener("abort", abort); controller.abort();
      if (lease) lease.close();
    }
  }
  return { expand };
});

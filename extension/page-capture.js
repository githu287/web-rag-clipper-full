// Extension-side orchestration: bounded waits, explicit frame destinations,
// cancellation and a single size limit across the entire capture.
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPageCapture = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const MAX_CHARS = 500000;
  const MAX_FRAMES = 12;
  const FILES = ["url-utils.js", "vendor/readability/Readability.js", "extractor.js", "snapshot-merge.js", "scroll-capture.js", "capture-content.js", "content.js"];

  function samePage(left, right) {
    if (globalThis.webRagUrlUtils) {
      try { return webRagUrlUtils.normalizeWebUrl(left) === webRagUrlUtils.normalizeWebUrl(right); } catch (_error) {}
    }
    return left === right;
  }

  function message(tabId, frameId, payload, timeout, signal) {
    return new Promise(function (resolve, reject) {
      function finish(error, value) {
        clearTimeout(timer);
        if (signal) signal.removeEventListener("abort", abort);
        if (error) reject(error); else resolve(value);
      }
      function abort() { finish(new Error("已取消提取")); }
      const timer = setTimeout(function () { finish(new Error("页面响应超时，请刷新后重试")); }, timeout);
      if (signal) signal.addEventListener("abort", abort, { once: true });
      if (signal && signal.aborted) { abort(); return; }
      try {
        chrome.tabs.sendMessage(tabId, payload, { frameId }, function (response) {
          const error = chrome.runtime.lastError;
          finish(error ? new Error(error.message) : null, response);
        });
      } catch (error) { finish(error); }
    });
  }

  function merge(results, mode, page) {
    const main = results.find(function (item) { return item.frameId === 0 && item.response && item.response.ok; });
    const ordered = (main ? [main] : []).concat(results.filter(function (item) { return item !== main; }));
    const parts = [];
    const seen = [];
    let length = 0;
    let includedFrames = 0;
    let truncated = false;
    const diagnostics = Object.assign({}, main && main.response.diagnostics || ordered[0] && ordered[0].response.diagnostics || {});
    const warnings = new Set(diagnostics.quality_warnings || []);
    for (const item of ordered) {
      const response = item.response;
      if (!response || !response.ok) continue;
      const text = String(response.raw_text || "").trim();
      if (!text) continue;
      const fingerprint = text.replace(/\s+/g, " ");
      if (seen.some(function (previous) { return previous === fingerprint || (fingerprint.length >= 180 && previous.includes(fingerprint)); })) continue;
      seen.push(fingerprint);
      const prefix = item.frameId !== 0 && parts.length
        ? `\n\n---\n\n内嵌页面来源：${response.url}\n\n` : "";
      const remaining = MAX_CHARS - length;
      const combined = prefix + text;
      if (combined.length > remaining) truncated = true;
      if (remaining > 0) { parts.push(combined.slice(0, remaining)); length += Math.min(combined.length, remaining); }
      if (item.frameId !== 0 && remaining > prefix.length) includedFrames += 1;
      if (response.diagnostics && response.diagnostics.truncated) truncated = true;
      if (response.diagnostics && response.diagnostics.content_settled === false) warnings.add("content_unsettled");
      for (const warning of response.diagnostics && response.diagnostics.quality_warnings || []) warnings.add(warning);
    }
    if (truncated) warnings.add("truncated");
    const rawText = parts.join("").trim();
    if (!rawText) throw new Error(mode === "selection" ? "请先在网页中选中文字，再提取" : "网页正文为空，请等待页面加载或切换剪藏方式");
    Object.assign(diagnostics, { mode, included_frame_count: includedFrames,
      extracted_char_count: rawText.length, truncated, quality_warnings: Array.from(warnings) });
    return { url: page.url, title: page.title || "", raw_text: rawText, diagnostics };
  }

  async function capture(tabId, options) {
    options = options || {};
    const mode = options.mode || "article";
    if (!["article", "page", "selection", "region"].includes(mode)) throw new Error("不支持的剪藏方式");
    const signal = options.signal;
    const autoScroll = !!options.autoScroll && (mode === "article" || mode === "page");
    const requestId = String(Date.now()) + ":" + Math.random().toString(36).slice(2);
    const page = await chrome.tabs.get(tabId);
    if (!page || !/^https?:/.test(page.url || "")) throw new Error("当前标签页不是可访问的网页");
    const injected = [];
    const capturedIds = [];
    let limited = false;
    const progressListener = function (message, sender) {
      if (message && message.type === "WEB_CLIP_PROGRESS" && message.requestId === requestId &&
          sender && sender.tab && sender.tab.id === tabId && sender.frameId === 0 &&
          !(signal && signal.aborted) && typeof options.onProgress === "function") {
        options.onProgress({ steps: Math.max(0, Math.min(20, Number(message.steps) || 0)),
          max_steps: 20, elapsed_ms: Math.max(0, Number(message.elapsed_ms) || 0),
          collected_chars: Math.max(0, Math.min(MAX_CHARS, Number(message.collected_chars) || 0)) });
      }
    };
    if (autoScroll && chrome.runtime.onMessage) chrome.runtime.onMessage.addListener(progressListener);
    try {
      try {
        injected.push(...await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: FILES }));
      } catch (_error) {
        limited = true;
        // A restricted child must not prevent extracting the accessible top page.
        injected.push(...await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] }, files: FILES }));
      }
      if (signal && signal.aborted) throw new Error("已取消提取");
      const ids = Array.from(new Set(injected.map(function (frame) { return frame.frameId; }))).sort(function (a, b) { return a - b; });
      if (ids.length > 64) limited = true;
      const descriptions = await Promise.all(ids.slice(0, 64).map(async function (frameId) {
        try { return { frameId, info: await message(tabId, frameId, { type: "WEB_CLIP_DESCRIBE", mode }, 2000, signal) }; }
        catch (_error) { limited = true; return { frameId, info: null }; }
      }));
      if (signal && signal.aborted) throw new Error("已取消提取");
      const top = descriptions.find(function (frame) { return frame.frameId === 0; });
      if (!top || !top.info) throw new Error("无法读取当前网页，请刷新后重试");
      // Walk only URLs exposed by visible frame elements in their parent page.
      // Hidden frames, unrelated advertising frames and frames without access
      // are not appended blindly just because allFrames injection succeeded.
      const selected = [top];
      const accepted = new Set([0]);
      for (let index = 0; index < selected.length && selected.length < MAX_FRAMES; index += 1) {
        const urls = new Set(selected[index].info.frames || []);
        descriptions.forEach(function (frame) {
          if (selected.length >= MAX_FRAMES) { limited = true; return; }
          if (!accepted.has(frame.frameId) && frame.info && frame.info.visible && urls.has(frame.info.url)) {
            selected.push(frame); accepted.add(frame.frameId);
          }
        });
        for (const url of urls) {
          if (!selected.some(function (frame) { return frame.info.url === url; })) limited = true;
        }
      }
      const requests = selected.map(async function (frame) {
        capturedIds.push(frame.frameId);
        const response = await message(tabId, frame.frameId,
          { type: "WEB_CLIP_CAPTURE_V4", options: { mode, requestId, autoScroll: autoScroll && frame.frameId === 0,
            scrollLimits: options.scrollLimits } }, mode === "region" ? 60000 : autoScroll ? 40000 : 10000, signal);
        if (!response || !response.ok) throw new Error(response && response.error || "页面内容提取失败");
        if ((mode === "selection" || mode === "region") && !String(response.raw_text || "").trim()) throw new Error("所选内容为空");
        return { frameId: frame.frameId, response };
      });
      let results;
      if (mode === "region" || mode === "selection") {
        try {
          const firstContent = Promise.any(requests);
          if (mode === "region") {
            const cancelledByUser = new Promise(function (_resolve, reject) {
              requests.forEach(function (request) {
                request.catch(function (error) { if (error.message === "已取消提取") reject(error); });
              });
            });
            results = [await Promise.race([firstContent, cancelledByUser])];
          } else results = [await firstContent];
        }
        catch (error) {
          if (signal && signal.aborted) throw new Error("已取消提取");
          throw new Error(mode === "selection" ? "请先在网页中选取正文文字，再提取" :
            error.errors && error.errors[0] && error.errors[0].message || "区域选择已取消");
        }
      } else {
        const settled = await Promise.allSettled(requests);
        if (settled[0] && settled[0].status === "rejected" && /访问限制|取消|页面已变化/.test(settled[0].reason.message || "")) throw settled[0].reason;
        results = settled.filter(function (item) { return item.status === "fulfilled"; }).map(function (item) { return item.value; });
        if (settled.some(function (item) { return item.status === "rejected"; })) limited = true;
      }
      if (signal && signal.aborted) throw new Error("已取消提取");
      const latest = await chrome.tabs.get(tabId);
      if (!samePage(latest.url, page.url) || (top.info.url && !samePage(top.info.url, page.url))) throw new Error("页面已变化，请重新提取");
      const result = merge(results, mode, page);
      if (limited) result.diagnostics.quality_warnings.push("frames_incomplete");
      return result;
    } catch (error) {
      if (/cannot\s+access|permission/i.test(error.message || "")) throw new Error("当前网页暂不支持剪藏（浏览器权限限制）");
      throw error;
    } finally {
      if (autoScroll && chrome.runtime.onMessage) chrome.runtime.onMessage.removeListener(progressListener);
      // Settle remaining region pickers immediately, including other frames.
      await Promise.allSettled(capturedIds.map(function (frameId) {
        return message(tabId, frameId, { type: "WEB_CLIP_CANCEL", requestId }, 1000);
      }));
    }
  }

  return { capture, merge, MAX_CHARS };
});

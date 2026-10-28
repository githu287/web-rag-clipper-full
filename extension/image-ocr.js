// Extension-origin image reads. Workspace credentials go only to the backend.
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagImageOcr = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const MAX_IMAGES = 8;
  const MAX_BYTES = 6 * 1024 * 1024;
  const TOTAL_BYTES = 24 * 1024 * 1024;
  const MAX_CHARS = 500000;

  async function readImage(url, signal, maxBytes) {
    maxBytes = Math.min(MAX_BYTES, maxBytes === undefined ? MAX_BYTES : maxBytes);
    function tooLarge() {
      const error = new Error(maxBytes < MAX_BYTES ? "图片总量超过 24 MiB 限制" : "图片超过 6 MiB 限制");
      if (maxBytes < MAX_BYTES) error.code = "OCR_BYTE_LIMIT";
      return error;
    }
    if (!/^https?:$/.test(new URL(url).protocol)) throw new Error("图片地址不支持");
    const controller = new AbortController();
    const abort = function () { controller.abort(); };
    const timer = setTimeout(abort, 12000);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    try {
      const response = await fetch(url, { signal: controller.signal, credentials: "omit", referrerPolicy: "no-referrer" });
      if (!response.ok || !response.body) throw new Error("图片读取失败");
      if (Number(response.headers.get("content-length")) > maxBytes) throw tooLarge();
      const type = (response.headers.get("content-type") || "application/octet-stream").split(";")[0].trim().toLowerCase();
      if (!/^image\/(png|jpeg|webp|gif|bmp)$/.test(type) && type !== "application/octet-stream") throw new Error("图片格式不支持");
      const reader = response.body.getReader();
      const parts = [];
      let size = 0;
      try {
        while (true) {
          const item = await reader.read();
          if (item.done) break;
          size += item.value.byteLength;
          if (size > maxBytes) throw tooLarge();
          parts.push(item.value);
        }
      } finally { await reader.cancel().catch(function () {}); reader.releaseLock(); }
      return new Blob(parts, { type });
    } finally { clearTimeout(timer); signal.removeEventListener("abort", abort); controller.abort(); }
  }

  async function enrich(page, options) {
    const controller = new AbortController();
    let timedOut = false;
    const abort = function () { controller.abort(); };
    options.signal.addEventListener("abort", abort, { once: true });
    if (options.signal.aborted) abort();
    const timer = setTimeout(function () { timedOut = true; abort(); }, 120000);
    const diagnostics = Object.assign({}, page.diagnostics);
    const warnings = new Set(diagnostics.quality_warnings || []);
    const images = Array.from(new Map((page.images || []).map(function (image) { return [image.url, image]; })).values());
    let text = page.raw_text || "";
    let processed = 0, recognized = 0, failed = 0, bytes = 0, empty = 0;
    const notices = new Set();
    if (images.length > MAX_IMAGES) warnings.add("ocr_image_limit");
    try {
      for (const image of images.slice(0, MAX_IMAGES)) {
        if (options.signal.aborted) throw new Error("已取消提取");
        if (timedOut) { warnings.add("ocr_time_limit"); break; }
        if (text.length >= MAX_CHARS) { warnings.add("truncated"); break; }
        await options.guard();
        options.onProgress({ phase: "ocr", current: processed + 1, total: Math.min(images.length, MAX_IMAGES) });
        try {
          if (bytes >= TOTAL_BYTES) { warnings.add("ocr_byte_limit"); break; }
          const blob = await readImage(image.url, controller.signal, TOTAL_BYTES - bytes);
          if (bytes + blob.size > TOTAL_BYTES) { warnings.add("ocr_byte_limit"); break; }
          bytes += blob.size;
          await options.guard();
          if (controller.signal.aborted) throw new Error("已取消提取");
          const result = await options.recognize(blob, controller.signal);
          await options.guard();
          for (const notice of result.warnings || []) notices.add(notice);
          const content = String(result.text || "").trim();
          if (content) {
            const label = String(image.alt || "").replace(/[\r\n]/g, " ").slice(0, 100);
            const addition = "\n\n---\n\n### 图片文字识别 " + (processed + 1) + (label ? "：" + label : "") + "\n\n" + content;
            if (text.length + addition.length > MAX_CHARS) warnings.add("truncated");
            text = (text + addition).slice(0, MAX_CHARS);
            recognized += 1;
          } else empty += 1;
        } catch (error) {
          if (options.signal.aborted) throw new Error("已取消提取");
          // Never continue under another tab, route or workspace identity.
          await options.guard();
          failed += 1;
          if (error && error.code === "OCR_BYTE_LIMIT") { warnings.add("ocr_byte_limit"); break; }
          if (error && (error.status === 401 || error.status === 403)) throw error;
          if (error && error.status === 503) { warnings.add("ocr_unavailable"); break; }
        }
        processed += 1;
      }
      if (options.signal.aborted) throw new Error("已取消提取");
      await options.guard();
      if (timedOut) warnings.add("ocr_time_limit");
      if (failed) warnings.add("ocr_failed");
      if (notices.size) warnings.add("ocr_review");
      if (recognized) warnings.delete("image_heavy");
      if (!images.length) warnings.add("ocr_no_images");
      if (!text.trim()) throw new Error("未识别到可保存的文字，请选择其他区域或检查图片访问权限");
      Object.assign(diagnostics, { ocr_enabled: true, ocr_recognized: recognized, ocr_failed: failed,
        ocr_empty: empty, ocr_candidates: images.length, ocr_notices: Array.from(notices),
        extracted_char_count: text.length, quality_warnings: Array.from(warnings) });
      // Source image URLs/bytes are ephemeral and never enter saved content.
      return Object.assign({}, page, { raw_text: text, images: [], diagnostics });
    } finally { clearTimeout(timer); options.signal.removeEventListener("abort", abort); controller.abort(); }
  }

  return { enrich, readImage, MAX_IMAGES, MAX_BYTES };
});

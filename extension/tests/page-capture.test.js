"use strict";
const assert = require("node:assert/strict");
const capture = require("../page-capture.js");

function result(frameId, text, url = "https://example.com/article") {
  return { frameId, response: { ok: true, url, raw_text: text,
    diagnostics: { quality_warnings: [], strategy: "article", truncated: false } } };
}
const page = { url: "https://example.com/article", title: "Top page" };

(async function () {
  const text = "正文内容".repeat(100);
  const merged = capture.merge([result(2, text), result(0, text), result(3, "嵌入文章", "https://frame.example/content")], "page", page);
  assert.equal(merged.raw_text.split(text).length, 2, "duplicate frame text must appear once");
  assert.match(merged.raw_text, /内嵌页面来源：https:\/\/frame.example\/content/);
  assert.equal(merged.diagnostics.included_frame_count, 1);
  assert.equal(merged.url, page.url, "frame selection must remain bound to the top-level page");
  const large = capture.merge([result(0, "a".repeat(499990)), result(1, "b".repeat(1000))], "page", page);
  assert.equal(large.raw_text.length, 500000);
  assert.equal(large.diagnostics.truncated, true);
  assert.throws(() => capture.merge([], "selection", page), /选中文字/);

  const frames = {
    0: { url: page.url, visible: true, frames: ["https://frame.example/content"] },
    1: { url: "https://frame.example/content", visible: true, frames: [] },
    2: { url: "https://frame.example/hidden", visible: false, frames: [] },
    3: { url: "https://ad.example/", visible: true, frames: [] },
  };
  let cancelled = [];
  let requested = [];
  let navigate = false;
  let hang = false;
  let blockedTop = false;
  const listeners = new Set();
  const captureOptions = [];
  global.chrome = {
    runtime: { onMessage: { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) } },
    scripting: { executeScript: async () => Object.keys(frames).map(frameId => ({ frameId: Number(frameId) })) },
    tabs: {
      get: async () => ({ ...page, url: navigate && requested.length ? "https://example.com/new" : page.url }),
      sendMessage(_tabId, message, options, callback) {
        const id = options.frameId;
        if (message.type === "WEB_CLIP_DESCRIBE") callback(frames[id]);
        if (message.type === "WEB_CLIP_CANCEL") { cancelled.push(id); callback({ ok: true }); }
        if (message.type === "WEB_CLIP_CAPTURE_V4") {
          requested.push(id);
          captureOptions.push({ id, options: message.options });
          if (message.options.autoScroll) {
            for (const listener of listeners) {
              listener({ type: "WEB_CLIP_PROGRESS", requestId: "old", steps: 7 }, { tab: { id: 9 }, frameId: 0 });
              listener({ type: "WEB_CLIP_PROGRESS", requestId: message.options.requestId, steps: 8 }, { tab: { id: 10 }, frameId: 0 });
              listener({ type: "WEB_CLIP_PROGRESS", requestId: message.options.requestId, steps: 9 }, { tab: { id: 9 }, frameId: 1 });
              listener({ type: "WEB_CLIP_PROGRESS", requestId: message.options.requestId, steps: 2, collected_chars: 100 }, { tab: { id: 9 }, frameId: 0 });
            }
          }
          if (!hang) callback(blockedTop && id === 0 ? { ok: false, error: typeof blockedTop === "string" ? blockedTop : "当前页面显示访问限制" } : result(id, id === 0 ? text : "内嵌正文").response);
        }
      },
    },
  };
  const actual = await capture.capture(9, { mode: "page" });
  assert.deepEqual(requested, [0, 1], "hidden and unrelated frames must not be captured");
  assert.match(actual.raw_text, /内嵌正文/);
  assert.equal(cancelled.length, 2);
  requested = []; cancelled = []; navigate = true;
  await assert.rejects(capture.capture(9, { mode: "article" }), /页面已变化/);
  assert.equal(cancelled.length, 2, "page navigation must still clean up capture listeners");
  requested = []; navigate = false; hang = true;
  const controller = new AbortController();
  const pending = capture.capture(9, { mode: "region", signal: controller.signal });
  setTimeout(() => controller.abort(), 10);
  await assert.rejects(pending, /取消/);
  hang = false;
  frames[0].frames.push("https://restricted.example/");
  requested = [];
  const partial = await capture.capture(9, { mode: "article" });
  assert(partial.diagnostics.quality_warnings.includes("frames_incomplete"));
  const progress = [];
  captureOptions.length = 0;
  await capture.capture(9, { mode: "page", autoScroll: true, onProgress: value => progress.push(value) });
  assert.deepEqual(progress.map(item => item.steps), [2], "only current tab, top frame and current request can update progress");
  assert(captureOptions.find(item => item.id === 0).options.autoScroll);
  assert.equal(captureOptions.find(item => item.id === 1).options.autoScroll, false);
  assert.equal(listeners.size, 0, "temporary progress listeners must be removed");
  blockedTop = true;
  await assert.rejects(capture.capture(9, { mode: "article" }), /访问限制/, "an accessible child must not hide a top-level access error");
  blockedTop = "已取消提取";
  await assert.rejects(capture.capture(9, { mode: "article", autoScroll: true }), /取消/, "a child-frame success must not hide Escape cancellation");
  assert.equal(listeners.size, 0);
  console.log("page capture filtering, deduplication, limits, cancellation and navigation tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

"use strict";
const assert = require("node:assert/strict");
const ocr = require("../image-ocr.js");
const originalFetch = global.fetch;
const page = (count = 1) => ({ url: "https://article.example/a", raw_text: "原有正文", diagnostics: { quality_warnings: ["image_heavy"] },
  images: Array.from({ length: count }, (_, i) => ({ url: "https://image.example/" + i, alt: "正文图" })) });
const okImage = () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } });
const options = overrides => ({ signal: new AbortController().signal, guard: async () => {}, onProgress: () => {},
  recognize: async () => ({ text: "图片正文", warnings: [] }), ...overrides });

(async () => {
  let reads = [];
  global.fetch = async (url, init) => { reads.push({ url, init }); return okImage(); };
  let result = await ocr.enrich(page(), options());
  assert.match(result.raw_text, /原有正文[\s\S]*图片文字识别 1：正文图[\s\S]*图片正文/);
  assert.equal(result.diagnostics.ocr_recognized, 1);
  assert(!result.diagnostics.quality_warnings.includes("image_heavy"));
  assert.equal(result.images.length, 0);
  assert.equal(reads[0].init.credentials, "omit");
  assert.equal(reads[0].init.headers, undefined, "workspace credentials never reach image hosts");
  assert(!result.raw_text.includes("image.example"));

  reads = [];
  result = await ocr.enrich(page(12), options());
  assert.equal(reads.length, 8);
  assert(result.diagnostics.quality_warnings.includes("ocr_image_limit"));
  const duplicates = page(); duplicates.images.push(duplicates.images[0]);
  reads = [];
  await ocr.enrich(duplicates, options());
  assert.equal(reads.length, 1);

  result = await ocr.enrich(page(), options({ recognize: async () => ({ text: "", warnings: [] }) }));
  assert.equal(result.raw_text, "原有正文");
  assert.equal(result.diagnostics.ocr_empty, 1);
  result = await ocr.enrich(page(), options({ recognize: async () => { throw { status: 503 }; } }));
  assert.equal(result.raw_text, "原有正文");
  assert(result.diagnostics.quality_warnings.includes("ocr_unavailable"));

  const controller = new AbortController();
  await assert.rejects(ocr.enrich(page(), options({ signal: controller.signal, recognize: async () => { controller.abort(); return { text: "STALE", warnings: [] }; } })), /取消/);
  let guards = 0, requests = 0;
  await assert.rejects(ocr.enrich(page(), options({ guard: async () => { if (++guards >= 2) throw new Error("身份已变化"); },
    recognize: async () => { requests++; } })), /身份已变化/);
  assert.equal(requests, 0);
  result = await ocr.enrich({ ...page(), raw_text: "x".repeat(499990) }, options());
  assert.equal(result.raw_text.length, 500000);
  assert(result.diagnostics.quality_warnings.includes("truncated"));

  global.fetch = async () => new Response(new Uint8Array([1]), { headers: { "Content-Type": "text/html" } });
  result = await ocr.enrich(page(), options());
  assert.equal(result.diagnostics.ocr_failed, 1);
  assert.equal(result.raw_text, "原有正文");
  global.fetch = async () => new Response(new Uint8Array(ocr.MAX_BYTES + 1), { headers: { "Content-Type": "image/png" } });
  await assert.rejects(ocr.readImage("https://example.com/image", new AbortController().signal), /6 MiB/);
  await assert.rejects(ocr.readImage("file:///secret", new AbortController().signal), /不支持/);
  console.log("OCR image bounds, source credential isolation, deduplication, cancellation, identity guard, partial failures and preview limits passed");
})().finally(() => { global.fetch = originalFetch; });

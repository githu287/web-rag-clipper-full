"use strict";

// clip-detect.js 回归测试：锁住「同一 URL 在 TTL 内只查一次」与
// 「缓存空结果 / 查询失败都不清空已知 documentId」两条契约。
// 第二条是跨上下文安全的关键：popup 与 Side Panel 各有一份缓存，互不通知。

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionDir = path.resolve(__dirname, "..");

const MATCHING_ITEMS = [
  { id: "11", url: "https://example.com/a", status: "SUCCESS", created_at: "2026-10-01T00:00:00" },
  { id: "12", url: "https://example.com/a", status: "SUCCESS", created_at: "2026-10-05T00:00:00" },
  // keyword 是 LIKE 查询：前缀相同但 url 不精确相等的候选必须被排除。
  { id: "13", url: "https://example.com/ab", status: "SUCCESS", created_at: "2026-10-09T00:00:00" },
  // 非 SUCCESS 的同一 URL 不参与「已剪藏」判定。
  { id: "14", url: "https://example.com/a", status: "PROCESSING", created_at: "2026-10-10T00:00:00" },
];

function loadClipDetect(initialItems) {
  const state = {
    calls: [],
    time: 1000000,
    items: initialItems === undefined ? MATCHING_ITEMS : initialItems,
    fail: false,
  };
  const webRagApiClient = {
    documents: {
      async list(params) {
        state.calls.push(params);
        if (state.fail) throw new Error("network down");
        return { items: state.items };
      },
    },
  };
  const context = vm.createContext({
    console,
    webRagApiClient,
    // 真实规范化规则参与测试，避免锚点/跟踪参数场景失真。
    webRagUrlUtils: undefined,
    Date: { now: function () { return state.time; } },
  });
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "url-utils.js"), "utf8"), context);
  vm.runInContext(
    fs.readFileSync(path.join(extensionDir, "clip-detect.js"), "utf8") +
      "\nglobalThis.__detect = webRagClipDetect;",
    context,
  );
  return { detect: context.__detect, state };
}

(async function run() {
  // ---------------------------------------------- 精确匹配 + created_at 最新
  {
    const { detect, state } = loadClipDetect();
    const documentId = await detect.detectDocumentId("plugin-a", "https://example.com/a", null);
    assert.equal(documentId, 12, "必须取精确匹配中 created_at 最新的一条");
    // 请求对象由沙箱构造，逐字段断言避免跨 realm 的原型比较。
    const params = state.calls[0];
    assert.equal(params.keyword, "https://example.com/a");
    assert.equal(params.status, "SUCCESS");
    assert.equal(params.page, 1);
    assert.equal(params.page_size, 100);
    assert.equal(Object.keys(params).sort().join(","), "keyword,page,page_size,status");
    assert.equal(state.calls.length, 1);
  }

  // ------------------------------------------------------ TTL 内不重复请求
  {
    const { detect, state } = loadClipDetect();
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), 12);
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), 12);
    assert.equal(state.calls.length, 1, "同一 URL 在 TTL 内只应查询一次");
    // 锚点与跟踪参数规范化后是同一个缓存项。
    assert.equal(
      await detect.detectDocumentId("plugin-a", "https://example.com/a#section", null),
      12
    );
    assert.equal(
      await detect.detectDocumentId("plugin-a", "https://example.com/a?utm_source=news#x", null),
      12
    );
    assert.equal(state.calls.length, 1, "规范化相同的 URL 必须命中同一缓存项");
    // 超过 TTL 后重新查询。
    state.time += detect.DETECT_TTL_MS + 1;
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), 12);
    assert.equal(state.calls.length, 2, "TTL 过期后必须重新查询");
  }

  // ------------------------------------- 空结果只影响新读取，不清空已知结果
  {
    const { detect, state } = loadClipDetect([]);
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), null);
    assert.equal(state.calls.length, 1);
    // 缓存里的「未剪藏」不得抹掉另一个上下文刚写入的 documentId。
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", 42), 42);
    assert.equal(state.calls.length, 1, "命中缓存时不应再次请求");
    // 但缓存过期后，真实删除必须能被感知（此时不再有已知结果保底）。
    state.time += detect.DETECT_TTL_MS + 1;
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", 42), null);
    assert.equal(state.calls.length, 2);
  }

  // ------------------------------------------- 查询失败保留已知结果且不缓存
  {
    const { detect, state } = loadClipDetect();
    state.fail = true;
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", 42), 42, "网络失败不应清空已知结果");
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), null);
    assert.equal(state.calls.length, 2, "失败结果不得写入缓存");
    // 恢复后立即重查，不应被失败期间的空缓存拖住。
    state.fail = false;
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), 12);
    assert.equal(state.calls.length, 3);
  }

  // ------------------------------------------------- 缓存按 plugin 隔离
  {
    const { detect, state } = loadClipDetect();
    assert.equal(await detect.detectDocumentId("plugin-a", "https://example.com/a", null), 12);
    assert.equal(await detect.detectDocumentId("plugin-b", "https://example.com/a", null), 12);
    assert.equal(state.calls.length, 2, "不同 Plugin Workspace 不得共用检测结果");
  }

  // --------------------------------------------------- 非法/缺失入参
  {
    const { detect, state } = loadClipDetect();
    assert.equal(await detect.detectDocumentId("", "https://example.com/a", 42), 42);
    assert.equal(await detect.detectDocumentId("plugin-a", "", 42), 42);
    // 非 http(s) 协议：normalizeWebUrl 抛错，必须退回已知结果而不是清空。
    assert.equal(await detect.detectDocumentId("plugin-a", "chrome://extensions", 42), 42);
    assert.equal(state.calls.length, 0, "入参不合法时不应发起请求");
  }

  console.log("clip-detect cache tests passed");
})().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});

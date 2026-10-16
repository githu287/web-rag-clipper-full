// clip-detect.js —— 「当前网页是否已剪藏」的探测与短 TTL 缓存
// 依赖全局 webRagApiClient / webRagUrlUtils（脚本顺序见 sidepanel.html）。
// 禁止：把探测结果写进 chrome.storage —— 绑定关系由 session-store 的 tabBindings 负责。
"use strict";

const webRagClipDetect = (() => {
  // Tab 来回切换时同一 URL 会反复打 documents.list（keyword 走 LIKE + page_size=100），
  // 结果在很短时间内是稳定的，用短 TTL 缓存压掉重复请求。
  const DETECT_TTL_MS = 30000;
  // 只保内存上限，不做逐条淘汰：面板生命周期内访问的 URL 数远小于该值，
  // 触顶时整表清空最多退化成「多发一次请求」。
  const DETECT_CACHE_MAX = 200;
  const detected = new Map();

  // 返回「应当写入 binding.documentId 的值」。
  // knownDocumentId 是调用方当前已知的结果：查询失败或缓存里的空结果都不能清空它，
  // 因为 popup 与 Side Panel 是两个 JS 上下文、缓存互不相通，
  // 否则「popup 刚剪藏成功、切回该 Tab」会把 documentId 抹掉。
  async function detectDocumentId(pluginId, pageUrl, knownDocumentId) {
    const fallback = knownDocumentId == null ? null : Number(knownDocumentId);
    if (!pluginId || !pageUrl) return fallback;
    let normalizedUrl = null;
    try {
      normalizedUrl = webRagUrlUtils.normalizeWebUrl(pageUrl);
    } catch (_err) {
      return fallback;
    }
    const key = pluginId + "\n" + normalizedUrl;
    const cached = detected.get(key);
    if (cached && cached.expiresAt > Date.now()) {
      return cached.documentId == null ? fallback : cached.documentId;
    }
    let data = null;
    try {
      data = await webRagApiClient.documents.list({
        keyword: normalizedUrl,
        status: "SUCCESS",
        page: 1,
        page_size: 100,
      });
    } catch (_err) {
      // 失败不写缓存：一次网络抖动不该让该页面在 TTL 内一直显示「未剪藏」。
      return fallback;
    }
    // keyword 是 LIKE 查询，必须精确匹配 url
    const matches = ((data && data.items) || []).filter(function (item) {
      return item && item.url === normalizedUrl && item.status === "SUCCESS";
    });
    // 多个匹配时取 created_at 最新的
    matches.sort(function (a, b) {
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
    const documentId = matches.length > 0 ? Number(matches[0].id) : null;
    if (detected.size >= DETECT_CACHE_MAX) detected.clear();
    detected.set(key, { documentId: documentId, expiresAt: Date.now() + DETECT_TTL_MS });
    return documentId;
  }

  return {
    detectDocumentId: detectDocumentId,
    DETECT_TTL_MS: DETECT_TTL_MS,
  };
})();

// content.js —— 网页正文提取（Web RAG Clipper Phase 3.2）
//
// 职责：
//   1. 作为注入到当前网页的 content script，仅负责「网页采集」：
//      从页面提取 URL / Title / 正文纯文本，通过 chrome.runtime.onMessage
//      响应 popup 的请求。
//   2. 不进行 Embedding / Chunking / Milvus / 数据库操作 —— 后端负责全链路。
//
// 正文提取策略（MVP，不引入第三方库）：
//   优先 document.querySelector("article")
//   其次 document.querySelector("main")
//   最后 document.body
//   提取前先 clone 节点（避免改动原页面），在 clone 上移除：
//     script / style / noscript / template
//     nav / footer / header / aside
//     iframe / form / button / svg / canvas / dialog
//   以及 class/id 命中常见广告/无关区特征（ad / banner / promo / sponsor / sidebar）的节点。
//   最终取 textContent → trim → 空白归一化 → 连续换行压缩。
//
// 幂等性：
//   每次 popup 打开都可能重新注入本脚本；通过 window.__WEB_RAG_CLIPPER_INJECTED__
//   标记避免重复注册 onMessage 监听器。

(() => {
  "use strict";

  if (window.__WEB_RAG_CLIPPER_INJECTED__) {
    return;
  }
  window.__WEB_RAG_CLIPPER_INJECTED__ = true;

  // ---------------------------------------------------------------- 正文提取
  const CANDIDATE_SELECTORS = ["article", "main", "body"];

  // 需要整体移除的标签（导航/页脚/表单/装饰性/嵌入内容等无关正文区域）
  const REMOVE_SELECTOR = [
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "footer",
    "header",
    "aside",
    "iframe",
    "form",
    "button",
    "svg",
    "canvas",
    "dialog",
  ].join(", ");

  // 常见广告/无关区域 class|id 特征（大小写不敏感；命中即移除整棵子树）
  const NOISE_PATTERN = /(^|[\s_-])(ad|ads|advert|advertisement|banner|promo|sponsor|sidebar|related|recommend)([\s_-]|$)/i;

  function isNoiseElement(el) {
    const className = String(el.className || "");
    const id = String(el.id || "");
    return NOISE_PATTERN.test(className) || NOISE_PATTERN.test(id);
  }

  function extractRawText() {
    // 1) 选择正文根节点：article → main → body
    let root = null;
    for (const selector of CANDIDATE_SELECTORS) {
      const candidate = document.querySelector(selector);
      if (candidate && candidate.textContent && candidate.textContent.trim().length > 0) {
        root = candidate;
        break;
      }
    }
    if (!root) {
      root = document.body;
    }
    if (!root) {
      return "";
    }

    // 2) clone 节点树，在副本上清理，绝不修改原页面
    const clone = root.cloneNode(true);

    // 3) 移除无关标签
    clone.querySelectorAll(REMOVE_SELECTOR).forEach((node) => node.remove());

    // 4) 移除广告/无关区域
    clone.querySelectorAll("*").forEach((node) => {
      if (isNoiseElement(node)) {
        node.remove();
      }
    });

    // 5) textContent → 空白归一化 → 连续换行压缩
    let text = clone.textContent || "";
    text = text.replace(/\u00a0|\u200b/g, " "); // NBSP / 零宽空格 → 普通空格
    text = text.replace(/[ \t\f\v]+/g, " ");    // 行内空白压缩
    text = text.replace(/\n[ \t]*/g, "\n");     // 行尾残留空白清理
    text = text.replace(/\n{3,}/g, "\n\n");     // 连续空行压缩为最多 1 个空行

    return text.trim();
  }

  // ------------------------------------------------------------ 消息响应
  // 同步 sendResponse：提取逻辑为纯同步 DOM 操作，无需 return true 异步通道。
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message.type !== "string") {
      return;
    }
    if (message.type === "WEB_CLIP_PING") {
      sendResponse({ ok: true });
      return;
    }
    if (message.type === "WEB_CLIP_EXTRACT") {
      sendResponse({
        ok: true,
        url: document.URL,
        title: document.title || "",
        raw_text: extractRawText(),
      });
    }
  });

  // ------------------------------------------------------------ URL 变化通知（SPA，Phase 3.4 Step F8 Step 2）
  // 独立消息 WEB_RAG_URL_CHANGED，不改变 WEB_CLIP_PING / WEB_CLIP_EXTRACT 原有行为。
  // history.pushState / replaceState 不触发 chrome.tabs.onUpdated，SPA 路由变化依赖此通知
  // 使 background 将旧 documentId 置为失效（stale），避免跨页面错误复用。
  const notifyUrlChanged = () => {
    try {
      chrome.runtime.sendMessage({
        type: "WEB_RAG_URL_CHANGED",
        url: document.URL,
        title: document.title || "",
      });
    } catch (_err) {
      // 通知失败不影响页面
    }
  };

  const patchHistoryMethod = (method) => {
    if (typeof history[method] !== "function" || history["__webRagOriginal_" + method]) {
      return;
    }
    const original = history[method];
    history["__webRagOriginal_" + method] = original;
    history[method] = function (...args) {
      const result = original.apply(history, args);
      notifyUrlChanged();
      return result;
    };
  };

  patchHistoryMethod("pushState");
  patchHistoryMethod("replaceState");
  window.addEventListener("popstate", notifyUrlChanged);
})();

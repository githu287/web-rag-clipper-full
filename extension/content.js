// content.js —— 网页正文提取入口（Web RAG Clipper）
//
// 职责：
//   1. 作为注入到当前网页的 content script，仅负责「网页采集」：
//      从页面提取 URL / Title / 正文纯文本，通过 chrome.runtime.onMessage
//      响应 popup 的请求。
//   2. 不进行 Embedding / Chunking / Milvus / 数据库操作 —— 后端负责全链路。
//
// 正文选择、清理和结构化文本序列化由 extractor.js 负责，本文件只负责消息通信
// 以及 SPA URL 变化通知。
//
// 重复注入时会主动替换消息监听器与 SPA 监听器，兼容扩展重新加载后页面中残留的
// 失效脚本上下文。

(() => {
  "use strict";

  const CONTENT_SCRIPT_VERSION = "0.7.4";
  window.__WEB_RAG_CLIPPER_INJECTED__ = CONTENT_SCRIPT_VERSION;

  function extractPage() {
    const extractor = globalThis.webRagPageExtractor;
    if (extractor && typeof extractor.extract === "function") {
      return extractor.extract(document);
    }
    const text = String(document.body && document.body.innerText || "").trim();
    return {
      text,
      diagnostics: {
        extractor_version: "fallback",
        strategy: "body.innerText",
        candidate_count: 0,
        source_char_count: text.length,
        cleaned_source_char_count: text.length,
        extracted_char_count: text.length,
        removed_node_count: 0,
        hidden_removed_node_count: 0,
        noise_removed_node_count: 0,
        shadow_root_count: 0,
        heading_count: 0,
        paragraph_count: 0,
        list_item_count: 0,
        code_block_count: 0,
        inline_code_count: 0,
        emphasis_count: 0,
        table_count: 0,
        link_count: 0,
        preserved_link_count: 0,
        link_density: 0,
        fallback: true,
        truncated: false,
        quality_warnings: ["fallback_root"],
      },
    };
  }

  // ------------------------------------------------------------ 消息响应
  // 同步 sendResponse：提取逻辑为纯同步 DOM 操作，无需 return true 异步通道。
  const previousMessageListener = window.__WEB_RAG_CLIPPER_MESSAGE_LISTENER__;
  if (typeof previousMessageListener === "function") {
    try {
      chrome.runtime.onMessage.removeListener(previousMessageListener);
    } catch (_err) {}
  }
  const handleMessage = (message, _sender, sendResponse) => {
    if (!message || typeof message.type !== "string") {
      return;
    }
    if (message.type === "WEB_CLIP_PING") {
      sendResponse({ ok: true });
      return;
    }
    if (message.type === "WEB_CLIP_EXTRACT_V3") {
      try {
        const result = extractPage();
        sendResponse({
          ok: true,
          url: document.URL,
          title: document.title || "",
          raw_text: result.text,
          diagnostics: result.diagnostics,
        });
      } catch (error) {
        console.error("Web RAG Clipper extraction failed", error);
        sendResponse({
          ok: false,
          error: error && error.message ? String(error.message) : "未知提取错误",
        });
      }
    }
  };
  window.__WEB_RAG_CLIPPER_MESSAGE_LISTENER__ = handleMessage;
  chrome.runtime.onMessage.addListener(handleMessage);

  // ------------------------------------------------------------ URL 变化通知（SPA，Phase 3.4 Step F8 Step 2）
  // 独立消息 WEB_RAG_URL_CHANGED，不改变 WEB_CLIP_PING / WEB_CLIP_EXTRACT_V3 原有行为。
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
    const originalKey = "__webRagOriginal_" + method;
    const original = history[originalKey] || history[method];
    if (typeof original !== "function") return;
    history[originalKey] = original;
    history[method] = function (...args) {
      const result = original.apply(history, args);
      notifyUrlChanged();
      return result;
    };
  };

  patchHistoryMethod("pushState");
  patchHistoryMethod("replaceState");
  const previousPopstateListener = window.__WEB_RAG_CLIPPER_POPSTATE_LISTENER__;
  if (typeof previousPopstateListener === "function") {
    window.removeEventListener("popstate", previousPopstateListener);
  }
  window.__WEB_RAG_CLIPPER_POPSTATE_LISTENER__ = notifyUrlChanged;
  window.addEventListener("popstate", notifyUrlChanged);
})();

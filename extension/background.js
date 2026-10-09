// background.js —— Web RAG Clipper 浏览器事件协调层（Phase 3.5 Step 2-F）
// 职责：sidePanel 行为 + tabs 事件 + 向 Side Panel 广播。
// 禁止：发送任何业务请求（/clips /rag/* /plugins/register）；保存/解密凭证。
"use strict";

importScripts("config.js", "url-utils.js", "session-store.js");

// STORAGE_KEYS 已在 config.js 全局声明，禁止重复声明（避免 SW 解析失败）。

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

function broadcast(message) {
  try {
    chrome.runtime.sendMessage(message).catch(() => {});
  } catch (_err) {}
}

function isHttpUrl(url) {
  return typeof url === "string" && /^https?:/.test(url);
}

function isSameNormalizedUrl(left, right) {
  try {
    return webRagUrlUtils.normalizeWebUrl(left) === webRagUrlUtils.normalizeWebUrl(right);
  } catch (_err) {
    return left === right;
  }
}

async function handleUrlChanged(tabId, url, title) {
  if (!isHttpUrl(url)) return;
  const updated = await sessionStore.updateTabBinding(tabId, function (binding) {
    if (isSameNormalizedUrl(binding.pageUrl, url) && !binding.stale) {
      // 只有 fragment / 跟踪参数变化时保留 Document 绑定，但更新展示 URL。
      return Object.assign({}, binding, {
        pageUrl: url,
        pageTitle: typeof title === "string" && title ? title : binding.pageTitle,
        updatedAt: Date.now(),
      });
    }
    return Object.assign({}, binding, {
      documentId: null,
      ingestJobId: null,
      ingestJobPageUrl: null,
      pageUrl: url,
      pageTitle: typeof title === "string" && title ? title : binding.pageTitle,
      stale: true,
      updatedAt: Date.now(),
    });
  });
  if (!updated) return;
  broadcast({ type: "WEB_RAG_TAB_URL_CHANGED", tabId: tabId, url: url, title: updated.pageTitle || "" });
}

chrome.tabs.onActivated.addListener((activeInfo) => {
  broadcast({ type: "WEB_RAG_TAB_ACTIVATED", tabId: activeInfo.tabId, windowId: activeInfo.windowId });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url && typeof changeInfo.url === "string") {
    handleUrlChanged(tabId, changeInfo.url, tab && tab.title).catch((err) => {
      console.error("[background] onUpdated handleUrlChanged 失败:", err);
    });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  sessionStore.removeTabBinding(tabId).catch((err) => {
    console.error("[background] removeTabBinding 失败:", err);
  });
  broadcast({ type: "WEB_RAG_TAB_REMOVED", tabId: tabId });
});

// content.js SPA URL 变化上报
chrome.runtime.onMessage.addListener((message, sender, _sendResponse) => {
  if (!message || message.type !== "WEB_RAG_URL_CHANGED") return;
  const tab = sender && sender.tab;
  if (!tab || tab.id == null) return;
  handleUrlChanged(tab.id, message.url || tab.url || "", message.title || tab.title || "").catch((err) => {
    console.error("[background] URL_CHANGED handleUrlChanged 失败:", err);
  });
});

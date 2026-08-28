// background.js —— Web RAG Clipper 浏览器事件协调层（Phase 3.5 Step 2-F）
// 职责：sidePanel 行为 + tabs 事件 + 向 Side Panel 广播。
// 禁止：发送任何业务请求（/clips /rag/* /plugins/register）；保存/解密凭证。
"use strict";

importScripts("config.js", "session-store.js");

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

async function handleUrlChanged(tabId, url, title) {
  if (!isHttpUrl(url)) return;
  const stored = await chrome.storage.local.get(STORAGE_KEYS.TAB_BINDINGS);
  const bindings = stored[STORAGE_KEYS.TAB_BINDINGS] || {};
  const key = String(tabId);
  const binding = bindings[key];
  if (!binding) return;
  if (binding.pageUrl === url && !binding.stale) return;
  bindings[key] = Object.assign({}, binding, {
    documentId: null,
    pageUrl: url,
    pageTitle: typeof title === "string" && title ? title : binding.pageTitle,
    stale: true,
    updatedAt: Date.now(),
  });
  try {
    await chrome.storage.local.set({ [STORAGE_KEYS.TAB_BINDINGS]: bindings });
  } catch (_err) {}
  broadcast({ type: "WEB_RAG_TAB_URL_CHANGED", tabId: tabId, url: url, title: bindings[key].pageTitle || "" });
}

chrome.tabs.onActivated.addListener((activeInfo) => {
  broadcast({ type: "WEB_RAG_TAB_ACTIVATED", tabId: activeInfo.tabId, windowId: activeInfo.windowId });
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url && typeof changeInfo.url === "string") {
    handleUrlChanged(tabId, changeInfo.url, tab && tab.title);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  try {
    sessionStore.removeTabBinding(tabId);
  } catch (_err) {}
  broadcast({ type: "WEB_RAG_TAB_REMOVED", tabId: tabId });
});

// content.js SPA URL 变化上报
chrome.runtime.onMessage.addListener((message, sender, _sendResponse) => {
  if (!message || message.type !== "WEB_RAG_URL_CHANGED") return;
  const tab = sender && sender.tab;
  if (!tab || tab.id == null) return;
  handleUrlChanged(tab.id, message.url || tab.url || "", message.title || tab.title || "");
});

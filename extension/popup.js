// popup.js —— Web RAG Clipper 快速剪藏入口（Phase 3.5 Step 2-F）
// 不再承载完整聊天，防止 Popup / Side Panel 各维护一套聊天状态导致第二套串台。
// 复用：config.js / session-store.js / api-client.js。
"use strict";

const els = {
  viewNotLoggedIn: document.getElementById("view-not-logged-in"),
  viewLoggedIn: document.getElementById("view-logged-in"),
  openPanelBtn: document.getElementById("open-panel-btn"),
  openPanelBtn2: document.getElementById("open-panel-btn-2"),
  warnBanner: document.getElementById("warn-banner"),
  warnOpenPanel: document.getElementById("warn-open-panel"),
  pageTitle: document.getElementById("page-title"),
  pageUrl: document.getElementById("page-url"),
  textLength: document.getElementById("text-length"),
  clipBtn: document.getElementById("clip-btn"),
  statusTitle: document.getElementById("status-title"),
  statusDetail: document.getElementById("status-detail"),
};

// ApiRequestError 由 api-client.js 顶层 class 声明，此处禁止重复声明（会与 api-client.js 冲突导致解析失败）。
// 需要时使用 webRagApiClient.ApiRequestError。

let currentTabId = null;
let currentPage = null;
let clipBusy = false;
const JOB_POLL_INTERVAL_MS = 1500;

// ================================================================ 工具
async function getCurrentTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs.length > 0 ? tabs[0] : null;
}

function setStatus(title, type, detail) {
  els.statusTitle.textContent = title;
  els.statusTitle.classList.remove("ok", "err");
  if (type) els.statusTitle.classList.add(type);
  els.statusDetail.textContent = detail || "";
}

function errorText(err) {
  if (err instanceof webRagApiClient.ApiRequestError) {
    switch (err.code) {
      case "UNAUTHENTICATED":
        return "插件凭证已失效，请重新创建插件";
      case "PLUGIN_DISABLED":
      case "DISABLED":
        return "插件已被禁用，请联系管理员";
      case "API_KEY_NOT_CONFIGURED":
        return "请前往设置配置阿里云百炼 API Key";
      case "NETWORK":
        return "网络错误，请重试";
      default:
        return err.message ? err.message : "出错了，请重试";
    }
  }
  return err && err.message ? err.message : "网络错误，请重试";
}

// ================================================================ Side Panel
async function openSidePanel() {
  const tab = await getCurrentTab();
  if (!tab) return;
  try {
    await chrome.sidePanel.open({ tabId: tab.id, windowId: tab.windowId });
  } catch (_err) {
    try {
      await chrome.sidePanel.open({ windowId: tab.windowId });
    } catch (_err2) {}
  }
}

// ================================================================ 页面提取
async function extractCurrentPage() {
  const tab = await getCurrentTab();
  if (!tab || tab.id == null || !/^https?:/.test(tab.url || "")) {
    throw new Error("当前标签页不是可访问的网页");
  }
  currentTabId = tab.id;
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (_err) {}
  const response = await chrome.tabs.sendMessage(tab.id, { type: "WEB_CLIP_EXTRACT" });
  if (!response || response.ok !== true || typeof response.raw_text !== "string") {
    throw new Error("页面内容提取失败，请刷新页面后重试");
  }
  currentPage = {
    url: response.url || tab.url || "",
    title: response.title || tab.title || "",
    raw_text: response.raw_text,
  };
  return currentPage;
}

// ================================================================ 剪藏
async function clipCurrentPage() {
  if (clipBusy) return;
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderNotLoggedIn();
    return;
  }
  clipBusy = true;
  els.clipBtn.disabled = true;
  setStatus("剪藏中…", null, "");
  try {
    const page = await extractCurrentPage();
    const job = await webRagApiClient.clips.clipAsync({
      url: page.url,
      title: page.title,
      raw_text: page.raw_text,
    });
    if (job && job.id) {
      await savePendingJobBinding(plugin.pluginId, page, job.id);
      await monitorJobInPopup(job.id, page);
    } else {
      setStatus("剪藏失败", "err", "后端未返回 job id");
    }
  } catch (err) {
    setStatus("剪藏失败", "err", errorText(err));
  } finally {
    clipBusy = false;
    els.clipBtn.disabled = false;
  }
}

async function savePendingJobBinding(pluginId, page, jobId) {
  const tab = await getCurrentTab();
  if (!tab || tab.id == null) return;
  currentTabId = tab.id;
  const existing = await sessionStore.getTabBinding(tab.id);
  const next = existing && existing.pluginId === pluginId ? existing : {
    pluginId: pluginId,
    mode: "current",
  };
  next.documentId = null;
  next.ingestJobId = jobId;
  next.ingestJobPageUrl = page.url;
  next.pageUrl = page.url;
  next.pageTitle = page.title;
  next.stale = false;
  next.updatedAt = Date.now();
  await sessionStore.setTabBinding(tab.id, next);
}

function waitMilliseconds(milliseconds) {
  return new Promise(function (resolve) {
    setTimeout(resolve, milliseconds);
  });
}

async function monitorJobInPopup(jobId, page) {
  while (true) {
    const job = await webRagApiClient.jobs.get(jobId);
    if (job.status === "FAILED") {
      throw new Error(job.error_message || "入库失败");
    }
    if (job.status === "SUCCEEDED") {
      const document = await webRagApiClient.documents.get(job.document_id);
      const binding = await sessionStore.getTabBinding(currentTabId);
      if (binding && binding.ingestJobId === jobId) {
        let pageStillMatches = !binding.stale;
        try {
          pageStillMatches = pageStillMatches && (
            webRagUrlUtils.normalizeWebUrl(binding.pageUrl) ===
            webRagUrlUtils.normalizeWebUrl(binding.ingestJobPageUrl)
          );
        } catch (_err) {
          pageStillMatches = false;
        }
        binding.ingestJobId = null;
        binding.ingestJobPageUrl = null;
        if (pageStillMatches) {
          binding.documentId = Number(job.document_id);
          binding.documentUrl = document.url || webRagUrlUtils.normalizeWebUrl(page.url);
          binding.stale = false;
        }
        await sessionStore.setTabBinding(currentTabId, binding);
      }
      try {
        chrome.runtime.sendMessage({
          type: "WEB_RAG_CLIP_COMPLETED",
          tabId: currentTabId,
          documentId: Number(job.document_id),
        });
      } catch (_err) {}
      setStatus(
        "✓ 剪藏成功（Document #" + job.document_id + "）",
        "ok",
        "请在 Side Panel 中提问。"
      );
      return;
    }
    setStatus(
      job.status === "QUEUED" ? "等待后台 Worker…" : "正在向量化入库…",
      null,
      (Number(job.progress) || 0) + "%"
    );
    await waitMilliseconds(JOB_POLL_INTERVAL_MS);
  }
}

// ================================================================ 视图
function renderNotLoggedIn() {
  els.viewNotLoggedIn.hidden = false;
  els.viewLoggedIn.hidden = true;
}

function renderLoggedIn() {
  const plugin = webRagApiClient.getPlugin();
  els.viewNotLoggedIn.hidden = true;
  els.viewLoggedIn.hidden = false;
  els.warnBanner.hidden = !(plugin.pluginId && plugin.apiKeyConfigured === false);
  setStatus("准备中...", null, "");
}

function renderPageInfo() {
  if (!currentPage) return;
  els.pageTitle.textContent = currentPage.title || "（无标题）";
  els.pageUrl.textContent = currentPage.url || "—";
  els.textLength.textContent = currentPage.raw_text ? currentPage.raw_text.length + " 字" : "—";
}

// ================================================================ 事件
function bindEvents() {
  els.openPanelBtn.addEventListener("click", function () {
    openSidePanel();
  });
  els.openPanelBtn2.addEventListener("click", function () {
    openSidePanel();
  });
  els.warnOpenPanel.addEventListener("click", function () {
    openSidePanel();
  });
  els.clipBtn.addEventListener("click", function () {
    clipCurrentPage();
  });
}

// ================================================================ 初始化
async function init() {
  bindEvents();
  await webRagApiClient.loadPlugin();
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderNotLoggedIn();
    return;
  }
  renderLoggedIn();
  try {
    await extractCurrentPage();
    renderPageInfo();
    const b = await sessionStore.getTabBinding(currentTabId);
    if (b && b.documentId != null && !b.stale) {
      setStatus("✓ 当前页面已剪藏（Document #" + b.documentId + "）", "ok", "点击上方按钮可重新剪藏。");
    } else {
      setStatus("就绪", null, "可剪藏当前网页。");
    }
  } catch (err) {
    setStatus("页面提取失败", "err", errorText(err));
  }
}

init();

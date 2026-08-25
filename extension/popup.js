// popup.js —— 剪藏 + AI 问答交互逻辑（Web RAG Clipper Phase 3.3 Step 3；
//              Phase 3.4 Step 4 加入 API Key 认证）
//
// 流程（Popup → Content Script → Backend）：
//   1. 获取当前活动 tab；检查 content.js 是否已注入（PING），未注入则动态注入
//   2. 向 content.js 请求 { url, title, raw_text }，展示页面信息
//   3. 点击「剪藏当前页面」→ POST {API_BASE_URL}/clips → 成功保存 currentDocumentId
//   4. AI 问答 Tab：
//      - 模式「当前网页」：POST {API_BASE_URL}/rag/ask { query, document_id }
//      - 模式「全部知识库」：POST {API_BASE_URL}/rag/ask { query }（不传 document_id）
//      - 渲染 Answer + Sources；Loading / Error / Empty 状态
//
// 认证（Phase 3.4 Step 4）：
//   - 用户在 Auth bar 输入百炼 API Key → POST /auth/login（未注册 401 →
//     POST /auth/register）→ 拿到 opaque token 存 chrome.storage.local；
//   - 剪藏 / 问答请求一律携带 Authorization: Bearer <token>；
//   - 收到 401 → 清除本地 token（invalidateAuth）并提示重新连接。
//
// 约束：
//   - 只负责「网页采集 + 调 API + UI」，不做 Embedding / Chunking / Milvus / LLM。
//   - 后端地址统一从 config.js 读取。
//   - 安全渲染：所有不可信内容一律使用 textContent / DOM API，禁止 innerHTML 拼接。

"use strict";

const API_BASE_URL = WEB_RAG_CLIPPER_CONFIG.API_BASE_URL;

const els = {
  // Tabs
  tabClip: document.getElementById("tab-clip"),
  tabAsk: document.getElementById("tab-ask"),
  panelClip: document.getElementById("panel-clip"),
  panelAsk: document.getElementById("panel-ask"),
  // 剪藏
  pageTitle: document.getElementById("page-title"),
  pageUrl: document.getElementById("page-url"),
  textLength: document.getElementById("text-length"),
  clipBtn: document.getElementById("clip-btn"),
  state: document.querySelector("#status .state"),
  detail: document.getElementById("status-detail"),
  // AI 问答
  askPageTitle: document.getElementById("ask-page-title"),
  modeCurrent: document.getElementById("mode-current"),
  modeAll: document.getElementById("mode-all"),
  chatArea: document.getElementById("chat-area"),
  chatEmpty: document.getElementById("chat-empty"),
  chatTextarea: document.getElementById("chat-textarea"),
  chatSend: document.getElementById("chat-send"),
  // 认证（Phase 3.4 Step 4）
  authApiKey: document.getElementById("auth-api-key"),
  authBtn: document.getElementById("auth-btn"),
  authStatus: document.getElementById("auth-status"),
};

// ---------------------------------------------------------------- 状态
let currentPage = null;
let currentTabId = null;
// 剪藏成功后保存的 Document ID（AI 问答「当前网页」模式使用）
let currentDocumentId = null;
// 当前问答模式："current"（当前网页） | "all"（全部知识库）
let askMode = "current";
let isSending = false;
// 认证状态（Phase 3.4 Step 4）：token 同时保存于 chrome.storage.local
let authToken = null;
let authUserId = null;
let authBusy = false;

// ---------------------------------------------------------------- 剪藏状态渲染
function setState(kind, text, detail) {
  els.state.textContent = text;
  els.state.className = "state " + kind;
  els.detail.textContent = detail || "";
}

function setClipEnabled(enabled) {
  els.clipBtn.disabled = !enabled;
  els.clipBtn.textContent = enabled ? "剪藏当前页面" : "剪藏中...";
}

// ---------------------------------------------------------------- 认证（Phase 3.4 Step 4）
function setAuthStatus(text, kind) {
  els.authStatus.textContent = text;
  els.authStatus.className = "auth-status" + (kind ? " " + kind : "");
}

function setAuthEnabled(enabled) {
  els.authBtn.disabled = !enabled;
  els.authBtn.textContent = enabled ? "连接" : "连接中...";
}

async function loadAuth() {
  const stored = await chrome.storage.local.get(["webRagToken", "webRagUserId"]);
  if (stored && stored.webRagToken) {
    authToken = String(stored.webRagToken);
    authUserId = stored.webRagUserId != null ? Number(stored.webRagUserId) : null;
    setAuthed(true);
  } else {
    authToken = null;
    authUserId = null;
    setAuthed(false);
  }
}

function setAuthed(authed) {
  if (authed) {
    els.authApiKey.value = "";
    els.authApiKey.style.display = "none";
    els.authBtn.style.display = "none";
    setAuthStatus("已连接（用户 #" + (authUserId != null ? authUserId : "?") + "）", "ok");
  } else {
    els.authApiKey.style.display = "";
    els.authBtn.style.display = "";
    if (authUserId != null) {
      setAuthStatus("连接已失效，请重新输入 API Key", "err");
    } else {
      setAuthStatus("未连接：输入 API Key 后点击「连接」");
    }
  }
}

async function invalidateAuth() {
  authToken = null;
  authUserId = null;
  await chrome.storage.local.remove(["webRagToken", "webRagUserId"]);
  setAuthed(false);
}

async function authenticate() {
  if (authBusy) {
    return;
  }
  const apiKey = els.authApiKey.value.trim();
  if (!apiKey) {
    setAuthStatus("请输入百炼 API Key", "err");
    return;
  }
  authBusy = true;
  setAuthEnabled(false);
  setAuthStatus("正在连接后端...");
  try {
    // 1) 先尝试登录（已注册用户）：成功直接返回 token
    let data = await authRequest("/auth/login", apiKey);
    // 2) 未注册（401）→ 注册并签发 token
    if (!data) {
      data = await authRequest("/auth/register", apiKey);
    }
    if (!data) {
      setAuthStatus("连接失败：API Key 校验未通过（请检查 Key 或后端服务）", "err");
      return;
    }
    authToken = data.token;
    authUserId = data.user_id;
    await chrome.storage.local.set({ webRagToken: authToken, webRagUserId: authUserId });
    setAuthed(true);
  } catch (err) {
    setAuthStatus("无法连接后端（" + API_BASE_URL + "）", "err");
  } finally {
    authBusy = false;
    setAuthEnabled(true);
  }
}

// 登录 / 注册请求；成功返回 { token, user_id }，失败返回 null（网络错误抛异常）。
async function authRequest(path, apiKey) {
  let response;
  try {
    response = await fetch(API_BASE_URL + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
  } catch (_err) {
    throw new Error("network");
  }
  const data = await response.json().catch(() => null);
  if (response.ok && data && typeof data.token === "string" && data.token) {
    return data;
  }
  return null;
}

// 带 Bearer token 的 fetch 包装；token 存在时自动附加 Authorization 头。
function authFetch(url, options) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {});
  if (authToken) {
    headers["Authorization"] = "Bearer " + authToken;
  }
  return fetch(url, Object.assign({}, opts, { headers: headers }));
}

// ---------------------------------------------------------------- 消息工具
// chrome.tabs.sendMessage 的 Promise 包装：receiver 不存在时抛错。
function sendMessageToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (response) => {
      const lastError = chrome.runtime.lastError;
      if (lastError) {
        reject(new Error(lastError.message || "content script 未就绪"));
        return;
      }
      resolve(response);
    });
  });
}

// ---------------------------------------------------------------- content 注入
// activeTab 权限下动态注入 content.js（幂等：脚本内部有注入标记）。
async function ensureContentScript(tabId) {
  try {
    await sendMessageToTab(tabId, { type: "WEB_CLIP_PING" });
    return; // 已注入
  } catch (_err) {
    // 未注入 → 动态注入
    await chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ["content.js"],
    });
  }
}

// ---------------------------------------------------------------- 页面采集
async function extractCurrentPage() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || tab.id === undefined) {
    throw new Error("未找到当前标签页");
  }
  currentTabId = tab.id;

  const tabUrl = tab.url || "";
  if (!/^https?:\/\//i.test(tabUrl)) {
    throw new Error("当前页面不支持剪藏（仅 http/https 网页，当前为：" + (tabUrl || "空") + "）");
  }

  await ensureContentScript(tab.id);
  const response = await sendMessageToTab(tab.id, { type: "WEB_CLIP_EXTRACT" });
  if (!response || !response.ok) {
    throw new Error("正文提取失败");
  }

  const rawText = String(response.raw_text || "");
  if (rawText.trim().length === 0) {
    throw new Error("未提取到正文内容（页面可能为空或纯脚本渲染）");
  }

  currentPage = {
    url: String(response.url || tabUrl),
    title: String(response.title || tab.title || ""),
    raw_text: rawText,
  };
}

function renderPageInfo() {
  if (!currentPage) {
    return;
  }
  els.pageTitle.textContent = currentPage.title || "（无标题）";
  els.pageUrl.textContent = currentPage.url;
  els.textLength.textContent = currentPage.raw_text.length.toLocaleString("en-US") + " chars";
  // AI 问答 Tab 同步当前页面标题
  els.askPageTitle.textContent = currentPage.title || "（无标题）";
}

// ---------------------------------------------------------------- 剪藏调用
async function clipCurrentPage() {
  if (!currentPage) {
    return;
  }
  if (!authToken) {
    setState("error", "未连接", "请先在上方输入 API Key 并点击「连接」");
    return;
  }
  setClipEnabled(false);
  setState("loading", "剪藏中...", "正在提交到 " + API_BASE_URL);

  try {
    const response = await authFetch(API_BASE_URL + "/clips", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: currentPage.url,
        title: currentPage.title,
        raw_text: currentPage.raw_text,
      }),
    });

    const data = await response.json().catch(() => null);

    if (response.status === 401) {
      setState("error", "认证已失效", "请重新输入 API Key 连接");
      await invalidateAuth();
      return;
    }

    if (response.ok && response.status === 201 && data) {
      // 保存当前网页的 Document ID（AI 问答「当前网页」模式使用）
      currentDocumentId = data.id;
      setState(
        "success",
        "剪藏成功",
        "Document ID: " + data.id + "\nchunk_count: " + data.chunk_count
      );
    } else {
      // 后端错误：优先取 FastAPI 的 detail（字符串或数组），否则显示 HTTP 状态
      let message = "HTTP " + response.status;
      if (data && typeof data.detail === "string") {
        message = data.detail;
      } else if (data && Array.isArray(data.detail) && data.detail.length > 0) {
        const first = data.detail[0];
        message =
          (first.msg || "参数错误") +
          (first.loc && first.loc.length ? "（字段: " + first.loc.join(".") + "）" : "");
      }
      setState("error", "剪藏失败：" + message);
    }
  } catch (err) {
    setState(
      "error",
      "剪藏失败：无法连接后端",
      "请确认后端已启动（" + API_BASE_URL + "）\n" + String(err && err.message ? err.message : err)
    );
  } finally {
    setClipEnabled(true);
  }
}

// ================================================================ AI 问答
// ---------------------------------------------------------------- Tab 切换
function switchTab(tabName) {
  const isClip = tabName === "clip";
  els.tabClip.classList.toggle("active", isClip);
  els.tabAsk.classList.toggle("active", !isClip);
  els.panelClip.classList.toggle("active", isClip);
  els.panelAsk.classList.toggle("active", !isClip);
}

// ---------------------------------------------------------------- 模式切换
function switchMode(mode) {
  askMode = mode;
  const isCurrent = mode === "current";
  els.modeCurrent.classList.toggle("active", isCurrent);
  els.modeAll.classList.toggle("active", !isCurrent);
  updateSendDisabled();
}

function updateSendDisabled() {
  els.chatSend.disabled = isSending || els.chatTextarea.value.trim().length === 0;
}

// ---------------------------------------------------------------- 消息渲染（DOM API，防 XSS）
function appendMessage(role, text, extra) {
  const msg = document.createElement("div");
  msg.className = "msg " + role;

  const roleLabel = document.createElement("div");
  roleLabel.className = "msg-role";
  roleLabel.textContent = role === "user" ? "你" : "AI";
  msg.appendChild(roleLabel);

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;
  msg.appendChild(bubble);

  // 引用来源卡片（仅 assistant + 有 extra.sources 时）
  if (role === "assistant" && extra && Array.isArray(extra.sources) && extra.sources.length > 0) {
    const sources = document.createElement("div");
    sources.className = "sources";

    const title = document.createElement("div");
    title.className = "sources-title";
    title.textContent = "来源";
    sources.appendChild(title);

    extra.sources.forEach(function (source) {
      const card = document.createElement("div");
      card.className = "source-card";

      const sourceTitle = document.createElement("div");
      sourceTitle.className = "source-title";
      sourceTitle.textContent = source.title || ("Document #" + source.document_id);
      card.appendChild(sourceTitle);

      if (source.url) {
        const link = document.createElement("a");
        link.className = "source-url";
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = source.url;
        link.addEventListener("click", function (event) {
          // 阻止 popup 直接导航，改用 chrome.tabs.create 打开新标签页
          event.preventDefault();
          chrome.tabs.create({ url: source.url });
        });
        card.appendChild(link);
      }

      const score = document.createElement("div");
      score.className = "source-score";
      score.textContent = "相似度: " + Number(source.score).toFixed(4);
      card.appendChild(score);

      sources.appendChild(card);
    });

    msg.appendChild(sources);
  }

  els.chatArea.appendChild(msg);
  els.chatEmpty.style.display = "none";
  scrollChatToBottom();
}

function scrollChatToBottom() {
  els.chatArea.scrollTop = els.chatArea.scrollHeight;
}

// ---------------------------------------------------------------- 提问
async function sendQuestion() {
  const query = els.chatTextarea.value.trim();
  if (!query || isSending) {
    return;
  }
  if (!authToken) {
    appendMessage("assistant", "请先在上方输入 API Key 并点击「连接」。", null);
    return;
  }

  // 「当前网页」模式必须有已剪藏的 Document
  if (askMode === "current" && currentDocumentId === null) {
    appendMessage("assistant", "请先剪藏当前网页后再提问。", null);
    return;
  }

  isSending = true;
  els.chatTextarea.value = "";
  updateSendDisabled();

  appendMessage("user", query, null);

  // Loading 消息
  const loadingMsg = document.createElement("div");
  loadingMsg.className = "msg assistant";
  const loadingBubble = document.createElement("div");
  loadingBubble.className = "msg-bubble loading";
  loadingBubble.textContent = "AI 正在思考...";
  loadingMsg.appendChild(loadingBubble);
  els.chatArea.appendChild(loadingMsg);
  els.chatEmpty.style.display = "none";
  scrollChatToBottom();

  // 请求体：当前网页携带 document_id；全部知识库仅 query（不传 document_id）
  const payload = { query: query };
  if (askMode === "current") {
    payload.document_id = currentDocumentId;
  }

  try {
    const response = await authFetch(API_BASE_URL + "/rag/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json().catch(() => null);

    // 移除 Loading 消息
    loadingMsg.remove();

    if (response.status === 401) {
      appendMessage("assistant", "认证已失效，请在上方重新输入 API Key 连接。", null);
      await invalidateAuth();
      return;
    }

    if (!response.ok) {
      let message = "HTTP " + response.status;
      if (data && typeof data.detail === "string") {
        message = data.detail;
      } else if (data && Array.isArray(data.detail) && data.detail.length > 0) {
        message = String(data.detail[0].msg || "请求失败");
      }
      appendMessage("assistant", "出错了：" + message, null);
      return;
    }

    // Success：Answer + Sources（answer 为空时兜底显示 Empty 提示）
    const answer = data && typeof data.answer === "string" ? data.answer : "";
    const sources = data && Array.isArray(data.sources) ? data.sources : [];
    if (!answer) {
      appendMessage("assistant", "当前内容中没有足够信息回答该问题。", { sources: [] });
    } else {
      appendMessage("assistant", answer, { sources: sources });
    }
  } catch (err) {
    // 移除 Loading 消息
    loadingMsg.remove();
    appendMessage(
      "assistant",
      "无法连接后端，请确认服务已启动（" + API_BASE_URL + "）",
      null
    );
  } finally {
    isSending = false;
    updateSendDisabled();
    els.chatTextarea.focus();
  }
}

// ---------------------------------------------------------------- 初始化
async function init() {
  // Tabs
  els.tabClip.addEventListener("click", function () { switchTab("clip"); });
  els.tabAsk.addEventListener("click", function () { switchTab("ask"); });
  // 模式切换
  els.modeCurrent.addEventListener("click", function () { switchMode("current"); });
  els.modeAll.addEventListener("click", function () { switchMode("all"); });
  // 聊天
  els.chatSend.addEventListener("click", sendQuestion);
  els.chatTextarea.addEventListener("input", updateSendDisabled);
  els.chatTextarea.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendQuestion();
    }
  });

  // 剪藏
  els.clipBtn.addEventListener("click", clipCurrentPage);

  // 认证（Phase 3.4 Step 4）
  els.authBtn.addEventListener("click", authenticate);
  els.authApiKey.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      authenticate();
    }
  });

  setClipEnabled(false);
  setState("ready", "正在读取当前页面...");
  await loadAuth();
  try {
    await extractCurrentPage();
    renderPageInfo();
    if (authToken) {
      setState("ready", "准备剪藏", "正文提取完成，点击下方按钮提交");
      setClipEnabled(true);
    } else {
      setState("ready", "等待连接", "请输入 API Key 并点击「连接」后开始剪藏");
      setClipEnabled(false);
    }
  } catch (err) {
    setState("error", "读取页面失败", String(err && err.message ? err.message : err));
    els.pageTitle.textContent = "—";
    els.pageUrl.textContent = "—";
    els.textLength.textContent = "—";
  }
}

document.addEventListener("DOMContentLoaded", init);

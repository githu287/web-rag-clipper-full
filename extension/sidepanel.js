// sidepanel.js —— Web RAG Clipper Side Panel 主界面逻辑（Phase 3.4 Step F8 Step 2）
// 目标：Tab/Session 隔离 + 长对话体验 + 认证/剪藏/RAG 链路无回归。
// 安全：AI 内容一律 textContent 渲染，禁止 innerHTML 拼接 answer。
"use strict";

const els = {
  viewAuth: document.getElementById("view-auth"),
  authTabLogin: document.getElementById("auth-tab-login"),
  authTabRegister: document.getElementById("auth-tab-register"),
  formLogin: document.getElementById("form-login"),
  formRegister: document.getElementById("form-register"),
  loginUsername: document.getElementById("login-username"),
  loginPassword: document.getElementById("login-password"),
  loginBtn: document.getElementById("login-btn"),
  loginStatus: document.getElementById("login-status"),
  regUsername: document.getElementById("reg-username"),
  regPassword: document.getElementById("reg-password"),
  regPassword2: document.getElementById("reg-password2"),
  regBtn: document.getElementById("reg-btn"),
  regStatus: document.getElementById("reg-status"),
  viewApp: document.getElementById("view-app"),
  warnBanner: document.getElementById("warn-banner"),
  warnGotoSettings: document.getElementById("warn-goto-settings"),
  newSessionBtn: document.getElementById("new-session-btn"),
  headerSettingsBtn: document.getElementById("header-settings-btn"),
  navCurrent: document.getElementById("nav-current"),
  navAll: document.getElementById("nav-all"),
  navComing: document.getElementById("nav-coming"),
  navSettings: document.getElementById("nav-settings"),
  viewChat: document.getElementById("view-chat"),
  viewSettings: document.getElementById("view-settings"),
  viewComing: document.getElementById("view-coming"),
  pageContext: document.getElementById("page-context"),
  pcTitle: document.getElementById("pc-title"),
  pcUrl: document.getElementById("pc-url"),
  pcStatus: document.getElementById("pc-status"),
  pcStale: document.getElementById("pc-stale"),
  clipBtn: document.getElementById("clip-btn"),
  allBanner: document.getElementById("all-banner"),
  chatArea: document.getElementById("chat-area"),
  chatEmpty: document.getElementById("chat-empty"),
  backToBottom: document.getElementById("back-to-bottom"),
  chatTextarea: document.getElementById("chat-textarea"),
  chatSend: document.getElementById("chat-send"),
  accountUsername: document.getElementById("account-username"),
  accountUserId: document.getElementById("account-user-id"),
  accountStatus: document.getElementById("account-status"),
  logoutBtn: document.getElementById("logout-btn"),
  modelStatus: document.getElementById("model-status"),
  apiKeyForm: document.getElementById("api-key-form"),
  apiKeyInput: document.getElementById("api-key-input"),
  apiKeySaveBtn: document.getElementById("api-key-save-btn"),
  apiKeyConfigBtn: document.getElementById("api-key-config-btn"),
  apiKeyRemoveBtn: document.getElementById("api-key-remove-btn"),
  apiKeyStatus: document.getElementById("api-key-status"),
};

// ApiRequestError 由 api-client.js 顶层 class 声明，此处禁止重复声明（会与 api-client.js 冲突导致解析失败）。
// 需要时使用 webRagApiClient.ApiRequestError。
const SCROLL_THRESHOLD = 120;
const LONG_ANSWER_CHARS = 600;

let currentTabId = null;
let binding = null;
let session = null;
let isSending = false;
let clipBusy = false;
let clipErrorMsg = null;
let currentView = "chat";
let authBusy = false;
let apiKeyBusy = false;

// ================================================================ 工具
async function getCurrentTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs.length > 0 ? tabs[0] : null;
}

function setStatus(el, text, type) {
  el.textContent = text;
  if (type) {
    el.classList.remove("ok", "err", "warn");
    el.classList.add(type);
  }
}

// ================================================================ 视图切换
function renderAuthView() {
  els.viewAuth.hidden = false;
  els.viewApp.hidden = true;
  currentView = "auth";
}

function renderAppView() {
  els.viewAuth.hidden = true;
  els.viewApp.hidden = false;
  renderWarnBanner();
  renderSettings();
  updateNav();
  updateSendState();
}

function switchAuthTab(mode) {
  const login = mode === "login";
  els.authTabLogin.classList.toggle("active", login);
  els.authTabRegister.classList.toggle("active", !login);
  els.formLogin.hidden = !login;
  els.formRegister.hidden = login;
  setStatus(els.loginStatus, "", null);
  setStatus(els.regStatus, "", null);
}

function switchView(viewName) {
  currentView = viewName;
  els.viewChat.classList.toggle("active", viewName === "chat");
  els.viewSettings.classList.toggle("active", viewName === "settings");
  els.viewComing.classList.toggle("active", viewName === "coming");
  if (viewName === "chat") {
    renderChat();
    renderPageContext();
  }
  if (viewName === "settings") renderSettings();
  updateNav();
  updateSendState();
}

function updateNav() {
  if (currentView === "auth") return;
  const mode = binding ? binding.mode : "current";
  els.navCurrent.classList.toggle("active", currentView === "chat" && mode === "current");
  els.navAll.classList.toggle("active", currentView === "chat" && mode === "all");
  els.navComing.classList.toggle("active", currentView === "coming");
  els.navSettings.classList.toggle("active", currentView === "settings");
}

// ================================================================ 认证
async function login() {
  if (authBusy) return;
  const username = els.loginUsername.value.trim();
  const password = els.loginPassword.value;
  if (!username || !password) {
    setStatus(els.loginStatus, "请输入用户名和密码", "err");
    return;
  }
  authBusy = true;
  els.loginBtn.disabled = true;
  els.loginBtn.textContent = "登录中...";
  setStatus(els.loginStatus, "", null);
  try {
    const data = await webRagApiClient.auth.login(username, password);
    await completeAuth(data);
  } catch (err) {
    setStatus(els.loginStatus, errorText(err), "err");
  } finally {
    authBusy = false;
    els.loginBtn.disabled = false;
    els.loginBtn.textContent = "登录";
  }
}

async function register() {
  if (authBusy) return;
  const username = els.regUsername.value.trim();
  const password = els.regPassword.value;
  const password2 = els.regPassword2.value;
  if (!username || !password) {
    setStatus(els.regStatus, "请输入用户名和密码", "err");
    return;
  }
  if (password.length < 8) {
    setStatus(els.regStatus, "密码至少 8 位", "err");
    return;
  }
  if (password !== password2) {
    setStatus(els.regStatus, "两次输入的密码不一致", "err");
    return;
  }
  authBusy = true;
  els.regBtn.disabled = true;
  els.regBtn.textContent = "注册中...";
  setStatus(els.regStatus, "", null);
  try {
    const data = await webRagApiClient.auth.register(username, password);
    await completeAuth(data);
  } catch (err) {
    setStatus(els.regStatus, errorText(err), "err");
  } finally {
    authBusy = false;
    els.regBtn.disabled = false;
    els.regBtn.textContent = "注册并登录";
  }
}

async function completeAuth(data) {
  webRagApiClient.setAuthDetails({ token: data.token, userId: data.user_id });
  try {
    const me = await webRagApiClient.users.me();
    webRagApiClient.setAuthDetails({
      username: me.username,
      userId: me.user_id,
      apiKeyConfigured: me.api_key_configured,
    });
  } catch (_err) {
    webRagApiClient.setAuthDetails({ apiKeyConfigured: false });
  }
  await webRagApiClient.persistAuth();
  renderAppView();
  const tab = await getCurrentTab();
  if (tab && tab.id != null) {
    await loadTabContext(tab.id);
  }
}

async function logout() {
  const auth = webRagApiClient.getAuth();
  const userId = auth.userId;
  try {
    await webRagApiClient.auth.logout();
  } catch (_err) {
    // 后端失败也继续本地清理
  }
  await webRagApiClient.clearAuth();
  if (userId != null) {
    await sessionStore.clearTabBindingsByUser(userId);
  }
  binding = null;
  session = null;
  currentTabId = null;
  isSending = false;
  clipBusy = false;
  renderAuthView();
  switchAuthTab("login");
  setStatus(els.loginStatus, "已退出登录", "ok");
}

// ================================================================ Tab 上下文
async function findSessionByUrl(userId, url) {
  const sessions = await sessionStore.getSessionsByUser(userId);
  for (const s of sessions) {
    if (!s.messages) continue;
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].pageUrl === url) return s;
    }
  }
  return null;
}

async function restoreOrCreateBinding(tab, userId, tabId) {
  const url = tab && /^https?:/.test(tab.url || "") ? tab.url : null;
  const title = tab && tab.title ? tab.title : null;
  if (url) {
    const found = await findSessionByUrl(userId, url);
    if (found) {
      const b = {
        userId: userId,
        sessionId: found.sessionId,
        documentId: null, // 浏览器重启后不恢复旧 documentId（无法确认仍有效）
        pageUrl: url,
        pageTitle: title,
        mode: "current",
        stale: false,
        updatedAt: Date.now(),
      };
      await sessionStore.setTabBinding(tabId, b);
      return b;
    }
  }
  const created = await sessionStore.createSession(userId, {
    title: title ? "当前网页 · " + title : "新会话",
  });
  const b = {
    userId: userId,
    sessionId: created.sessionId,
    documentId: null,
    pageUrl: url,
    pageTitle: title,
    mode: "current",
    stale: false,
    updatedAt: Date.now(),
  };
  await sessionStore.setTabBinding(tabId, b);
  return b;
}

async function loadTabContext(tabId) {
  const auth = webRagApiClient.getAuth();
  if (!auth.token) {
    renderAuthView();
    return;
  }
  currentTabId = tabId;
  let b = await sessionStore.getTabBinding(tabId);
  if (!b || b.userId !== auth.userId) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    b = await restoreOrCreateBinding(tab, auth.userId, tabId);
  }
  binding = b;
  let s = await sessionStore.getSession(b.sessionId);
  if (!s || s.userId !== auth.userId) {
    s = await sessionStore.createSession(auth.userId, { title: "新会话" });
    binding.sessionId = s.sessionId;
    binding.documentId = null;
    binding.stale = false;
    await sessionStore.setTabBinding(tabId, binding);
  }
  session = s;
  if (currentView === "chat") {
    renderChat();
    renderPageContext();
  }
  updateNav();
  updateSendState();
  renderWarnBanner();
}

async function refreshContextFromStorage() {
  if (currentTabId == null) return;
  const b = await sessionStore.getTabBinding(currentTabId);
  if (!b) {
    binding = null;
    session = null;
    renderPageContext();
    renderChat();
    updateSendState();
    return;
  }
  binding = b;
  const s = await sessionStore.getSession(b.sessionId);
  if (s) session = s;
  renderPageContext();
  renderChat();
  updateNav();
  updateSendState();
}

// ================================================================ 模式与页面上下文
async function switchMode(mode) {
  if (!binding || currentTabId == null) return;
  if (binding.mode === mode) {
    switchView("chat");
    return;
  }
  binding.mode = mode;
  await sessionStore.setTabBinding(currentTabId, binding);
  if (currentView !== "chat") switchView("chat");
  renderPageContext();
  renderChat();
  updateNav();
  updateSendState();
}

function renderPageContext() {
  if (!binding) return;
  els.pageContext.hidden = binding.mode !== "current";
  els.allBanner.hidden = binding.mode !== "all";
  if (binding.mode !== "current") return;
  els.pcTitle.textContent = binding.pageTitle || "（无标题）";
  els.pcUrl.textContent = binding.pageUrl || "—";
  els.pcStale.hidden = !binding.stale;
  els.clipBtn.disabled = clipBusy;
  if (binding.stale) {
    setStatus(els.pcStatus, "页面已变化，请重新剪藏", "warn");
    els.clipBtn.textContent = "重新剪藏";
  } else if (clipBusy) {
    setStatus(els.pcStatus, "处理中…", null);
    els.clipBtn.textContent = "剪藏当前网页";
  } else if (binding.documentId != null) {
    setStatus(els.pcStatus, "✓ 已剪藏（Document #" + binding.documentId + "）", "ok");
    els.clipBtn.textContent = "重新剪藏";
  } else if (clipErrorMsg) {
    setStatus(els.pcStatus, "剪藏失败：" + clipErrorMsg, "err");
    els.clipBtn.textContent = "剪藏当前网页";
  } else {
    setStatus(els.pcStatus, "未剪藏", null);
    els.clipBtn.textContent = "剪藏当前网页";
  }
}

// ================================================================ 聊天渲染
function renderChat() {
  els.chatArea.innerHTML = "";
  if (!session || !session.messages || session.messages.length === 0) {
    els.chatEmpty.style.display = "";
    els.chatEmpty.textContent = emptyText();
    return;
  }
  els.chatEmpty.style.display = "none";
  for (const m of session.messages) {
    appendMessageToDom(m);
  }
  scrollToBottom(true);
}

function emptyText() {
  if (!binding) return "暂无对话";
  if (binding.mode === "current") return "请先剪藏当前网页";
  return "当前知识库中暂无可用内容";
}

function appendMessageToDom(message) {
  if (!message || typeof message !== "object") return;
  const msg = document.createElement("div");
  msg.className = "msg " + (message.role === "user" ? "user" : "assistant");

  const meta = document.createElement("div");
  meta.className = "msg-meta";
  if (message.role === "user") {
    meta.textContent = "你";
  } else {
    const modeText = message.mode === "all" ? "全部知识库" : "当前网页";
    meta.textContent = "Web RAG · " + modeText;
  }
  msg.appendChild(meta);

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = message.content || "";
  msg.appendChild(bubble);

  if (message.role === "assistant" && Array.isArray(message.sources) && message.sources.length > 0) {
    msg.appendChild(buildSources(message.sources));
  }

  if (message.role === "assistant" && typeof message.content === "string" && message.content.length > LONG_ANSWER_CHARS) {
    bubble.classList.add("long");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "msg-expand";
    toggle.textContent = "展开全文";
    let expanded = false;
    toggle.addEventListener("click", function () {
      expanded = !expanded;
      bubble.classList.toggle("expanded", expanded);
      toggle.textContent = expanded ? "收起" : "展开全文";
      if (expanded) scrollToBottom(false);
    });
    msg.appendChild(toggle);
  }

  els.chatArea.appendChild(msg);
}

function buildSources(sources) {
  const wrap = document.createElement("div");
  wrap.className = "sources";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "sources-head";
  head.textContent = "来源 (" + sources.length + ") ▼";
  const list = document.createElement("div");
  list.className = "sources-list";
  list.hidden = true;
  let expanded = false;
  sources.forEach(function (s) {
    if (!s || typeof s !== "object") return;
    const card = document.createElement("div");
    card.className = "source-card";
    if (typeof s.title === "string" && s.title) {
      const st = document.createElement("div");
      st.className = "source-title";
      st.textContent = s.title;
      card.appendChild(st);
    }
    if (typeof s.url === "string" && s.url) {
      const link = document.createElement("a");
      link.className = "source-url";
      link.href = s.url;
      link.textContent = s.url;
      link.addEventListener("click", function (e) {
        e.preventDefault();
        chrome.tabs.create({ url: s.url });
      });
      card.appendChild(link);
    }
    if (s.score != null) {
      const score = document.createElement("div");
      score.className = "source-score";
      score.textContent = "相关度：" + Number(s.score).toFixed(2);
      card.appendChild(score);
    }
    list.appendChild(card);
  });
  head.addEventListener("click", function () {
    expanded = !expanded;
    list.hidden = !expanded;
    head.textContent = "来源 (" + sources.length + ") " + (expanded ? "▲" : "▼");
    if (expanded) scrollToBottom(false);
  });
  wrap.appendChild(head);
  wrap.appendChild(list);
  return wrap;
}

function appendLoading() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = "Web RAG";
  el.appendChild(meta);
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble loading";
  bubble.textContent = "AI 正在思考…";
  el.appendChild(bubble);
  els.chatArea.appendChild(el);
  scrollToBottom(false);
  return el;
}

function removeLoading(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

function showInlineHint(text) {
  els.chatEmpty.style.display = "none";
  const el = document.createElement("div");
  el.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.style.borderColor = "#f59e0b";
  bubble.style.color = "#92400e";
  bubble.textContent = text;
  el.appendChild(bubble);
  els.chatArea.appendChild(el);
  scrollToBottom(false);
}

// ================================================================ 滚动
function scrollToBottom(force) {
  if (!els.chatArea) return;
  const nearBottom =
    els.chatArea.scrollHeight - els.chatArea.scrollTop - els.chatArea.clientHeight < SCROLL_THRESHOLD;
  if (force || nearBottom) {
    els.chatArea.scrollTop = els.chatArea.scrollHeight;
    els.backToBottom.hidden = true;
  } else {
    els.backToBottom.hidden = false;
  }
}

// ================================================================ 会话
async function saveCurrentSession() {
  if (!session) return;
  const firstUser = session.messages.find(function (m) {
    return m.role === "user";
  });
  if (firstUser && (!session.title || session.title === "新会话" || session.title.indexOf("当前网页") === 0)) {
    const t = firstUser.content.trim();
    session.title = t.length > 30 ? t.slice(0, 30) + "…" : t;
  }
  const ok = await sessionStore.saveSession(session);
  if (!ok) {
    // 写失败不崩溃，仅记录
    try {
      console.error("[web-rag-clipper] session save failed");
    } catch (_err) {}
  }
}

async function newSession() {
  const auth = webRagApiClient.getAuth();
  if (!auth.token || currentTabId == null) return;
  const created = await sessionStore.createSession(auth.userId, { title: "新会话" });
  if (binding) {
    binding = Object.assign({}, binding, {
      sessionId: created.sessionId,
      documentId: null,
      stale: false,
      updatedAt: Date.now(),
    });
  } else {
    binding = {
      userId: auth.userId,
      sessionId: created.sessionId,
      documentId: null,
      pageUrl: null,
      pageTitle: null,
      mode: "current",
      stale: false,
      updatedAt: Date.now(),
    };
  }
  await sessionStore.setTabBinding(currentTabId, binding);
  session = created;
  switchView("chat");
  els.chatTextarea.value = "";
  renderPageContext();
  renderChat();
  updateSendState();
}

// ================================================================ 提问
async function sendQuestion() {
  const auth = webRagApiClient.getAuth();
  if (!auth.token) {
    renderAuthView();
    return;
  }
  if (isSending || !binding || !session) return;
  const query = els.chatTextarea.value.trim();
  if (!query) return;
  const mode = binding.mode;
  if (mode === "current" && (!binding.documentId || binding.stale)) {
    showInlineHint("请先剪藏当前网页后再提问");
    return;
  }
  isSending = true;
  els.chatTextarea.value = "";
  updateSendState();

  const userMsg = {
    id: sessionStore.newId(),
    role: "user",
    content: query,
    mode: mode,
    documentId: mode === "current" ? binding.documentId : null,
    pageUrl: binding.pageUrl || null,
    pageTitle: binding.pageTitle || null,
    sources: null,
    createdAt: Date.now(),
  };
  session.messages.push(userMsg);
  await saveCurrentSession();
  appendMessageToDom(userMsg);

  const loadingEl = appendLoading();
  try {
    const data = await webRagApiClient.rag.ask({
      query: query,
      document_id: mode === "current" && binding.documentId != null ? binding.documentId : null,
    });
    removeLoading(loadingEl);
    const answer = data && typeof data.answer === "string" ? data.answer : "";
    const assistantMsg = {
      id: sessionStore.newId(),
      role: "assistant",
      content: answer || "当前内容中没有足够信息回答该问题。",
      mode: mode,
      documentId: mode === "current" ? binding.documentId : null,
      pageUrl: binding.pageUrl || null,
      pageTitle: binding.pageTitle || null,
      sources: Array.isArray(data && data.sources) ? data.sources : [],
      createdAt: Date.now(),
    };
    session.messages.push(assistantMsg);
    await saveCurrentSession();
    appendMessageToDom(assistantMsg);
  } catch (err) {
    removeLoading(loadingEl);
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理视图
      isSending = false;
      updateSendState();
      return;
    }
    const text = errorText(err);
    const errorMsg = {
      id: sessionStore.newId(),
      role: "assistant",
      content: text,
      mode: mode,
      documentId: null,
      pageUrl: null,
      pageTitle: null,
      sources: null,
      createdAt: Date.now(),
    };
    session.messages.push(errorMsg);
    await saveCurrentSession();
    appendMessageToDom(errorMsg);
  } finally {
    isSending = false;
    updateSendState();
    try {
      els.chatTextarea.focus();
    } catch (_err) {}
  }
}

// ================================================================ 剪藏
async function extractCurrentPage() {
  const tab = await chrome.tabs.get(currentTabId).catch(() => null);
  if (!tab || tab.id == null || !/^https?:/.test(tab.url || "")) {
    throw new Error("当前标签页不是可访问的网页");
  }
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (_err) {}
  const response = await chrome.tabs.sendMessage(tab.id, { type: "WEB_CLIP_EXTRACT" });
  if (!response || response.ok !== true || typeof response.raw_text !== "string") {
    throw new Error("页面内容提取失败，请刷新页面后重试");
  }
  return {
    url: response.url || tab.url || "",
    title: response.title || tab.title || "",
    raw_text: response.raw_text,
  };
}

async function clipCurrentPage() {
  const auth = webRagApiClient.getAuth();
  if (!auth.token) {
    renderAuthView();
    return;
  }
  if (clipBusy) return;
  if (!binding && currentTabId != null) {
    await loadTabContext(currentTabId);
  }
  if (!binding) return;
  clipBusy = true;
  clipErrorMsg = null;
  renderPageContext();
  try {
    const page = await extractCurrentPage();
    const data = await webRagApiClient.clips.clip({
      url: page.url,
      title: page.title,
      raw_text: page.raw_text,
    });
    if (data && data.id != null) {
      binding.documentId = Number(data.id);
      binding.stale = false;
      binding.pageUrl = page.url;
      binding.pageTitle = page.title;
      await sessionStore.setTabBinding(currentTabId, binding);
      try {
        chrome.runtime.sendMessage({
          type: "WEB_RAG_CLIP_COMPLETED",
          tabId: currentTabId,
          documentId: binding.documentId,
        });
      } catch (_err) {}
    } else {
      clipErrorMsg = "后端未返回 document id";
    }
  } catch (err) {
    clipErrorMsg = err instanceof webRagApiClient.ApiRequestError ? err.message : err && err.message ? err.message : "未知错误";
  } finally {
    clipBusy = false;
    renderPageContext();
    updateSendState();
  }
}

// ================================================================ 错误文案
function errorText(err) {
  if (err instanceof webRagApiClient.ApiRequestError) {
    switch (err.code) {
      case "UNAUTHENTICATED":
        return "登录已失效，请重新登录";
      case "DISABLED":
        return "账号已被禁用，请联系管理员";
      case "API_KEY_NOT_CONFIGURED":
        return "请前往设置配置阿里云百炼 API Key";
      case "NETWORK":
        return "网络错误，请重试";
      case "BAD_CREDENTIALS":
        return "用户名或密码错误";
      case "USERNAME_EXISTS":
        return "用户名已存在，请直接登录";
      default:
        return err.message ? "出错了：" + err.message : "出错了，请重试";
    }
  }
  return "网络错误，请重试";
}

// ================================================================ 设置
function renderSettings() {
  const auth = webRagApiClient.getAuth();
  els.accountUsername.textContent = auth.username || "—";
  els.accountUserId.textContent = auth.userId != null ? String(auth.userId) : "—";
  els.accountStatus.textContent = auth.token ? "已登录" : "未登录";
  if (auth.apiKeyConfigured) {
    els.modelStatus.textContent = "✓ 已配置";
    els.modelStatus.className = "model-status ok";
    els.apiKeyConfigBtn.textContent = "更换 API Key";
    els.apiKeyRemoveBtn.hidden = false;
  } else {
    els.modelStatus.textContent = "⚠ 尚未配置";
    els.modelStatus.className = "model-status warn";
    els.apiKeyConfigBtn.textContent = "配置 API Key";
    els.apiKeyRemoveBtn.hidden = true;
  }
}

function renderWarnBanner() {
  const auth = webRagApiClient.getAuth();
  els.warnBanner.hidden = !(auth.token && auth.apiKeyConfigured === false);
}

function toggleApiKeyForm(show) {
  els.apiKeyForm.hidden = !show;
  if (show) {
    els.apiKeyInput.value = "";
    els.apiKeyInput.focus();
  }
}

function setApiKeyStatus(text, type) {
  els.apiKeyStatus.textContent = text;
  if (type) {
    els.apiKeyStatus.classList.remove("ok", "err");
    els.apiKeyStatus.classList.add(type);
  }
}

async function saveApiKey() {
  if (apiKeyBusy) return;
  const apiKey = els.apiKeyInput.value.trim();
  if (!apiKey) {
    setApiKeyStatus("请输入 API Key", "err");
    return;
  }
  if (!/^sk-/.test(apiKey)) {
    setApiKeyStatus("API Key 无效，请确认你使用的是阿里云百炼 DashScope API Key。", "err");
    return;
  }
  apiKeyBusy = true;
  els.apiKeySaveBtn.disabled = true;
  els.apiKeySaveBtn.textContent = "保存中...";
  setApiKeyStatus("正在验证 API Key…", null);
  try {
    await webRagApiClient.users.updateApiKey(apiKey);
    els.apiKeyInput.value = ""; // Key 只在内存短暂存在，提交后立即清空
    toggleApiKeyForm(false);
    let me = null;
    try {
      me = await webRagApiClient.users.me();
    } catch (_err) {}
    if (me) {
      webRagApiClient.setAuthDetails({
        username: me.username,
        userId: me.user_id,
        apiKeyConfigured: me.api_key_configured,
      });
    } else {
      webRagApiClient.setAuthDetails({ apiKeyConfigured: true });
    }
    await webRagApiClient.persistAuth();
    renderSettings();
    renderWarnBanner();
    updateSendState();
    setApiKeyStatus("API Key 配置成功", "ok");
  } catch (err) {
    els.apiKeyInput.value = "";
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已自动登出
    } else if (err instanceof webRagApiClient.ApiRequestError && (err.status === 400 || err.status === 422)) {
      setApiKeyStatus("API Key 无效，请确认你使用的是阿里云百炼 DashScope API Key。", "err");
    } else {
      setApiKeyStatus(err instanceof webRagApiClient.ApiRequestError ? err.message : "保存失败，请稍后重试", "err");
    }
  } finally {
    apiKeyBusy = false;
    els.apiKeySaveBtn.disabled = false;
    els.apiKeySaveBtn.textContent = "保存";
  }
}

async function removeApiKey() {
  if (apiKeyBusy) return;
  apiKeyBusy = true;
  els.apiKeyRemoveBtn.disabled = true;
  setApiKeyStatus("正在移除…", null);
  try {
    await webRagApiClient.users.removeApiKey();
    webRagApiClient.setAuthDetails({ apiKeyConfigured: false });
    await webRagApiClient.persistAuth();
    renderSettings();
    renderWarnBanner();
    updateSendState();
    setApiKeyStatus("已移除 API Key", "ok");
  } catch (err) {
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已自动登出
    } else {
      setApiKeyStatus(err instanceof webRagApiClient.ApiRequestError ? err.message : "移除失败，请稍后重试", "err");
    }
  } finally {
    apiKeyBusy = false;
    els.apiKeyRemoveBtn.disabled = false;
  }
}

// ================================================================ 发送请求（按钮 click 与 Enter 共用）
function handleSendRequest() {
  const auth = webRagApiClient.getAuth();
  if (!auth.token) {
    renderAuthView();
    return;
  }
  if (isSending) return;
  if (auth.apiKeyConfigured === false) {
    showInlineHint("请先在「设置」中配置阿里云百炼 API Key");
    switchView("settings");
    return;
  }
  if (binding && binding.mode === "current" && (!binding.documentId || binding.stale)) {
    showInlineHint("请先剪藏当前网页后再提问");
    return;
  }
  sendQuestion();
}

// ================================================================ 发送状态
function updateSendState() {
  const auth = webRagApiClient.getAuth();
  let disabled = !auth.token || isSending;
  if (!disabled && auth.apiKeyConfigured === false) disabled = true;
  if (!disabled && binding && binding.mode === "current") {
    if (!binding.documentId || binding.stale) disabled = true;
  }
  els.chatSend.disabled = disabled;
}

// ================================================================ 事件绑定
function bindEvents() {
  els.authTabLogin.addEventListener("click", function () {
    switchAuthTab("login");
  });
  els.authTabRegister.addEventListener("click", function () {
    switchAuthTab("register");
  });
  els.formLogin.addEventListener("submit", function (e) {
    e.preventDefault();
    login();
  });
  els.formRegister.addEventListener("submit", function (e) {
    e.preventDefault();
    register();
  });

  els.navCurrent.addEventListener("click", function () {
    switchMode("current");
  });
  els.navAll.addEventListener("click", function () {
    switchMode("all");
  });
  els.navSettings.addEventListener("click", function () {
    switchView("settings");
  });
  els.navComing.addEventListener("click", function () {
    switchView("coming");
  });
  els.headerSettingsBtn.addEventListener("click", function () {
    switchView("settings");
  });
  els.warnGotoSettings.addEventListener("click", function () {
    switchView("settings");
  });
  els.newSessionBtn.addEventListener("click", function () {
    newSession();
  });

  els.clipBtn.addEventListener("click", function () {
    clipCurrentPage();
  });

  els.chatSend.addEventListener("click", function () {
    handleSendRequest();
  });
  els.chatTextarea.addEventListener("input", function () {
    updateSendState();
  });
  els.chatTextarea.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSendRequest();
    }
  });
  els.chatArea.addEventListener("scroll", function () {
    const nearBottom =
      els.chatArea.scrollHeight - els.chatArea.scrollTop - els.chatArea.clientHeight < SCROLL_THRESHOLD;
    els.backToBottom.hidden = nearBottom;
  });
  els.backToBottom.addEventListener("click", function () {
    els.chatArea.scrollTop = els.chatArea.scrollHeight;
    els.backToBottom.hidden = true;
  });

  els.logoutBtn.addEventListener("click", function () {
    logout();
  });
  els.apiKeyConfigBtn.addEventListener("click", function () {
    toggleApiKeyForm(true);
  });
  els.apiKeySaveBtn.addEventListener("click", function () {
    saveApiKey();
  });
  els.apiKeyRemoveBtn.addEventListener("click", function () {
    removeApiKey();
  });
  els.apiKeyInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      saveApiKey();
    }
  });
}

// ================================================================ background 广播
function bindRuntimeMessages() {
  chrome.runtime.onMessage.addListener(function (message, _sender, _sendResponse) {
    if (!message || typeof message.type !== "string") return;
    if (message.type === "WEB_RAG_TAB_ACTIVATED") {
      if (message.tabId !== currentTabId) {
        loadTabContext(message.tabId);
      }
    } else if (message.type === "WEB_RAG_TAB_URL_CHANGED") {
      if (message.tabId === currentTabId) {
        refreshContextFromStorage();
      }
    } else if (message.type === "WEB_RAG_TAB_REMOVED") {
      if (message.tabId === currentTabId) {
        getCurrentTab().then(function (tab) {
          if (tab && tab.id != null) loadTabContext(tab.id);
        });
      }
    } else if (message.type === "WEB_RAG_CLIP_COMPLETED") {
      if (message.tabId === currentTabId) {
        refreshContextFromStorage();
      }
    }
  });
}

// ================================================================ 初始化
async function init() {
  bindEvents();
  bindRuntimeMessages();
  webRagApiClient.setUnauthenticatedHandler(async function (ctx) {
    if (ctx && ctx.userId != null) {
      await sessionStore.clearTabBindingsByUser(ctx.userId);
    }
    binding = null;
    session = null;
    currentTabId = null;
    isSending = false;
    clipBusy = false;
    renderAuthView();
    switchAuthTab("login");
    setStatus(els.loginStatus, "登录已失效，请重新登录", "err");
  });
  await webRagApiClient.loadAuth();
  const auth = webRagApiClient.getAuth();
  if (auth.token) {
    renderAppView();
    const tab = await getCurrentTab();
    if (tab && tab.id != null) {
      await loadTabContext(tab.id);
    }
  } else {
    renderAuthView();
    switchAuthTab("login");
  }
}

init();

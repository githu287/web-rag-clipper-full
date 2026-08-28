// sidepanel.js —— Web RAG Clipper Side Panel 主界面逻辑（Phase 3.5 Step 2-F）
// 目标：Tab/Session 隔离 + 长对话体验 + Plugin Workspace 双凭证（X-Plugin-ID/X-Plugin-Secret）无回归。
// 安全：AI 内容一律 textContent 渲染，禁止 innerHTML 拼接 answer。
"use strict";

const els = {
  viewWelcome: document.getElementById("view-welcome"),
  viewBlocked: document.getElementById("view-blocked"),
  formRegisterPlugin: document.getElementById("form-register-plugin"),
  pluginNameInput: document.getElementById("plugin-name-input"),
  pluginRegisterBtn: document.getElementById("plugin-register-btn"),
  welcomeStatus: document.getElementById("welcome-status"),
  viewApp: document.getElementById("view-app"),
  warnBanner: document.getElementById("warn-banner"),
  warnGotoSettings: document.getElementById("warn-goto-settings"),
  newSessionBtn: document.getElementById("new-session-btn"),
  headerSettingsBtn: document.getElementById("header-settings-btn"),
  navClip: document.getElementById("nav-clip"),
  navChat: document.getElementById("nav-chat"),
  navLibrary: document.getElementById("nav-library"),
  navSettings: document.getElementById("nav-settings"),
  viewChat: document.getElementById("view-chat"),
  viewClip: document.getElementById("view-clip"),
  viewSettings: document.getElementById("view-settings"),
  viewLibrary: document.getElementById("view-library"),
  scopeCurrent: document.getElementById("scope-current"),
  scopeAll: document.getElementById("scope-all"),
  scopeDesc: document.getElementById("scope-desc"),
  clipTitle: document.getElementById("clip-title"),
  clipUrl: document.getElementById("clip-url"),
  clipStatus: document.getElementById("clip-status"),
  clipStale: document.getElementById("clip-stale"),
  clipBtn: document.getElementById("clip-btn"),
  gotoChatBtn: document.getElementById("goto-chat-btn"),
  librarySearchInput: document.getElementById("library-search-input"),
  libraryStatusFilter: document.getElementById("library-status-filter"),
  librarySourceFilter: document.getElementById("library-source-filter"),
  libraryRefreshBtn: document.getElementById("library-refresh-btn"),
  librarySummary: document.getElementById("library-summary"),
  libraryList: document.getElementById("library-list"),
  libraryState: document.getElementById("library-state"),
  libraryPager: document.getElementById("library-pager"),
  libraryPrevBtn: document.getElementById("library-prev-btn"),
  libraryNextBtn: document.getElementById("library-next-btn"),
  libraryPages: document.getElementById("library-pages"),
  libraryDeleteModal: document.getElementById("library-delete-modal"),
  libraryDeleteText: document.getElementById("library-delete-text"),
  libraryDeleteCancelBtn: document.getElementById("library-delete-cancel-btn"),
  libraryDeleteConfirmBtn: document.getElementById("library-delete-confirm-btn"),
  libraryDeleteStatus: document.getElementById("library-delete-status"),
  chatArea: document.getElementById("chat-area"),
  chatEmpty: document.getElementById("chat-empty"),
  backToBottom: document.getElementById("back-to-bottom"),
  chatTextarea: document.getElementById("chat-textarea"),
  chatSend: document.getElementById("chat-send"),
  pluginNameField: document.getElementById("plugin-name-field"),
  pluginNameSaveBtn: document.getElementById("plugin-name-save-btn"),
  pluginNameStatus: document.getElementById("plugin-name-status"),
  pluginIdField: document.getElementById("plugin-id-field"),
  pluginAuthStatus: document.getElementById("plugin-auth-status"),
  deletePluginBtn: document.getElementById("delete-plugin-btn"),
  deleteModal: document.getElementById("delete-modal"),
  deleteConfirmName: document.getElementById("delete-confirm-name"),
  deleteCancelBtn: document.getElementById("delete-cancel-btn"),
  deleteConfirmBtn: document.getElementById("delete-confirm-btn"),
  deleteStatus: document.getElementById("delete-status"),
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
let registerBusy = false;
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
function renderWelcomeView() {
  els.viewWelcome.hidden = false;
  els.viewBlocked.hidden = true;
  els.viewApp.hidden = true;
  currentView = "welcome";
}

function renderBlockedView() {
  els.viewWelcome.hidden = true;
  els.viewBlocked.hidden = false;
  els.viewApp.hidden = true;
  currentView = "blocked";
}

function renderAppView() {
  els.viewWelcome.hidden = true;
  els.viewBlocked.hidden = true;
  els.viewApp.hidden = false;
  renderWarnBanner();
  renderSettings();
  updateNav();
  updateSendState();
}

function switchView(viewName) {
  currentView = viewName;
  els.viewChat.classList.toggle("active", viewName === "chat");
  els.viewClip.classList.toggle("active", viewName === "clip");
  els.viewSettings.classList.toggle("active", viewName === "settings");
  els.viewLibrary.classList.toggle("active", viewName === "library");
  if (viewName === "chat") {
    renderChat();
    updateScopeSelector();
  }
  if (viewName === "clip") renderClipView();
  if (viewName === "settings") renderSettings();
  if (viewName === "library") loadLibrary();
  updateNav();
  updateSendState();
}

function updateNav() {
  if (currentView === "welcome" || currentView === "blocked") return;
  els.navClip.classList.toggle("active", currentView === "clip");
  els.navChat.classList.toggle("active", currentView === "chat");
  els.navLibrary.classList.toggle("active", currentView === "library");
  els.navSettings.classList.toggle("active", currentView === "settings");
  updateScopeDesc();
}

function updateScopeSelector() {
  const mode = binding ? binding.mode : "current";
  els.scopeCurrent.classList.toggle("active", mode === "current");
  els.scopeAll.classList.toggle("active", mode === "all");
  updateScopeDesc();
}

function updateScopeDesc() {
  if (!els.scopeDesc) return;
  const mode = binding ? binding.mode : "current";
  if (currentView === "chat") {
    els.scopeDesc.textContent = mode === "all"
      ? "检索范围：你的全部已剪藏文档"
      : "检索范围：当前剪藏的网页";
  } else {
    els.scopeDesc.textContent = "";
  }
}

// ================================================================ 我的知识库（Phase 3.6 Step 2-C）
const LIBRARY_PAGE_SIZE = 20;
const libraryState = {
  page: 1,
  total: 0,
  pages: 0,
  items: [],
  loading: false,
  searchTimer: null,
  deletePendingId: null,
  deleteBusy: false,
};

// 显示中间状态（Loading / Empty / Search Empty / Error），隐藏列表与分页。
// 内容一律 textContent / createElement 渲染，禁止 innerHTML 拼接用户数据。
function showLibraryState(text, withRetry) {
  els.libraryState.hidden = false;
  els.libraryState.replaceChildren();
  els.libraryState.appendChild(document.createTextNode(text || ""));
  if (withRetry) {
    const retryBtn = document.createElement("button");
    retryBtn.type = "button";
    retryBtn.className = "btn-secondary";
    retryBtn.textContent = "重试";
    retryBtn.addEventListener("click", function () {
      loadLibrary();
    });
    els.libraryState.appendChild(retryBtn);
  }
  els.libraryList.hidden = true;
  els.libraryPager.hidden = true;
  els.librarySummary.textContent = "";
}

async function loadLibrary() {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  if (libraryState.loading) return;
  libraryState.loading = true;
  showLibraryState("正在加载知识库…", false);
  const params = { page: libraryState.page, page_size: LIBRARY_PAGE_SIZE };
  const keyword = els.librarySearchInput.value.trim();
  if (keyword) params.keyword = keyword;
  const status = els.libraryStatusFilter.value;
  if (status) params.status = status;
  const sourceType = els.librarySourceFilter.value;
  if (sourceType) params.source_type = sourceType;
  try {
    const data = await webRagApiClient.documents.list(params);
    libraryState.total = data.total || 0;
    libraryState.pages = data.pages || 0;
    libraryState.items = data.items || [];
    libraryState.loading = false;
    renderLibrary();
  } catch (err) {
    libraryState.loading = false;
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理视图
      return;
    }
    showLibraryState("知识库加载失败", true);
  }
}

function renderLibrary() {
  const keyword = els.librarySearchInput.value.trim();
  const hasFilter = !!(
    keyword ||
    els.libraryStatusFilter.value ||
    els.librarySourceFilter.value
  );
  if (libraryState.total === 0) {
    showLibraryState(hasFilter ? "没有找到匹配的文档" : "还没有剪藏或上传任何文档", false);
    return;
  }
  els.libraryState.hidden = true;
  els.libraryList.hidden = false;
  els.libraryList.replaceChildren();
  els.librarySummary.textContent = "共 " + libraryState.total + " 个文档";
  for (const doc of libraryState.items) {
    els.libraryList.appendChild(buildDocCard(doc));
  }
  renderLibraryPager();
}

function buildDocCard(doc) {
  const card = document.createElement("div");
  card.className = "doc-card";

  const title = document.createElement("div");
  title.className = "doc-title";
  title.textContent = doc.title || doc.filename || "（无标题）";
  card.appendChild(title);

  const meta = document.createElement("div");
  meta.className = "doc-meta";
  const sourceLabel = doc.source_type === "webpage" ? "网页" : "上传";
  meta.textContent = doc.url || ("来源：" + sourceLabel);
  card.appendChild(meta);

  const statusEl = document.createElement("div");
  statusEl.className = "doc-status " + statusClass(doc.status);
  statusEl.textContent = statusText(doc.status);
  card.appendChild(statusEl);

  const detail = document.createElement("div");
  detail.className = "doc-meta";
  detail.textContent = "chunks: " + (doc.chunk_count || 0) + " · " + formatLibraryTime(doc.created_at);
  card.appendChild(detail);

  const actions = document.createElement("div");
  actions.className = "doc-actions";
  if (doc.status === "SUCCESS") {
    actions.appendChild(makeCardButton("问答", false, function () {
      askDocumentFromLibrary(doc);
    }));
    if (doc.url) {
      actions.appendChild(makeCardButton("打开网页", false, function () {
        openDocumentUrl(doc.url);
      }));
    }
    actions.appendChild(makeCardButton("删除", true, function () {
      openLibraryDeleteModal(doc);
    }));
  } else if (doc.status === "FAILED") {
    actions.appendChild(makeCardButton("重试", false, function () {
      retryDocument(doc);
    }));
    actions.appendChild(makeCardButton("删除", true, function () {
      openLibraryDeleteModal(doc);
    }));
  } else {
    const pending = document.createElement("span");
    pending.className = "doc-meta";
    pending.textContent = "处理中";
    actions.appendChild(pending);
  }
  card.appendChild(actions);
  return card;
}

function makeCardButton(text, danger, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  if (danger) button.className = "danger";
  button.addEventListener("click", onClick);
  return button;
}

function statusText(status) {
  switch (status) {
    case "SUCCESS":
      return "SUCCESS";
    case "FAILED":
      return "FAILED";
    case "PENDING":
      return "待处理";
    case "PROCESSING":
      return "处理中";
    case "DELETING":
      return "删除中";
    default:
      return status || "未知";
  }
}

function statusClass(status) {
  switch (status) {
    case "SUCCESS":
      return "ok";
    case "FAILED":
      return "err";
    case "PROCESSING":
    case "PENDING":
    case "DELETING":
      return "warn";
    default:
      return "idle";
  }
}

function formatLibraryTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const pad = function (n) { return n < 10 ? "0" + n : String(n); };
  return (
    d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
  );
}

function renderLibraryPager() {
  const totalPages = libraryState.pages;
  if (totalPages <= 1) {
    els.libraryPager.hidden = true;
    return;
  }
  els.libraryPager.hidden = false;
  els.libraryPrevBtn.disabled = libraryState.page <= 1;
  els.libraryNextBtn.disabled = libraryState.page >= totalPages;
  els.libraryPages.replaceChildren();
  const max = 5;
  let start = Math.max(1, libraryState.page - 2);
  let end = Math.min(totalPages, start + max - 1);
  start = Math.max(1, end - max + 1);
  let prev = 0;
  for (let n = start; n <= end; n++) {
    if (n > prev + 1) {
      const dot = document.createElement("span");
      dot.className = "page-num ellipsis";
      dot.textContent = "…";
      els.libraryPages.appendChild(dot);
    }
    const pageBtn = document.createElement("button");
    pageBtn.type = "button";
    pageBtn.className = "page-num" + (n === libraryState.page ? " current" : "");
    pageBtn.textContent = String(n);
    if (n !== libraryState.page) {
      pageBtn.addEventListener("click", function () {
        goLibraryPage(n);
      });
    }
    els.libraryPages.appendChild(pageBtn);
    prev = n;
  }
}

async function goLibraryPage(page) {
  if (page < 1 || page > libraryState.pages || page === libraryState.page) return;
  libraryState.page = page;
  await loadLibrary();
}

function onLibrarySearchInput() {
  if (libraryState.searchTimer) {
    clearTimeout(libraryState.searchTimer);
  }
  libraryState.searchTimer = setTimeout(function () {
    libraryState.searchTimer = null;
    libraryState.page = 1;
    loadLibrary();
  }, 400);
}

function onLibraryFilterChange() {
  libraryState.page = 1;
  loadLibrary();
}

function openLibraryDeleteModal(doc) {
  libraryState.deletePendingId = doc.id;
  libraryState.deleteBusy = false;
  const label = doc.title || doc.filename || ("文档 #" + doc.id);
  els.libraryDeleteText.textContent = "删除后无法恢复，确认删除？\n" + label;
  setStatus(els.libraryDeleteStatus, "", null);
  els.libraryDeleteConfirmBtn.disabled = false;
  els.libraryDeleteConfirmBtn.textContent = "确认删除";
  els.libraryDeleteModal.hidden = false;
}

function closeLibraryDeleteModal() {
  els.libraryDeleteModal.hidden = true;
  libraryState.deletePendingId = null;
}

async function confirmLibraryDelete() {
  if (libraryState.deletePendingId == null || libraryState.deleteBusy) return;
  libraryState.deleteBusy = true;
  els.libraryDeleteConfirmBtn.disabled = true;
  els.libraryDeleteConfirmBtn.textContent = "删除中...";
  setStatus(els.libraryDeleteStatus, "", null);
  try {
    await webRagApiClient.documents.delete(libraryState.deletePendingId);
    closeLibraryDeleteModal();
    if (libraryState.items.length === 1 && libraryState.page > 1) {
      libraryState.page -= 1;
    }
    await loadLibrary();
  } catch (err) {
    libraryState.deleteBusy = false;
    els.libraryDeleteConfirmBtn.disabled = false;
    els.libraryDeleteConfirmBtn.textContent = "确认删除";
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理视图
      return;
    }
    setStatus(els.libraryDeleteStatus, errorText(err), "err");
  }
}

// 从「我的知识库」选择文档进入「当前网页」问答：
// 仅修改当前激活 Tab 的 binding（documentId + mode=current + stale=false），
// 不创建 session、不修改其他 Tab。
async function askDocumentFromLibrary(doc) {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  const tab = await getCurrentTab();
  if (!tab || tab.id == null) return;
  if (currentTabId !== tab.id || !binding) {
    await loadTabContext(tab.id);
  }
  if (!binding) return;
  binding.documentId = Number(doc.id);
  binding.mode = "current";
  binding.stale = false;
  if (doc.url) binding.pageUrl = doc.url;
  if (doc.title) binding.pageTitle = doc.title;
  binding.updatedAt = Date.now();
  await sessionStore.setTabBinding(tab.id, binding);
  switchView("chat");
}

function openDocumentUrl(url) {
  if (typeof url !== "string" || !url) return;
  chrome.tabs.create({ url: url });
}

// Phase 3.6 Step 2：现有 POST /documents/{id}/ingest 的 chunks（min_length=1）仅由
// 后端在剪藏/上传时写入 Milvus，知识库列表前端没有可复用的 chunks 数据源。
// 按实施指令：不猜测构造 chunks、不擅自新增后端「重新切分」逻辑，
// 因此从知识库无法直接重试，仅提示用户重新剪藏/上传（详见完成报告）。
function retryDocument(_doc) {
  setStatus(els.librarySummary, "该文档缺少原文 chunks，无法从知识库直接重试；请重新剪藏或上传。", "warn");
}

// ================================================================ Plugin 注册 / 设置 / 删除
function validatePluginName(name) {
  if (typeof name !== "string") return "请输入插件名称";
  const trimmed = name.trim();
  if (trimmed.length < 2) return "插件名称至少 2 个字符";
  if (trimmed.length > 32) return "插件名称最多 32 个字符";
  if (!/^[\u4e00-\u9fa5A-Za-z0-9 _.\-]+$/.test(trimmed)) {
    return "插件名称仅支持中文、字母、数字、空格、-、_、.";
  }
  return null;
}

async function registerPlugin() {
  if (registerBusy) return;
  const name = els.pluginNameInput.value.trim();
  const errMsg = validatePluginName(name);
  if (errMsg) {
    setStatus(els.welcomeStatus, errMsg, "err");
    return;
  }
  registerBusy = true;
  els.pluginRegisterBtn.disabled = true;
  els.pluginRegisterBtn.textContent = "创建中...";
  setStatus(els.welcomeStatus, "", null);
  try {
    const data = await webRagApiClient.plugins.register(name);
    webRagApiClient.setPluginDetails({
      pluginId: data.plugin_id,
      pluginSecret: data.plugin_secret,
      pluginName: data.plugin_name,
      apiKeyConfigured: false,
    });
    await webRagApiClient.persistPlugin();
    renderAppView();
    const tab = await getCurrentTab();
    if (tab && tab.id != null) {
      await loadTabContext(tab.id);
      renderChat();
    }
  } catch (err) {
    setStatus(els.welcomeStatus, errorText(err), "err");
  } finally {
    registerBusy = false;
    els.pluginRegisterBtn.disabled = false;
    els.pluginRegisterBtn.textContent = "创建插件";
  }
}

async function savePluginName() {
  const name = els.pluginNameField.value.trim();
  const errMsg = validatePluginName(name);
  if (errMsg) {
    setStatus(els.pluginNameStatus, errMsg, "err");
    return;
  }
  els.pluginNameSaveBtn.disabled = true;
  els.pluginNameSaveBtn.textContent = "保存中...";
  setStatus(els.pluginNameStatus, "", null);
  try {
    const data = await webRagApiClient.plugins.updateName(name);
    webRagApiClient.setPluginDetails({
      pluginId: data.plugin_id,
      pluginName: data.plugin_name,
    });
    await webRagApiClient.persistPlugin();
    setStatus(els.pluginNameStatus, "已保存", "ok");
  } catch (err) {
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "PLUGIN_NAME_TAKEN") {
      setStatus(els.pluginNameStatus, "这个插件名称已经被使用，请换一个名称", "err");
    } else if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理视图
    } else {
      setStatus(els.pluginNameStatus, errorText(err), "err");
    }
  } finally {
    els.pluginNameSaveBtn.disabled = false;
    els.pluginNameSaveBtn.textContent = "保存名称";
  }
}

function openDeleteModal() {
  els.deleteModal.hidden = false;
  els.deleteConfirmName.value = "";
  setStatus(els.deleteStatus, "", null);
  els.deleteConfirmName.focus();
}

function closeDeleteModal() {
  els.deleteModal.hidden = true;
}

async function confirmDeletePlugin() {
  const plugin = webRagApiClient.getPlugin();
  const typed = els.deleteConfirmName.value.trim();
  if (!plugin.pluginName || typed !== plugin.pluginName) {
    setStatus(els.deleteStatus, "输入的插件名称与当前插件不一致", "err");
    return;
  }
  els.deleteConfirmBtn.disabled = true;
  els.deleteConfirmBtn.textContent = "删除中...";
  setStatus(els.deleteStatus, "", null);
  try {
    await webRagApiClient.plugins.delete(typed);
    const pluginId = plugin.pluginId;
    await webRagApiClient.clearPlugin();
    if (pluginId != null) {
      await sessionStore.clearTabBindingsByPlugin(pluginId);
    }
    binding = null;
    session = null;
    currentTabId = null;
    isSending = false;
    clipBusy = false;
    closeDeleteModal();
    renderWelcomeView();
    setStatus(els.welcomeStatus, "插件空间已删除", "ok");
  } catch (err) {
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理视图
      closeDeleteModal();
    } else {
      setStatus(els.deleteStatus, errorText(err), "err");
    }
  } finally {
    els.deleteConfirmBtn.disabled = false;
    els.deleteConfirmBtn.textContent = "确认删除";
  }
}

// ================================================================ Tab 上下文（Phase 3.6 Step 2-H 重构）
// 核心原则：Tab 是网页上下文，不是聊天 Session。
// Tab Binding 只负责 { pluginId, documentId, pageUrl, pageTitle, mode, stale }
// 全局 Session 由 currentSessionId 管理，Tab 切换不改变 Session。

// 确保当前 Plugin 有一个全局 Session（首次启动或 Session 被删除时调用）
async function ensureGlobalSession(pluginId) {
  let sid = await sessionStore.getCurrentSessionId(pluginId);
  if (sid) {
    const s = await sessionStore.getSession(sid);
    if (s && s.pluginId === pluginId) return s;
  }
  const created = await sessionStore.createSession(pluginId, { title: "新会话" });
  await sessionStore.setCurrentSessionId(pluginId, created.sessionId);
  await sessionStore.enforceSessionLimit(pluginId, created.sessionId);
  return created;
}

// 根据当前网页 URL 自动检测是否已剪藏（使用 GET /documents 精确匹配 URL）
async function detectClippedDocument(pluginId, pageUrl) {
  if (!pluginId || !pageUrl) return null;
  try {
    const data = await webRagApiClient.documents.list({
      keyword: pageUrl,
      status: "SUCCESS",
      page: 1,
      page_size: 100,
    });
    const items = data.items || [];
    // keyword 是 LIKE 查询，必须精确匹配 url
    const matches = items.filter(function (item) {
      return item.url === pageUrl && item.status === "SUCCESS";
    });
    if (matches.length === 0) return null;
    // 多个匹配时取 created_at 最新的
    matches.sort(function (a, b) {
      return (b.created_at || "").localeCompare(a.created_at || "");
    });
    return matches[0];
  } catch (_err) {
    return null;
  }
}

// 为 Tab 创建或恢复 Binding（只包含网页上下文，不包含 sessionId）
async function restoreOrCreateBinding(tab, pluginId, tabId) {
  const url = tab && /^https?:/.test(tab.url || "") ? tab.url : null;
  const title = tab && tab.title ? tab.title : null;
  const b = {
    pluginId: pluginId,
    documentId: null,
    pageUrl: url,
    pageTitle: title,
    mode: "current",
    stale: false,
    updatedAt: Date.now(),
  };
  // 自动检测当前 URL 是否已剪藏
  if (url) {
    const doc = await detectClippedDocument(pluginId, url);
    if (doc) {
      b.documentId = Number(doc.id);
    }
  }
  await sessionStore.setTabBinding(tabId, b);
  return b;
}

// Tab 切换时调用：只更新网页上下文，不切换 Session
async function loadTabContext(tabId) {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  currentTabId = tabId;
  let b = await sessionStore.getTabBinding(tabId);
  if (!b || b.pluginId !== plugin.pluginId) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    b = await restoreOrCreateBinding(tab, plugin.pluginId, tabId);
  } else {
    // Binding 已存在，但仍需重新检测当前 URL 是否已剪藏
    // （用户可能在其他 Tab 剪藏了该 URL，或文档被删除）
    if (b.pageUrl && !b.stale) {
      const doc = await detectClippedDocument(plugin.pluginId, b.pageUrl);
      b.documentId = doc ? Number(doc.id) : null;
      await sessionStore.setTabBinding(tabId, b);
    }
  }
  binding = b;

  // 确保有全局 Session（不根据 Tab 切换 Session）
  const s = await ensureGlobalSession(plugin.pluginId);
  session = s;

  // 更新剪藏视图和发送状态
  if (currentView === "chat" || currentView === "clip") {
    renderClipView();
    updateSendState();
  }
  updateNav();
  renderWarnBanner();
}

// URL 变化或剪藏完成后刷新上下文
async function refreshContextFromStorage() {
  if (currentTabId == null) return;
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) return;
  const b = await sessionStore.getTabBinding(currentTabId);
  if (!b) {
    binding = null;
    renderClipView();
    updateSendState();
    return;
  }
  binding = b;
  // 不重新加载 Session（全局 Session 不变）
  renderClipView();
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
  updateScopeSelector();
  renderChat();
  updateNav();
  updateSendState();
}

function renderClipView() {
  if (!binding) return;
  els.clipTitle.textContent = binding.pageTitle || "（无标题）";
  els.clipUrl.textContent = binding.pageUrl || "—";
  els.clipStale.hidden = !binding.stale;
  els.clipBtn.disabled = clipBusy;
  const hasDoc = binding.documentId != null && !binding.stale;
  els.gotoChatBtn.hidden = !hasDoc;
  if (binding.stale) {
    setStatus(els.clipStatus, "页面已变化，请重新剪藏", "warn");
    els.clipBtn.textContent = "重新剪藏";
  } else if (clipBusy) {
    setStatus(els.clipStatus, "处理中…", null);
    els.clipBtn.textContent = "剪藏当前网页";
  } else if (binding.documentId != null) {
    setStatus(els.clipStatus, "✓ 已剪藏（Document #" + binding.documentId + "）", "ok");
    els.clipBtn.textContent = "重新剪藏";
  } else if (clipErrorMsg) {
    setStatus(els.clipStatus, "剪藏失败：" + clipErrorMsg, "err");
    els.clipBtn.textContent = "剪藏当前网页";
  } else {
    setStatus(els.clipStatus, "未剪藏", null);
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
  let prevMode = null;
  for (const m of session.messages) {
    const curMode = m.mode === "all" ? "all" : "current";
    if (prevMode !== null && curMode !== prevMode) {
      appendModeSeparatorToDom(curMode);
    }
    appendMessageToDom(m);
    prevMode = curMode;
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
  const msgMode = message.mode === "all" ? "all" : "current";
  msg.setAttribute("data-mode", msgMode);

  const meta = document.createElement("div");
  meta.className = "msg-meta";
  const modeText = msgMode === "all" ? "全部知识库" : "当前网页";
  if (message.role === "user") {
    meta.textContent = "你 · " + modeText;
  } else {
    meta.textContent = "Web RAG · " + modeText;
  }
  msg.appendChild(meta);

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = message.content || "";
  msg.appendChild(bubble);

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

  if (message.role === "assistant" && Array.isArray(message.sources) && message.sources.length > 0) {
    msg.appendChild(buildSources(message.sources));
  }

  els.chatArea.appendChild(msg);
}

function appendModeSeparatorToDom(mode) {
  const sep = document.createElement("div");
  sep.className = "mode-separator";
  const label = document.createElement("span");
  label.className = "mode-separator-label";
  label.textContent = "当前检索范围：" + (mode === "all" ? "全部知识库" : "当前网页");
  sep.appendChild(label);
  els.chatArea.appendChild(sep);
}

function buildSources(sources) {
  const wrap = document.createElement("div");
  wrap.className = "sources";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "sources-head";
  head.setAttribute("aria-expanded", "false");
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
    head.setAttribute("aria-expanded", String(expanded));
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
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) return;
  const created = await sessionStore.createSession(plugin.pluginId, { title: "新会话" });
  await sessionStore.setCurrentSessionId(plugin.pluginId, created.sessionId);
  await sessionStore.enforceSessionLimit(plugin.pluginId, created.sessionId);
  session = created;
  // Tab binding 不再包含 sessionId，只重置 documentId 和 stale
  if (binding) {
    binding.documentId = null;
    binding.stale = false;
    if (currentTabId != null) {
      await sessionStore.setTabBinding(currentTabId, binding);
    }
  }
  switchView("chat");
  els.chatTextarea.value = "";
  renderClipView();
  renderChat();
  updateSendState();
}

// ================================================================ 提问
async function sendQuestion() {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
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
  const curMode = mode === "all" ? "all" : "current";
  const lastMsgEl = els.chatArea.querySelector(".msg:last-of-type");
  if (lastMsgEl && lastMsgEl.getAttribute("data-mode") !== curMode) {
    appendModeSeparatorToDom(curMode);
  }
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
/**
 * 带超时的 chrome.tabs.sendMessage 封装。
 * 作用：避免 content.js 注入失败或未响应时导致 Promise 永久 pending，
 * 剪藏按钮一直 loading。
 * @param {number} tabId 目标标签页 ID
 * @param {object} message 发送的消息对象
 * @param {number} timeoutMs 超时毫秒数，默认 10 秒
 * @returns {Promise<any>} content.js 返回的响应
 */
function sendMessageWithTimeout(tabId, message, timeoutMs) {
  const timeout = timeoutMs || 10000;
  return new Promise(function (resolve, reject) {
    const timer = setTimeout(function () {
      reject(new Error("页面响应超时，请刷新网页后重试"));
    }, timeout);
    try {
      chrome.tabs.sendMessage(tabId, message, function (response) {
        clearTimeout(timer);
        // chrome.runtime.lastError 通常在「没有接收端（content.js 未注入）」时设置
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message || "页面内容脚本未就绪，请刷新网页后重试"));
          return;
        }
        resolve(response);
      });
    } catch (err) {
      clearTimeout(timer);
      reject(err);
    }
  });
}

/**
 * 从当前激活 Tab 提取网页正文。
 * 作用：1) 校验 Tab 可访问性；2) 注入 content.js；3) 请求正文提取；4) 返回结构化数据。
 * @returns {Promise<{url: string, title: string, raw_text: string}>} 提取结果
 */
async function extractCurrentPage() {
  const tab = await chrome.tabs.get(currentTabId).catch(() => null);
  if (!tab || tab.id == null || !/^https?:/.test(tab.url || "")) {
    throw new Error("当前标签页不是可访问的网页");
  }
  // 尝试注入 content.js（幂等：content.js 内有 __WEB_RAG_CLIPPER_INJECTED__ 守卫）。
  // 此处不再静默吞掉错误：如果因权限/CSP 导致注入失败，直接向用户暴露明确原因。
  try {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (injectErr) {
    const msg = injectErr && injectErr.message ? injectErr.message : "未知注入错误";
    // Chrome 常见注入失败文案包含「Cannot access」「permission」等，统一翻译为用户可读提示
    if (/cannot\s+access/i.test(msg) || /permission/i.test(msg)) {
      throw new Error("当前网页暂不支持剪藏（浏览器权限限制）");
    }
    if (/chrome(-extension)?:\/\//i.test(tab.url || "")) {
      throw new Error("当前网页暂不支持剪藏");
    }
    throw new Error("页面注入失败：" + msg);
  }
  const response = await sendMessageWithTimeout(tab.id, { type: "WEB_CLIP_EXTRACT" }, 10000);
  if (!response || response.ok !== true || typeof response.raw_text !== "string") {
    throw new Error("页面内容提取失败，请刷新页面后重试");
  }
  // 正文长度校验：避免剪藏空白页面（如纯图片站、未渲染的 SPA）
  const trimmed = response.raw_text.trim();
  if (trimmed.length === 0) {
    throw new Error("网页正文为空，请确认页面已加载完成后重试");
  }
  return {
    url: response.url || tab.url || "",
    title: response.title || tab.title || "",
    raw_text: trimmed,
  };
}

async function clipCurrentPage() {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  if (clipBusy) return;
  if (!binding && currentTabId != null) {
    await loadTabContext(currentTabId);
  }
  if (!binding) return;
  clipBusy = true;
  clipErrorMsg = null;
  renderClipView();
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
    renderClipView();
    updateSendState();
  }
}

// ================================================================ 错误文案
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
      case "PLUGIN_NAME_TAKEN":
        return "这个插件名称已经被使用，请换一个名称";
      case "NETWORK":
        return "网络错误，请重试";
      case "BAD_CREDENTIALS":
        return "凭证错误，请重新创建插件";
      default:
        return err.message ? "出错了：" + err.message : "出错了，请重试";
    }
  }
  return "网络错误，请重试";
}

// ================================================================ 设置
function renderSettings() {
  const plugin = webRagApiClient.getPlugin();
  els.pluginNameField.value = plugin.pluginName || "";
  const pid = plugin.pluginId || "";
  els.pluginIdField.textContent = pid.length > 12 ? pid.slice(0, 6) + "…" + pid.slice(-6) : pid || "—";
  els.pluginAuthStatus.textContent = "● 已连接";
  if (plugin.apiKeyConfigured) {
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
  const plugin = webRagApiClient.getPlugin();
  els.warnBanner.hidden = !(plugin.pluginId && plugin.apiKeyConfigured === false);
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
    await webRagApiClient.plugins.updateApiKey(apiKey);
    els.apiKeyInput.value = ""; // Key 只在内存短暂存在，提交后立即清空
    toggleApiKeyForm(false);
    let me = null;
    try {
      me = await webRagApiClient.plugins.me();
    } catch (_err) {}
    if (me) {
      webRagApiClient.setPluginDetails({
        pluginId: me.plugin_id,
        pluginName: me.plugin_name,
        apiKeyConfigured: me.api_key_configured,
      });
    } else {
      webRagApiClient.setPluginDetails({ apiKeyConfigured: true });
    }
    await webRagApiClient.persistPlugin();
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
    await webRagApiClient.plugins.removeApiKey();
    webRagApiClient.setPluginDetails({ apiKeyConfigured: false });
    await webRagApiClient.persistPlugin();
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
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  if (isSending) return;
  if (plugin.apiKeyConfigured === false) {
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
  const plugin = webRagApiClient.getPlugin();
  let disabled = !plugin.pluginId || isSending;
  if (!disabled && plugin.apiKeyConfigured === false) disabled = true;
  if (!disabled && binding && binding.mode === "current") {
    if (!binding.documentId || binding.stale) disabled = true;
  }
  els.chatSend.disabled = disabled;
}

// ================================================================ 事件绑定
function bindEvents() {
  els.formRegisterPlugin.addEventListener("submit", function (e) {
    e.preventDefault();
    registerPlugin();
  });

  els.navClip.addEventListener("click", function () {
    switchView("clip");
  });
  els.navChat.addEventListener("click", function () {
    switchView("chat");
  });
  els.scopeCurrent.addEventListener("click", function () {
    switchMode("current");
  });
  els.scopeAll.addEventListener("click", function () {
    switchMode("all");
  });
  els.gotoChatBtn.addEventListener("click", function () {
    switchView("chat");
  });
  els.navSettings.addEventListener("click", function () {
    switchView("settings");
  });
  els.navLibrary.addEventListener("click", function () {
    switchView("library");
  });
  els.librarySearchInput.addEventListener("input", onLibrarySearchInput);
  els.libraryStatusFilter.addEventListener("change", onLibraryFilterChange);
  els.librarySourceFilter.addEventListener("change", onLibraryFilterChange);
  els.libraryRefreshBtn.addEventListener("click", function () {
    loadLibrary();
  });
  els.libraryPrevBtn.addEventListener("click", function () {
    goLibraryPage(libraryState.page - 1);
  });
  els.libraryNextBtn.addEventListener("click", function () {
    goLibraryPage(libraryState.page + 1);
  });
  els.libraryDeleteCancelBtn.addEventListener("click", closeLibraryDeleteModal);
  els.libraryDeleteConfirmBtn.addEventListener("click", confirmLibraryDelete);
  els.libraryDeleteModal.addEventListener("click", function (e) {
    if (e.target === els.libraryDeleteModal) closeLibraryDeleteModal();
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

  els.pluginNameSaveBtn.addEventListener("click", function () {
    savePluginName();
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
  els.deletePluginBtn.addEventListener("click", function () {
    openDeleteModal();
  });
  els.deleteCancelBtn.addEventListener("click", function () {
    closeDeleteModal();
  });
  els.deleteConfirmBtn.addEventListener("click", function () {
    confirmDeletePlugin();
  });
  els.deleteConfirmName.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      confirmDeletePlugin();
    }
  });
  els.deleteModal.addEventListener("click", function (e) {
    if (e.target === els.deleteModal) closeDeleteModal();
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

// ================================================================ 初始化（Plugin Workspace 状态机）
async function validatePlugin() {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  try {
    const me = await webRagApiClient.plugins.me();
    webRagApiClient.setPluginDetails({
      pluginId: me.plugin_id,
      pluginName: me.plugin_name,
      apiKeyConfigured: me.api_key_configured,
    });
    await webRagApiClient.persistPlugin();
    renderAppView();
    const tab = await getCurrentTab();
    if (tab && tab.id != null) {
      await loadTabContext(tab.id);
      // 初始加载时渲染聊天历史（Tab 切换时不重新渲染）
      renderChat();
    }
  } catch (err) {
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "PLUGIN_DISABLED") {
      // 插件被禁用：保留本地身份，仅显示禁用视图（不清 secret、不创建新 workspace）
      renderBlockedView();
      return;
    }
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已由 unauthenticatedHandler 处理：clearPlugin + 清 tabBindings + renderWelcomeView
      return;
    }
    // 网络错误等：保留本地身份，回 Welcome 并允许重试
    renderWelcomeView();
    setStatus(els.welcomeStatus, "无法连接服务器，请检查网络后重试", "err");
  }
}

async function init() {
  bindEvents();
  bindRuntimeMessages();
  webRagApiClient.setUnauthenticatedHandler(async function (ctx) {
    if (ctx && ctx.pluginId != null) {
      await sessionStore.clearTabBindingsByPlugin(ctx.pluginId);
    }
    binding = null;
    session = null;
    currentTabId = null;
    isSending = false;
    clipBusy = false;
    renderWelcomeView();
    setStatus(els.welcomeStatus, "插件凭证已失效，请重新创建插件", "err");
  });
  await webRagApiClient.loadPlugin();
  await validatePlugin();
}

init();

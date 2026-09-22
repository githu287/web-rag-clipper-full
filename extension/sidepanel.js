// sidepanel.js —— Web RAG Clipper Side Panel 主界面逻辑（Phase 3.5 Step 2-F）
// 目标：Tab/Session 隔离 + 长对话体验 + Plugin Workspace 双凭证（X-Plugin-ID/X-Plugin-Secret）无回归。
// 安全：AI 内容只通过 textContent / createElement 渲染，禁止 innerHTML 拼接 answer。
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
  clipJob: document.getElementById("clip-job"),
  clipJobStage: document.getElementById("clip-job-stage"),
  clipJobProgress: document.getElementById("clip-job-progress"),
  clipJobBar: document.getElementById("clip-job-bar"),
  clipJobError: document.getElementById("clip-job-error"),
  clipJobRetryBtn: document.getElementById("clip-job-retry-btn"),
  clipPreview: document.getElementById("clip-preview"),
  clipPreviewCount: document.getElementById("clip-preview-count"),
  clipPreviewDiagnostics: document.getElementById("clip-preview-diagnostics"),
  clipPreviewTitle: document.getElementById("clip-preview-title"),
  clipPreviewText: document.getElementById("clip-preview-text"),
  clipPreviewCancelBtn: document.getElementById("clip-preview-cancel-btn"),
  clipPreviewRefreshBtn: document.getElementById("clip-preview-refresh-btn"),
  clipPreviewSaveBtn: document.getElementById("clip-preview-save-btn"),
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
  libraryUploadBtn: document.getElementById("library-upload-btn"),
  libraryFileInput: document.getElementById("library-file-input"),
  uploadStatus: document.getElementById("upload-status"),
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
  embeddingProvider: document.getElementById("embedding-provider"),
  embeddingBaseUrl: document.getElementById("embedding-base-url"),
  embeddingModel: document.getElementById("embedding-model"),
  embeddingApiKey: document.getElementById("embedding-api-key"),
  embeddingSendDimensions: document.getElementById("embedding-send-dimensions"),
  llmProvider: document.getElementById("llm-provider"),
  llmBaseUrl: document.getElementById("llm-base-url"),
  llmModel: document.getElementById("llm-model"),
  llmApiKey: document.getElementById("llm-api-key"),
  apiKeySaveBtn: document.getElementById("api-key-save-btn"),
  apiKeyConfigBtn: document.getElementById("api-key-config-btn"),
  apiKeyRemoveBtn: document.getElementById("api-key-remove-btn"),
  apiKeyStatus: document.getElementById("api-key-status"),
};

// ApiRequestError 由 api-client.js 顶层 class 声明，此处禁止重复声明（会与 api-client.js 冲突导致解析失败）。
// 需要时使用 webRagApiClient.ApiRequestError。
const SCROLL_THRESHOLD = 120;
const LONG_ANSWER_CHARS = 600;
const MAX_UPLOAD_SIZE = 2 * 1024 * 1024;
const ALLOWED_UPLOAD_EXTENSIONS = [".txt", ".md", ".markdown"];
const CLIP_JOB_POLL_INTERVAL_MS = 1500;
const UPLOAD_JOB_POLL_INTERVAL_MS = 1500;

let currentTabId = null;
let binding = null;
let session = null;
let isSending = false;
let clipBusy = false;
let clipBusyAction = null;
let clipErrorMsg = null;
let clipDraft = null;
let clipDraftTabId = null;
let activeClipJob = null;
let clipJobPollToken = 0;
let currentView = "chat";
let registerBusy = false;
let apiKeyBusy = false;
let modelProviderCatalog = null;
let currentModelConfig = null;
let uploadBusy = false;
let activeUploadJob = null;
let uploadJobPollToken = 0;
let uploadResumePending = false;

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
  resumeUploadJobMonitor();
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
  const deletingDocumentId = libraryState.deletePendingId;
  libraryState.deleteBusy = true;
  els.libraryDeleteConfirmBtn.disabled = true;
  els.libraryDeleteConfirmBtn.textContent = "删除中...";
  setStatus(els.libraryDeleteStatus, "", null);
  try {
    await webRagApiClient.documents.delete(deletingDocumentId);
    const plugin = webRagApiClient.getPlugin();
    await sessionStore.clearUploadJob(plugin.pluginId, deletingDocumentId);
    if (
      activeUploadJob &&
      Number(activeUploadJob.document_id) === Number(deletingDocumentId)
    ) {
      uploadJobPollToken += 1;
      activeUploadJob = null;
      uploadBusy = false;
      els.libraryUploadBtn.disabled = false;
    }
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

// 异步上传会保留原文件与 Job 引用，可由知识库直接重试。
// 历史同步失败文档没有 Job 记录，仍需用户重新上传原文件。
async function retryDocument(doc) {
  const plugin = webRagApiClient.getPlugin();
  const record = await sessionStore.getUploadJob(plugin.pluginId, doc.id);
  if (!record || Number(record.documentId) !== Number(doc.id)) {
    setStatus(els.librarySummary, "该文档没有可恢复的异步任务，请重新上传原文件。", "warn");
    return;
  }
  if (uploadBusy) return;
  uploadBusy = true;
  els.libraryUploadBtn.disabled = true;
  setUploadStatus("正在重新提交任务…", "loading");
  try {
    const job = await webRagApiClient.jobs.retry(record.jobId);
    activeUploadJob = job;
    startUploadJobMonitor(record, job);
    await loadLibrary();
  } catch (err) {
    uploadBusy = false;
    els.libraryUploadBtn.disabled = false;
    setUploadStatus("重试失败：" + errorText(err), "err");
  }
}

// ================================================================ 文件上传（Phase 3.6 Step 3）
function triggerFileInput() {
  if (uploadBusy) return;
  els.libraryFileInput.value = "";
  els.libraryFileInput.click();
}

function validateUploadFile(file) {
  if (!file) return "请选择文件";
  var name = file.name || "";
  var dotIndex = name.lastIndexOf(".");
  if (dotIndex < 0) return "不支持的文件类型，仅支持 .txt / .md / .markdown";
  var ext = name.substring(dotIndex).toLowerCase();
  if (ALLOWED_UPLOAD_EXTENSIONS.indexOf(ext) < 0) {
    return "不支持的文件类型（" + ext + "），仅支持 .txt / .md / .markdown";
  }
  if (file.size > MAX_UPLOAD_SIZE) {
    return "文件过大（" + formatFileSize(file.size) + "），最大 2MB";
  }
  if (file.size === 0) return "文件为空，请选择有内容的文件";
  return null;
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(2) + " MB";
}

function setUploadStatus(text, type) {
  els.uploadStatus.textContent = text || "";
  els.uploadStatus.classList.remove("ok", "err", "warn", "loading");
  if (type) els.uploadStatus.classList.add(type);
  els.uploadStatus.hidden = !text;
}

async function handleFileUpload(file) {
  var plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  var errMsg = validateUploadFile(file);
  if (errMsg) {
    setUploadStatus(errMsg, "err");
    return;
  }
  if (uploadBusy) return;
  uploadBusy = true;
  els.libraryUploadBtn.disabled = true;
  setUploadStatus("正在上传 " + file.name + "…", "loading");
  var submitted = false;
  try {
    var job = await webRagApiClient.documents.uploadFileAsync(file);
    if (!job || !job.id || job.document_id == null) {
      throw new Error("后端未返回完整的上传任务");
    }
    var record = {
      jobId: job.id,
      documentId: Number(job.document_id),
      filename: file.name,
    };
    await sessionStore.setUploadJob(plugin.pluginId, record);
    submitted = true;
    activeUploadJob = job;
    startUploadJobMonitor(record, job);
    libraryState.page = 1;
    await loadLibrary();
  } catch (err) {
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      return;
    }
    setUploadStatus("上传失败：" + errorText(err), "err");
  } finally {
    if (!submitted) {
      uploadBusy = false;
      els.libraryUploadBtn.disabled = false;
    }
    els.libraryFileInput.value = "";
  }
}

async function resumeUploadJobMonitor() {
  if (uploadResumePending) return;
  uploadResumePending = true;
  const plugin = webRagApiClient.getPlugin();
  try {
    if (!plugin.pluginId) return;
    const record = await sessionStore.getUploadJob(plugin.pluginId);
    if (!record || !record.jobId) return;
    if (activeUploadJob && activeUploadJob.id === record.jobId) return;
    startUploadJobMonitor(record, null);
  } finally {
    uploadResumePending = false;
  }
}

function startUploadJobMonitor(record, initialJob) {
  const token = ++uploadJobPollToken;
  activeUploadJob = initialJob || { id: record.jobId, status: "QUEUED", progress: 0 };
  uploadBusy = true;
  els.libraryUploadBtn.disabled = true;
  monitorUploadJob(record, token);
}

async function monitorUploadJob(record, token) {
  while (token === uploadJobPollToken) {
    try {
      const job = await webRagApiClient.jobs.get(record.jobId);
      if (token !== uploadJobPollToken) return;
      activeUploadJob = job;
      const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
      if (job.status === "SUCCEEDED") {
        const plugin = webRagApiClient.getPlugin();
        await sessionStore.clearUploadJob(plugin.pluginId, record.documentId);
        activeUploadJob = null;
        uploadBusy = false;
        els.libraryUploadBtn.disabled = false;
        setUploadStatus("上传成功：" + record.filename + "（100%）", "ok");
        libraryState.page = 1;
        await loadLibrary();
        return;
      }
      if (job.status === "FAILED") {
        uploadBusy = false;
        els.libraryUploadBtn.disabled = false;
        setUploadStatus(
          "入库失败：" + (job.error_message || "未知错误") + "（可在文档卡片中重试）",
          "err"
        );
        await loadLibrary();
        return;
      }
      const stage = job.status === "QUEUED" ? "等待 Worker" : "正在解析、切块并向量化";
      setUploadStatus(stage + "：" + record.filename + "（" + progress + "%）", "loading");
    } catch (err) {
      if (token !== uploadJobPollToken) return;
      activeUploadJob = null;
      uploadBusy = false;
      els.libraryUploadBtn.disabled = false;
      setUploadStatus("任务状态查询失败：" + errorText(err) + "，重新打开知识库后会继续。", "err");
      return;
    }
    await waitMilliseconds(UPLOAD_JOB_POLL_INTERVAL_MS);
  }
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
      await sessionStore.clearUploadJob(pluginId);
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
// Tab Binding 只负责网页上下文；pageUrl 保留 Tab 实际 URL，documentUrl
// 保留后端返回的规范化来源 URL。
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

// 根据当前网页 URL 自动检测是否已剪藏。查询前使用与后端对齐的
// 规范化规则，避免锚点或 utm_* 等跟踪参数导致重复文档。
async function detectClippedDocument(pluginId, pageUrl) {
  if (!pluginId || !pageUrl) return null;
  try {
    const normalizedUrl = webRagUrlUtils.normalizeWebUrl(pageUrl);
    const data = await webRagApiClient.documents.list({
      keyword: normalizedUrl,
      status: "SUCCESS",
      page: 1,
      page_size: 100,
    });
    const items = data.items || [];
    // keyword 是 LIKE 查询，必须精确匹配 url
    const matches = items.filter(function (item) {
      return item.url === normalizedUrl && item.status === "SUCCESS";
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

function isSameNormalizedWebUrl(left, right) {
  try {
    return webRagUrlUtils.normalizeWebUrl(left) === webRagUrlUtils.normalizeWebUrl(right);
  } catch (_err) {
    return left === right;
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
  if (currentTabId !== tabId) {
    clipJobPollToken += 1;
    activeClipJob = null;
    clearClipDraft();
    clipErrorMsg = null;
  }
  currentTabId = tabId;
  let b = await sessionStore.getTabBinding(tabId);
  if (!b || b.pluginId !== plugin.pluginId) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    b = await restoreOrCreateBinding(tab, plugin.pluginId, tabId);
  } else {
    // Binding 已存在，但仍需重新检测当前 URL 是否已剪藏
    // （用户可能在其他 Tab 剪藏了该 URL，或文档被删除）
    if (b.pageUrl && !b.stale && !b.ingestJobId) {
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
  if (b.ingestJobId) {
    startClipJobMonitor(b.ingestJobId, tabId);
  }
}

// URL 变化或剪藏完成后刷新上下文
async function refreshContextFromStorage() {
  if (currentTabId == null) return;
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) return;
  const b = await sessionStore.getTabBinding(currentTabId);
  if (!b) {
    clipJobPollToken += 1;
    activeClipJob = null;
    binding = null;
    clearClipDraft();
    renderClipView();
    updateSendState();
    return;
  }
  const activeJobId = activeClipJob && activeClipJob.id;
  if (activeJobId && activeJobId !== b.ingestJobId) {
    clipJobPollToken += 1;
    activeClipJob = null;
  }
  binding = b;
  if (clipDraft && (
    clipDraftTabId !== currentTabId ||
    !isSameNormalizedWebUrl(clipDraft.url, b.pageUrl)
  )) {
    clearClipDraft();
  }
  // 不重新加载 Session（全局 Session 不变）
  renderClipView();
  updateNav();
  updateSendState();
  if (b.ingestJobId && b.ingestJobId !== activeJobId) {
    startClipJobMonitor(b.ingestJobId, currentTabId);
  }
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
  if (!binding) {
    clearClipDraft();
    return;
  }
  els.clipTitle.textContent = binding.pageTitle || "（无标题）";
  els.clipUrl.textContent = binding.pageUrl || "—";
  els.clipStale.hidden = !binding.stale;
  const jobIsActive = !!(
    activeClipJob &&
    (activeClipJob.status === "QUEUED" || activeClipJob.status === "RUNNING")
  );
  els.clipBtn.disabled = clipBusy || jobIsActive;
  renderClipJob();
  els.clipPreview.hidden = !clipDraft;
  if (clipDraft) {
    if (els.clipPreviewTitle.value !== clipDraft.title) {
      els.clipPreviewTitle.value = clipDraft.title;
    }
    if (els.clipPreviewText.value !== clipDraft.raw_text) {
      els.clipPreviewText.value = clipDraft.raw_text;
    }
    renderClipDraftMeta();
  }
  els.clipPreviewTitle.disabled = clipBusy || jobIsActive;
  els.clipPreviewText.disabled = clipBusy || jobIsActive;
  els.clipPreviewCancelBtn.disabled = clipBusy || jobIsActive;
  els.clipPreviewRefreshBtn.disabled = clipBusy || jobIsActive;
  const hasDoc = binding.documentId != null && !binding.stale;
  els.gotoChatBtn.hidden = !hasDoc;
  if (clipBusy) {
    const isSubmitting = clipBusyAction === "save";
    setStatus(els.clipStatus, isSubmitting ? "正在提交入库任务…" : "正在提取网页正文…", null);
    els.clipBtn.textContent = isSubmitting ? "正在提交…" : "正在提取…";
  } else if (jobIsActive) {
    setStatus(els.clipStatus, "已提交，后台正在入库", null);
    els.clipBtn.textContent = "处理中…";
  } else if (activeClipJob && activeClipJob.status === "FAILED") {
    setStatus(els.clipStatus, "入库失败：" + (activeClipJob.error_message || "未知错误"), "err");
    els.clipBtn.textContent = "重新提取";
  } else if (clipErrorMsg) {
    setStatus(els.clipStatus, "剪藏失败：" + clipErrorMsg, "err");
    els.clipBtn.textContent = clipDraft ? "重新提取" : "重试提取";
  } else if (clipDraft) {
    setStatus(els.clipStatus, "已提取，请检查并确认剪藏", null);
    els.clipBtn.textContent = "重新提取";
  } else if (binding.stale) {
    setStatus(els.clipStatus, "页面已变化，请重新剪藏", "warn");
    els.clipBtn.textContent = "提取并预览";
  } else if (binding.documentId != null) {
    setStatus(els.clipStatus, "✓ 已剪藏（Document #" + binding.documentId + "）", "ok");
    els.clipBtn.textContent = "重新提取并预览";
  } else {
    setStatus(els.clipStatus, "未剪藏", null);
    els.clipBtn.textContent = "提取并预览";
  }
}

function renderClipJob() {
  els.clipJob.hidden = !activeClipJob;
  if (!activeClipJob) return;
  const progress = Math.max(0, Math.min(100, Number(activeClipJob.progress) || 0));
  const stageLabels = {
    QUEUED: "等待 Worker 处理",
    INGESTING: "正在切块、向量化并入库",
    COMPLETED: "入库完成",
    FAILED: "入库失败",
  };
  els.clipJobStage.textContent = stageLabels[activeClipJob.stage] || activeClipJob.stage || "处理中";
  els.clipJobProgress.textContent = progress + "%";
  els.clipJobBar.style.width = progress + "%";
  els.clipJobError.textContent = activeClipJob.status === "FAILED"
    ? (activeClipJob.error_message || "未知错误")
    : "";
  els.clipJobRetryBtn.hidden = activeClipJob.status !== "FAILED";
}

function waitMilliseconds(milliseconds) {
  return new Promise(function (resolve) {
    setTimeout(resolve, milliseconds);
  });
}

function startClipJobMonitor(jobId, tabId) {
  const token = ++clipJobPollToken;
  monitorClipJob(jobId, tabId, token);
}

async function monitorClipJob(jobId, tabId, token) {
  while (token === clipJobPollToken && currentTabId === tabId) {
    try {
      const job = await webRagApiClient.jobs.get(jobId);
      if (token !== clipJobPollToken || currentTabId !== tabId) return;
      activeClipJob = job;
      renderClipView();
      if (job.status === "SUCCEEDED") {
        await completeClipJob(job, tabId, token);
        return;
      }
      if (job.status === "FAILED") return;
    } catch (err) {
      if (token !== clipJobPollToken || currentTabId !== tabId) return;
      clipErrorMsg = errorText(err);
      renderClipView();
      return;
    }
    await waitMilliseconds(CLIP_JOB_POLL_INTERVAL_MS);
  }
}

async function completeClipJob(job, tabId, token) {
  if (!job.document_id) {
    clipErrorMsg = "任务完成但未返回 document id";
    renderClipView();
    return;
  }
  const document = await webRagApiClient.documents.get(job.document_id);
  if (token !== clipJobPollToken || currentTabId !== tabId) return;
  const storedBinding = await sessionStore.getTabBinding(tabId);
  if (!storedBinding) return;
  const expectedUrl = storedBinding.ingestJobPageUrl;
  let pageStillMatches = !storedBinding.stale;
  try {
    pageStillMatches = pageStillMatches && !!expectedUrl && (
      webRagUrlUtils.normalizeWebUrl(storedBinding.pageUrl) ===
      webRagUrlUtils.normalizeWebUrl(expectedUrl)
    );
  } catch (_err) {
    pageStillMatches = false;
  }
  storedBinding.ingestJobId = null;
  storedBinding.ingestJobPageUrl = null;
  if (!pageStillMatches) {
    await sessionStore.setTabBinding(tabId, storedBinding);
    binding = storedBinding;
    activeClipJob = null;
    renderClipView();
    return;
  }
  storedBinding.documentId = Number(job.document_id);
  storedBinding.documentUrl = document.url || null;
  storedBinding.stale = false;
  if (document.title) storedBinding.pageTitle = document.title;
  await sessionStore.setTabBinding(tabId, storedBinding);
  binding = storedBinding;
  activeClipJob = null;
  clipErrorMsg = null;
  renderClipView();
  updateSendState();
  try {
    chrome.runtime.sendMessage({
      type: "WEB_RAG_CLIP_COMPLETED",
      tabId: tabId,
      documentId: storedBinding.documentId,
    });
  } catch (_err) {}
}

function clearClipDraft() {
  clipDraft = null;
  clipDraftTabId = null;
  if (els.clipPreview) {
    els.clipPreview.hidden = true;
    els.clipPreviewTitle.value = "";
    els.clipPreviewText.value = "";
    els.clipPreviewCount.textContent = "0 字";
    els.clipPreviewDiagnostics.hidden = true;
    els.clipPreviewDiagnostics.textContent = "";
    delete els.clipPreviewDiagnostics.dataset.warning;
  }
}

function syncClipDraftFromInputs() {
  if (!clipDraft) return;
  clipDraft.title = els.clipPreviewTitle.value.slice(0, 512);
  clipDraft.raw_text = els.clipPreviewText.value;
  renderClipDraftMeta();
}

function renderClipDraftMeta() {
  const text = clipDraft ? clipDraft.raw_text : "";
  const trimmedLength = text.trim().length;
  els.clipPreviewCount.textContent = trimmedLength.toLocaleString("zh-CN") + " 字";
  els.clipPreviewSaveBtn.disabled = clipBusy || trimmedLength === 0;
  renderExtractionDiagnostics(trimmedLength);
}

function renderExtractionDiagnostics(trimmedLength) {
  const diagnostics = clipDraft && clipDraft.diagnostics;
  if (!diagnostics) {
    els.clipPreviewDiagnostics.hidden = true;
    return;
  }
  const sourceCount = Math.max(0, Number(diagnostics.source_char_count) || 0);
  const removedCount = Math.max(0, Number(diagnostics.removed_node_count) || 0);
  const strategy = diagnostics.strategy || "未知区域";
  const warnings = [];
  if (diagnostics.fallback) warnings.push("使用整页降级提取，请重点检查正文");
  if (trimmedLength < 200) warnings.push("提取内容较短");
  if (diagnostics.truncated) warnings.push("超长内容已截断");
  const parts = [
    (diagnostics.fallback ? "降级区域：" : "正文区域：") + strategy,
    "原始 " + sourceCount.toLocaleString("zh-CN") + " 字 → 提取 " + trimmedLength.toLocaleString("zh-CN") + " 字",
    "清理 " + removedCount.toLocaleString("zh-CN") + " 个噪声节点",
  ];
  if (warnings.length > 0) parts.push("⚠ " + warnings.join("；"));
  els.clipPreviewDiagnostics.textContent = parts.join(" · ");
  els.clipPreviewDiagnostics.hidden = false;
  els.clipPreviewDiagnostics.dataset.warning = warnings.length > 0 ? "true" : "false";
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

// 渲染回答中的少量行内 Markdown。所有内容最终都写入 textContent，
// 不把模型输出交给 innerHTML，因此 `<script>` 等文本不会被当作 HTML 执行。
function appendInlineMarkdown(parent, text) {
  const source = String(text || "");
  const tokenPattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let cursor = 0;
  let match;
  while ((match = tokenPattern.exec(source)) !== null) {
    if (match.index > cursor) {
      parent.appendChild(document.createTextNode(source.slice(cursor, match.index)));
    }
    const token = match[0];
    const el = document.createElement(token.startsWith("`") ? "code" : "strong");
    el.textContent = token.startsWith("`") ? token.slice(1, -1) : token.slice(2, -2);
    parent.appendChild(el);
    cursor = match.index + token.length;
  }
  if (cursor < source.length) {
    parent.appendChild(document.createTextNode(source.slice(cursor)));
  }
}

function normalizeAssistantText(content) {
  const normalized = String(content || "")
    .replace(/(?:&#x20;|&#32;|&nbsp;)/gi, " ")
    .replace(/^\\(?=(?:---|___|\*\*\*)\s*$)/gm, "")
    .replace(/^\\>\s?/gm, "> ")
    .replace(/\\([`<>])/g, "$1")
    .replace(/\*{4,}/g, "***");
  const outerMarkdownFence = normalized
    .trim()
    .match(/^```(?:markdown|md)\s*\n([\s\S]*?)\n```$/i);
  return outerMarkdownFence ? outerMarkdownFence[1] : normalized;
}

// 安全的轻量 Markdown：标题、段落、列表、代码块、粗体与行内代码。
// 未识别语法按普通文本显示，避免引入第三方 HTML sanitizer 或 XSS 面。
function renderAssistantMarkdown(container, content) {
  const lines = normalizeAssistantText(content).replace(/\r\n?/g, "\n").split("\n");
  let list = null;
  let listType = null;
  let codeBlock = null;

  function closeList() {
    list = null;
    listType = null;
  }

  for (const rawLine of lines) {
    const line = rawLine.trim();

    if (line.startsWith("```")) {
      closeList();
      if (codeBlock) {
        codeBlock = null;
      } else {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        pre.appendChild(code);
        container.appendChild(pre);
        codeBlock = code;
      }
      continue;
    }

    if (codeBlock) {
      codeBlock.textContent += (codeBlock.textContent ? "\n" : "") + rawLine;
      continue;
    }

    if (!line) {
      closeList();
      continue;
    }

    if (/^(?:---|___|\*\*\*)$/.test(line)) {
      closeList();
      container.appendChild(document.createElement("hr"));
      continue;
    }

    const blockquoteMatch = line.match(/^>\s?(.+)$/);
    if (blockquoteMatch) {
      closeList();
      const quote = document.createElement("blockquote");
      appendInlineMarkdown(quote, blockquoteMatch[1]);
      container.appendChild(quote);
      continue;
    }

    const headingMatch = line.match(/^#{1,4}\s+(.+)$/);
    if (headingMatch) {
      closeList();
      const heading = document.createElement("div");
      heading.className = "answer-heading";
      appendInlineMarkdown(heading, headingMatch[1]);
      container.appendChild(heading);
      continue;
    }

    const unorderedMatch = line.match(/^[-*•]\s+(.+)$/);
    const orderedMatch = line.match(/^\d+[.)]\s+(.+)$/);
    if (unorderedMatch || orderedMatch) {
      const nextType = unorderedMatch ? "ul" : "ol";
      if (!list || listType !== nextType) {
        closeList();
        list = document.createElement(nextType);
        listType = nextType;
        container.appendChild(list);
      }
      const item = document.createElement("li");
      appendInlineMarkdown(item, (unorderedMatch || orderedMatch)[1]);
      list.appendChild(item);
      continue;
    }

    closeList();
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, line);
    container.appendChild(paragraph);
  }
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
  if (message.role === "assistant") {
    bubble.classList.add("rich-text");
    renderAssistantMarkdown(bubble, message.content || "");
  } else {
    bubble.textContent = message.content || "";
  }
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
  // 新建聊天只切换全局 Session；当前 Tab 的网页上下文保持不变。
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
 * 作用：1) 校验 Tab 可访问性；2) 注入提取器和 content.js；3) 请求正文提取；4) 返回结构化数据。
 * @returns {Promise<{url: string, title: string, raw_text: string, diagnostics: object|null}>} 提取结果
 */
async function extractCurrentPage() {
  const tab = await chrome.tabs.get(currentTabId).catch(() => null);
  if (!tab || tab.id == null || !/^https?:/.test(tab.url || "")) {
    throw new Error("当前标签页不是可访问的网页");
  }
  // 先注入可测试的提取引擎，再注入消息入口。
  // 此处不再静默吞掉错误：如果因权限/CSP 导致注入失败，直接向用户暴露明确原因。
  const injectExtractionScripts = function () {
    return chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["extractor.js", "content.js"],
    });
  };
  try {
    await injectExtractionScripts();
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
  // 使用版本化消息，避免页面中旧版 content.js 监听器抢先返回旧提取结果。
  let response;
  try {
    response = await sendMessageWithTimeout(tab.id, { type: "WEB_CLIP_EXTRACT_V3" }, 10000);
  } catch (messageError) {
    const message = messageError && messageError.message ? messageError.message : "";
    if (!/message port closed|receiving end does not exist|could not establish connection/i.test(message)) {
      throw messageError;
    }
    await injectExtractionScripts();
    response = await sendMessageWithTimeout(tab.id, { type: "WEB_CLIP_EXTRACT_V3" }, 10000);
  }
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
    diagnostics: response.diagnostics || null,
  };
}

async function prepareClipPreview() {
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
  clipBusyAction = "extract";
  clipErrorMsg = null;
  const extractionTabId = currentTabId;
  renderClipView();
  try {
    const page = await extractCurrentPage();
    if (currentTabId !== extractionTabId) {
      return;
    }
    clipDraft = page;
    clipDraftTabId = extractionTabId;
  } catch (err) {
    if (currentTabId === extractionTabId) {
      clearClipDraft();
      clipErrorMsg = err instanceof webRagApiClient.ApiRequestError ? err.message : err && err.message ? err.message : "未知错误";
    }
  } finally {
    clipBusy = false;
    clipBusyAction = null;
    renderClipView();
  }
}

async function submitClipDraft() {
  const plugin = webRagApiClient.getPlugin();
  if (!plugin.pluginId) {
    renderWelcomeView();
    return;
  }
  if (clipBusy || !clipDraft || clipDraftTabId !== currentTabId) return;
  const submitTabId = currentTabId;
  const submitBinding = binding;
  syncClipDraftFromInputs();
  const draft = {
    url: clipDraft.url,
    title: clipDraft.title,
    raw_text: clipDraft.raw_text,
  };
  const rawText = draft.raw_text.trim();
  if (!rawText) {
    clipErrorMsg = "剪藏正文不能为空";
    renderClipView();
    return;
  }
  const tab = await chrome.tabs.get(submitTabId).catch(() => null);
  if (!tab || !isSameNormalizedWebUrl(tab.url, draft.url) || currentTabId !== submitTabId) {
    if (currentTabId === submitTabId) {
      clearClipDraft();
      clipErrorMsg = "页面已变化，请重新提取";
      renderClipView();
    }
    return;
  }
  const page = {
    url: draft.url,
    title: draft.title.trim(),
    raw_text: rawText,
  };
  clipBusy = true;
  clipBusyAction = "save";
  clipErrorMsg = null;
  let submittedJobId = null;
  renderClipView();
  try {
    const job = await webRagApiClient.clips.clipAsync({
      url: page.url,
      title: page.title,
      raw_text: page.raw_text,
    });
    if (job && job.id) {
      submitBinding.documentId = null;
      submitBinding.ingestJobId = job.id;
      submitBinding.ingestJobPageUrl = page.url;
      submitBinding.stale = false;
      submitBinding.pageUrl = page.url;
      submitBinding.pageTitle = page.title;
      await sessionStore.setTabBinding(submitTabId, submitBinding);
      if (currentTabId === submitTabId) {
        binding = submitBinding;
        activeClipJob = job;
      }
      if (clipDraftTabId === submitTabId) {
        clearClipDraft();
      }
      submittedJobId = job.id;
    } else {
      if (currentTabId === submitTabId) {
        clipErrorMsg = "后端未返回 job id";
      }
    }
  } catch (err) {
    if (currentTabId === submitTabId) {
      clipErrorMsg = err instanceof webRagApiClient.ApiRequestError ? err.message : err && err.message ? err.message : "未知错误";
    }
  } finally {
    clipBusy = false;
    clipBusyAction = null;
    renderClipView();
    updateSendState();
    if (submittedJobId && currentTabId === submitTabId) {
      startClipJobMonitor(submittedJobId, submitTabId);
    }
  }
}

async function retryActiveClipJob() {
  if (!activeClipJob || activeClipJob.status !== "FAILED" || clipBusy) return;
  const jobId = activeClipJob.id;
  const tabId = currentTabId;
  els.clipJobRetryBtn.disabled = true;
  try {
    activeClipJob = await webRagApiClient.jobs.retry(jobId);
    clipErrorMsg = null;
    renderClipView();
    startClipJobMonitor(jobId, tabId);
  } catch (err) {
    clipErrorMsg = errorText(err);
    renderClipView();
  } finally {
    els.clipJobRetryBtn.disabled = false;
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
        return "请前往设置配置模型服务";
      case "PLUGIN_NAME_TAKEN":
        return "这个插件名称已经被使用，请换一个名称";
      case "NETWORK":
        return "网络错误，请重试";
      case "BAD_CREDENTIALS":
        return "凭证错误，请重新创建插件";
      case "FILE_TOO_LARGE":
        return "文件过大，最大 2MB";
      case "UNSUPPORTED_FILE_TYPE":
        return "不支持的文件类型，仅支持 .txt / .md / .markdown";
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
    const embedding = currentModelConfig && currentModelConfig.embedding;
    const llm = currentModelConfig && currentModelConfig.llm;
    els.modelStatus.textContent = embedding && llm
      ? "✓ Embedding：" + embedding.provider + " / " + embedding.model + "；问答：" + llm.provider + " / " + llm.model
      : "✓ 已配置（旧版百炼配置）";
    els.modelStatus.className = "model-status ok";
    els.apiKeyConfigBtn.textContent = "修改模型服务";
    els.apiKeyRemoveBtn.hidden = false;
  } else {
    els.modelStatus.textContent = "⚠ 尚未配置";
    els.modelStatus.className = "model-status warn";
    els.apiKeyConfigBtn.textContent = "配置模型服务";
    els.apiKeyRemoveBtn.hidden = true;
  }
}

function renderWarnBanner() {
  const plugin = webRagApiClient.getPlugin();
  els.warnBanner.hidden = !(plugin.pluginId && plugin.apiKeyConfigured === false);
}

function toggleApiKeyForm(show) {
  els.apiKeyForm.hidden = !show;
  if (!show) clearModelKeyInputs();
}

function clearModelKeyInputs() {
  els.embeddingApiKey.value = "";
  els.llmApiKey.value = "";
}

function providerPreset(kind, providerId) {
  const list = modelProviderCatalog && modelProviderCatalog[kind];
  return Array.isArray(list) ? list.find(function (item) { return item.id === providerId; }) : null;
}

function populateProviderSelect(select, providers) {
  select.textContent = "";
  (providers || []).forEach(function (provider) {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.label;
    select.appendChild(option);
  });
}

function applyProviderPreset(kind, keepModel) {
  const isEmbedding = kind === "embedding";
  const select = isEmbedding ? els.embeddingProvider : els.llmProvider;
  const baseInput = isEmbedding ? els.embeddingBaseUrl : els.llmBaseUrl;
  const modelInput = isEmbedding ? els.embeddingModel : els.llmModel;
  const preset = providerPreset(kind, select.value);
  if (!preset) return;
  baseInput.readOnly = preset.id !== "custom";
  if (preset.id !== "custom") baseInput.value = preset.base_url || "";
  if (!keepModel || !modelInput.value) modelInput.value = preset.default_model || "";
  if (isEmbedding) {
    els.embeddingSendDimensions.checked = !!preset.send_dimensions;
    els.embeddingSendDimensions.disabled = preset.id !== "custom";
  }
}

async function openModelConfigForm() {
  if (apiKeyBusy) return;
  setApiKeyStatus("正在加载服务商配置…", null);
  try {
    if (!modelProviderCatalog) {
      modelProviderCatalog = await webRagApiClient.plugins.modelProviders();
    }
    currentModelConfig = await webRagApiClient.plugins.modelConfig();
    populateProviderSelect(els.embeddingProvider, modelProviderCatalog.embedding);
    populateProviderSelect(els.llmProvider, modelProviderCatalog.llm);
    const embedding = currentModelConfig && currentModelConfig.embedding;
    const llm = currentModelConfig && currentModelConfig.llm;
    els.embeddingProvider.value = embedding ? embedding.provider : "dashscope";
    els.llmProvider.value = llm ? llm.provider : "dashscope";
    applyProviderPreset("embedding", false);
    applyProviderPreset("llm", false);
    if (embedding) {
      els.embeddingBaseUrl.value = embedding.base_url;
      els.embeddingModel.value = embedding.model;
      els.embeddingSendDimensions.checked = !!embedding.send_dimensions;
    }
    if (llm) {
      els.llmBaseUrl.value = llm.base_url;
      els.llmModel.value = llm.model;
    }
    clearModelKeyInputs();
    toggleApiKeyForm(true);
    els.embeddingApiKey.focus();
    setApiKeyStatus("请输入两个服务的 API Key；Key 不会回显。", null);
  } catch (err) {
    setApiKeyStatus(errorText(err), "err");
  }
}

function setApiKeyStatus(text, type) {
  els.apiKeyStatus.textContent = text;
  els.apiKeyStatus.classList.remove("ok", "err");
  if (type) {
    els.apiKeyStatus.classList.add(type);
  }
}

async function saveApiKey() {
  if (apiKeyBusy) return;
  const embeddingKey = els.embeddingApiKey.value.trim();
  const llmKey = els.llmApiKey.value.trim();
  if (!embeddingKey || !llmKey) {
    setApiKeyStatus("请输入 Embedding 和问答服务的 API Key", "err");
    return;
  }
  const payload = {
    embedding: {
      provider: els.embeddingProvider.value,
      base_url: els.embeddingBaseUrl.value.trim() || null,
      model: els.embeddingModel.value.trim(),
      api_key: embeddingKey,
      send_dimensions: els.embeddingSendDimensions.checked,
    },
    llm: {
      provider: els.llmProvider.value,
      base_url: els.llmBaseUrl.value.trim() || null,
      model: els.llmModel.value.trim(),
      api_key: llmKey,
      send_dimensions: false,
    },
  };
  if (!payload.embedding.model || !payload.llm.model) {
    setApiKeyStatus("模型名称不能为空", "err");
    return;
  }
  apiKeyBusy = true;
  els.apiKeySaveBtn.disabled = true;
  els.apiKeySaveBtn.textContent = "验证中...";
  setApiKeyStatus("正在分别验证 Embedding 与问答服务…", null);
  try {
    currentModelConfig = await webRagApiClient.plugins.updateModelConfig(payload);
    clearModelKeyInputs();
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
    setApiKeyStatus("模型服务配置成功", "ok");
  } catch (err) {
    clearModelKeyInputs();
    if (err instanceof webRagApiClient.ApiRequestError && err.code === "UNAUTHENTICATED") {
      // 已自动登出
    } else if (err instanceof webRagApiClient.ApiRequestError && (err.status === 400 || err.status === 422)) {
      setApiKeyStatus(err.message || "模型服务验证失败，请检查配置。", "err");
    } else {
      setApiKeyStatus(err instanceof webRagApiClient.ApiRequestError ? err.message : "保存失败，请稍后重试", "err");
    }
  } finally {
    apiKeyBusy = false;
    els.apiKeySaveBtn.disabled = false;
    els.apiKeySaveBtn.textContent = "验证并保存";
  }
}

async function removeApiKey() {
  if (apiKeyBusy) return;
  apiKeyBusy = true;
  els.apiKeyRemoveBtn.disabled = true;
  setApiKeyStatus("正在移除…", null);
  try {
    await webRagApiClient.plugins.removeApiKey();
    currentModelConfig = null;
    webRagApiClient.setPluginDetails({ apiKeyConfigured: false });
    await webRagApiClient.persistPlugin();
    renderSettings();
    renderWarnBanner();
    updateSendState();
    setApiKeyStatus("已移除模型配置", "ok");
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
    showInlineHint("请先在「设置」中配置 Embedding 与问答服务");
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
    if (!binding.documentId || binding.stale || binding.ingestJobId) disabled = true;
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
  els.libraryUploadBtn.addEventListener("click", function () {
    triggerFileInput();
  });
  els.libraryFileInput.addEventListener("change", function () {
    var file = els.libraryFileInput.files && els.libraryFileInput.files[0];
    if (file) handleFileUpload(file);
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
    prepareClipPreview();
  });
  els.clipPreviewRefreshBtn.addEventListener("click", function () {
    prepareClipPreview();
  });
  els.clipPreviewCancelBtn.addEventListener("click", function () {
    clearClipDraft();
    clipErrorMsg = null;
    renderClipView();
  });
  els.clipPreviewSaveBtn.addEventListener("click", function () {
    submitClipDraft();
  });
  els.clipJobRetryBtn.addEventListener("click", function () {
    retryActiveClipJob();
  });
  els.clipPreviewTitle.addEventListener("input", function () {
    syncClipDraftFromInputs();
  });
  els.clipPreviewText.addEventListener("input", function () {
    syncClipDraftFromInputs();
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
    openModelConfigForm();
  });
  els.apiKeySaveBtn.addEventListener("click", function () {
    saveApiKey();
  });
  els.apiKeyRemoveBtn.addEventListener("click", function () {
    removeApiKey();
  });
  [els.embeddingApiKey, els.llmApiKey].forEach(function (input) {
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        saveApiKey();
      }
    });
  });
  els.embeddingProvider.addEventListener("change", function () {
    els.embeddingModel.value = "";
    applyProviderPreset("embedding", false);
  });
  els.llmProvider.addEventListener("change", function () {
    els.llmModel.value = "";
    applyProviderPreset("llm", false);
  });
  els.embeddingBaseUrl.addEventListener("input", function () {
    if (els.embeddingProvider.value !== "custom") applyProviderPreset("embedding", true);
  });
  els.llmBaseUrl.addEventListener("input", function () {
    if (els.llmProvider.value !== "custom") applyProviderPreset("llm", true);
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
    currentModelConfig = null;
    if (me.api_key_configured) {
      try {
        currentModelConfig = await webRagApiClient.plugins.modelConfig();
      } catch (_err) {}
    }
    await webRagApiClient.persistPlugin();
    renderAppView();
    const tab = await getCurrentTab();
    if (tab && tab.id != null) {
      await loadTabContext(tab.id);
      // 初始加载时渲染聊天历史（Tab 切换时不重新渲染）
      renderChat();
    }
    resumeUploadJobMonitor();
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
      await sessionStore.clearUploadJob(ctx.pluginId);
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

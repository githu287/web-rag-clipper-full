// session-store.js —— Session 与 TabBinding 存储层（Phase 3.5 Step 2-F）
// 职责：webRagSessions / webRagSession_<id> / webRagTabBindings 的读写与配额。
// 禁止：Side Panel / Popup / Background 直接操作上述 storage key。
"use strict";

// STORAGE_KEYS 已在 config.js 全局声明，禁止重复声明（避免页面/SW 解析失败）。
const SESSION_PREFIX = STORAGE_KEYS.SESSION_PREFIX;
const MAX_MESSAGES_PER_SESSION = WEB_RAG_CLIPPER_CONFIG.LIMITS.MAX_MESSAGES_PER_SESSION;
const MAX_SESSIONS_PER_PLUGIN = WEB_RAG_CLIPPER_CONFIG.LIMITS.MAX_SESSIONS_PER_PLUGIN;

function newId() {
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

const sessionStore = (() => {
  function noop() {}

  // 同键「读-改-写」串行化：并发异步调用在 await 处交错时会丢失写入，
  // 本上下文用 promise 队列；支持 Web Locks 时，再串行化不同面板与 Service Worker。
  const storageLocks = {};
  function withStorageLock(key, task) {
    const prev = storageLocks[key] || Promise.resolve();
    const lockedTask = () => typeof navigator !== "undefined" && navigator.locks
      ? navigator.locks.request("web-rag-store:" + key, task) : task();
    const run = prev.then(lockedTask, lockedTask);
    storageLocks[key] = run.then(noop, noop);
    return run;
  }

  // ------------------------------------------------------------ tabBindings
  async function getTabBindingsMap() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.TAB_BINDINGS);
    return stored[STORAGE_KEYS.TAB_BINDINGS] || {};
  }
  async function setTabBindingsMap(map) {
    await chrome.storage.local.set({ [STORAGE_KEYS.TAB_BINDINGS]: map });
  }
  async function getTabBinding(tabId) {
    const map = await getTabBindingsMap();
    return map[String(tabId)] || null;
  }
  async function setTabBinding(tabId, binding) {
    return withStorageLock(STORAGE_KEYS.TAB_BINDINGS, async function () {
      const map = await getTabBindingsMap();
      map[String(tabId)] = Object.assign({}, binding, { updatedAt: Date.now() });
      await setTabBindingsMap(map);
      return true;
    });
  }
  async function updateTabBinding(tabId, updater) {
    return withStorageLock(STORAGE_KEYS.TAB_BINDINGS, async function () {
      const map = await getTabBindingsMap();
      const key = String(tabId);
      if (!map[key]) return null;
      map[key] = updater(map[key]);
      await setTabBindingsMap(map);
      return map[key];
    });
  }
  async function removeTabBinding(tabId) {
    return withStorageLock(STORAGE_KEYS.TAB_BINDINGS, async function () {
      const map = await getTabBindingsMap();
      if (map[String(tabId)]) {
        delete map[String(tabId)];
        await setTabBindingsMap(map);
      }
    });
  }
  async function clearTabBindingsByPlugin(pluginId) {
    return withStorageLock(STORAGE_KEYS.TAB_BINDINGS, async function () {
      const map = await getTabBindingsMap();
      let changed = false;
      for (const key of Object.keys(map)) {
        if (map[key] && map[key].pluginId === pluginId) {
          delete map[key];
          changed = true;
        }
      }
      if (changed) await setTabBindingsMap(map);
    });
  }

  // ------------------------------------------------------------ sessions
  async function getSessionIndex() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.SESSIONS);
    return stored[STORAGE_KEYS.SESSIONS] || {};
  }
  async function getSession(sessionId) {
    if (!sessionId) return null;
    const stored = await chrome.storage.local.get(SESSION_PREFIX + sessionId);
    return stored[SESSION_PREFIX + sessionId] || null;
  }
  async function createSession(pluginId, metadata) {
    const now = Date.now();
    const session = {
      sessionId: newId(),
      pluginId: pluginId,
      title: (metadata && metadata.title) || "新会话",
      createdAt: now,
      updatedAt: now,
      messages: [],
    };
    if (!await writeSession(session, true)) throw new Error("无法保存新会话");
    return session;
  }
  // 会话详情是整份覆盖写，而消息只有「追加」这一种变更方式，
  // 因此写之前先读回最新副本按 id 合并，避免并发保存把对方刚追加的消息覆盖掉。
  // 排序按 createdAt（稳定排序，等值时保持「已存副本 → 本次副本」的追加顺序）。
  function mergeSessionMessages(storedMessages, incomingMessages) {
    const seen = new Set();
    const merged = [];
    const sources = [storedMessages, incomingMessages];
    for (const list of sources) {
      if (!Array.isArray(list)) continue;
      for (const message of list) {
        if (!message || typeof message !== "object") continue;
        const id = typeof message.id === "string" ? message.id : null;
        if (id !== null) {
          if (seen.has(id)) continue;
          seen.add(id);
        }
        merged.push(message);
      }
    }
    merged.sort(function (a, b) {
      return (a.createdAt || 0) - (b.createdAt || 0);
    });
    return merged;
  }

  async function saveSession(session) {
    return writeSession(session, false);
  }
  async function writeSession(session, create) {
    if (!session || !session.sessionId) return false;
    session.updatedAt = Date.now();
    const detailKey = SESSION_PREFIX + session.sessionId;
    try {
      // 锁顺序固定为「详情 → SESSIONS 索引」，deleteSession 等路径不得反向取锁。
      return await withStorageLock(detailKey, async function () {
        const stored = await getSession(session.sessionId);
        // 只有 createSession 可以创建；迟到回答、旧面板和配额淘汰后的副本不得重建。
        if (create ? !!stored : !stored || stored.pluginId !== session.pluginId) return false;
        session.messages = mergeSessionMessages(
          stored && stored.messages,
          session.messages
        ).slice(-MAX_MESSAGES_PER_SESSION);
        await withStorageLock(STORAGE_KEYS.SESSIONS, async function () {
          const index = await getSessionIndex();
          index[session.sessionId] = {
            pluginId: session.pluginId,
            title: session.title,
            createdAt: session.createdAt,
            updatedAt: session.updatedAt,
            messageCount: (session.messages || []).length,
          };
          // 消息与索引同批提交，避免详情成功而索引失败后产生无法找回的记录。
          await chrome.storage.local.set({ [detailKey]: session, [STORAGE_KEYS.SESSIONS]: index });
        });
        return true;
      });
    } catch (_err) {
      // 写失败不崩溃（仅记录）；调用方负责继续当前会话
      return false;
    }
  }
  async function deleteSession(sessionId, pluginId) {
    if (!sessionId) return false;
    // 与 saveSession 保持同一锁序（详情 → SESSIONS 索引），
    // 避免删除与「读-合并-写」交错后把已删会话又写回存储。
    return withStorageLock(SESSION_PREFIX + sessionId, async function () {
      const target = await getSession(sessionId);
      if (!target || (pluginId && target.pluginId !== pluginId)) return false;
      return withStorageLock(STORAGE_KEYS.SESSIONS, async function () {
        const index = await getSessionIndex();
        return withStorageLock(STORAGE_KEYS.CURRENT_SESSION, async function () {
          const current = await getCurrentSessionMap();
          delete index[sessionId];
          if (current[target.pluginId] === sessionId) delete current[target.pluginId];
          // 一次提交删除标记、索引及当前指针；失败时不先清空用户内容。
          await chrome.storage.local.set({
            [SESSION_PREFIX + sessionId]: null,
            [STORAGE_KEYS.DRAFT_PREFIX + sessionId]: null,
            [STORAGE_KEYS.SESSIONS]: index,
            [STORAGE_KEYS.CURRENT_SESSION]: current,
          });
          // null 已不含内容且 getSession 视为删除；清理键失败也不会暴露旧记录。
          try { await chrome.storage.local.remove([SESSION_PREFIX + sessionId, STORAGE_KEYS.DRAFT_PREFIX + sessionId]); } catch (_err) {}
          return true;
        });
      });
    });
  }
  async function getSessionsByPlugin(pluginId) {
    const index = await getSessionIndex();
    const ids = Object.keys(index).filter((id) => index[id] && index[id].pluginId === pluginId);
    const sessions = [];
    for (const id of ids) {
      const s = await getSession(id);
      // 旧版（Phase 3.4）session 只有 userId 无 pluginId → 视为 legacy，不加载到任何 Plugin Workspace
      if (s && s.pluginId === pluginId) sessions.push(s);
    }
    sessions.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    return sessions;
  }
  // 每插件最多 MAX_SESSIONS_PER_PLUGIN；超过删除 updatedAt 最小的旧 session（跳过 keepSessionId）
  async function enforceSessionLimit(pluginId, keepSessionId) {
    const index = await getSessionIndex();
    const pluginSessions = Object.keys(index)
      .filter((id) => index[id] && index[id].pluginId === pluginId)
      .sort((a, b) => (index[a].updatedAt || 0) - (index[b].updatedAt || 0));
    const excess = pluginSessions.length - MAX_SESSIONS_PER_PLUGIN;
    if (excess <= 0) return;
    let removed = 0;
    for (const id of pluginSessions) {
      if (removed >= excess) break;
      if (id === keepSessionId) continue;
      await deleteSession(id, pluginId);
      removed++;
    }
  }

  // 草稿与会话删除使用同一把详情锁；删除后迟到的草稿写入不会重建会话。
  async function saveQuestionDraft(pluginId, sessionId, credentialTag, value) {
    return withStorageLock(SESSION_PREFIX + sessionId, async function () {
      const target = await getSession(sessionId);
      if (!target || target.pluginId !== pluginId) return false;
      const key = STORAGE_KEYS.DRAFT_PREFIX + sessionId;
      if (!value || !value.query.trim()) {
        await chrome.storage.local.remove(key);
        return true;
      }
      // 白名单复制，不保存调用方传入的凭证、模型配置或消息对象。
      const context = value.context;
      const validContext = context && (context.mode === "all" ||
        (context.mode === "current" && Number.isSafeInteger(context.documentId) && context.documentId > 0));
      await chrome.storage.local.set({ [key]: {
        pluginId, sessionId, credentialTag, query: value.query, updatedAt: Date.now(),
        context: validContext ? {
          mode: context.mode, documentId: context.mode === "all" ? null : context.documentId,
          pageUrl: typeof context.pageUrl === "string" ? context.pageUrl : null,
          pageTitle: typeof context.pageTitle === "string" ? context.pageTitle : null,
        } : null,
      } });
      return true;
    });
  }
  async function getQuestionDraft(pluginId, sessionId, credentialTag) {
    const target = await getSession(sessionId);
    if (!target || target.pluginId !== pluginId) return null;
    const key = STORAGE_KEYS.DRAFT_PREFIX + sessionId;
    const value = (await chrome.storage.local.get(key))[key];
    if (!value || value.pluginId !== pluginId || value.sessionId !== sessionId ||
      value.credentialTag !== credentialTag || typeof value.query !== "string" || !value.query.trim()) return null;
    return value;
  }
  async function getQuestionDrafts(pluginId, credentialTag) {
    const result = [];
    for (const target of await getSessionsByPlugin(pluginId)) {
      const draft = await getQuestionDraft(pluginId, target.sessionId, credentialTag);
      if (draft) result.push(draft);
    }
    return result.sort((a, b) => b.updatedAt - a.updatedAt);
  }
  async function clearQuestionDrafts(pluginId) {
    for (const target of await getSessionsByPlugin(pluginId)) {
      await saveQuestionDraft(pluginId, target.sessionId, "", null);
    }
  }

  // ------------------------------------------------------------ current session (Phase 3.6 Step 2-H)
  // 每个 Plugin Workspace 只有一个当前 Session（全局聊天）。
  // Tab 切换不改变 currentSessionId；新建、选择或删除会话才改变。
  async function getCurrentSessionMap() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.CURRENT_SESSION);
    return stored[STORAGE_KEYS.CURRENT_SESSION] || {};
  }
  async function getCurrentSessionId(pluginId) {
    if (!pluginId) return null;
    const map = await getCurrentSessionMap();
    return map[pluginId] || null;
  }
  async function setCurrentSessionId(pluginId, sessionId) {
    if (!pluginId) return false;
    return withStorageLock(STORAGE_KEYS.CURRENT_SESSION, async function () {
      const target = await getSession(sessionId);
      if (!target || target.pluginId !== pluginId) return false;
      const map = await getCurrentSessionMap();
      map[pluginId] = sessionId;
      await chrome.storage.local.set({ [STORAGE_KEYS.CURRENT_SESSION]: map });
      return true;
    });
  }
  async function clearCurrentSessionId(pluginId) {
    if (!pluginId) return;
    await withStorageLock(STORAGE_KEYS.CURRENT_SESSION, async function () {
      const map = await getCurrentSessionMap();
      delete map[pluginId];
      await chrome.storage.local.set({ [STORAGE_KEYS.CURRENT_SESSION]: map });
    });
  }

  // ------------------------------------------------------------ upload jobs
  function normalizeUploadJobRecords(value) {
    if (!value || typeof value !== "object") return {};
    if (value.jobId && value.documentId != null) {
      return { [String(value.documentId)]: value };
    }
    return value;
  }
  async function getUploadJobs(pluginId) {
    if (!pluginId) return [];
    const stored = await chrome.storage.local.get(STORAGE_KEYS.UPLOAD_JOBS);
    const map = stored[STORAGE_KEYS.UPLOAD_JOBS] || {};
    return Object.values(normalizeUploadJobRecords(map[pluginId])).sort(function (a, b) {
      return (b.updatedAt || 0) - (a.updatedAt || 0);
    });
  }
  async function getUploadJob(pluginId, documentId) {
    const records = await getUploadJobs(pluginId);
    if (documentId == null) return records[0] || null;
    return records.find(function (record) {
      return Number(record.documentId) === Number(documentId);
    }) || null;
  }
  async function setUploadJob(pluginId, job) {
    if (!pluginId || !job || job.documentId == null) return;
    await withStorageLock(STORAGE_KEYS.UPLOAD_JOBS, async function () {
      const stored = await chrome.storage.local.get(STORAGE_KEYS.UPLOAD_JOBS);
      const map = stored[STORAGE_KEYS.UPLOAD_JOBS] || {};
      const records = normalizeUploadJobRecords(map[pluginId]);
      records[String(job.documentId)] = Object.assign({}, job, { updatedAt: Date.now() });
      map[pluginId] = records;
      await chrome.storage.local.set({ [STORAGE_KEYS.UPLOAD_JOBS]: map });
    });
  }
  async function clearUploadJob(pluginId, documentId) {
    if (!pluginId) return;
    await withStorageLock(STORAGE_KEYS.UPLOAD_JOBS, async function () {
      const stored = await chrome.storage.local.get(STORAGE_KEYS.UPLOAD_JOBS);
      const map = stored[STORAGE_KEYS.UPLOAD_JOBS] || {};
      if (!map[pluginId]) return;
      if (documentId == null) {
        delete map[pluginId];
      } else {
        const records = normalizeUploadJobRecords(map[pluginId]);
        delete records[String(documentId)];
        if (Object.keys(records).length > 0) map[pluginId] = records;
        else delete map[pluginId];
      }
      await chrome.storage.local.set({ [STORAGE_KEYS.UPLOAD_JOBS]: map });
    });
  }

  return {
    newId,
    getTabBinding,
    setTabBinding,
    updateTabBinding,
    removeTabBinding,
    clearTabBindingsByPlugin,
    getSession,
    createSession,
    saveSession,
    deleteSession,
    getSessionsByPlugin,
    enforceSessionLimit,
    saveQuestionDraft,
    getQuestionDraft,
    getQuestionDrafts,
    clearQuestionDrafts,
    getCurrentSessionId,
    setCurrentSessionId,
    clearCurrentSessionId,
    getUploadJob,
    getUploadJobs,
    setUploadJob,
    clearUploadJob,
  };
})();

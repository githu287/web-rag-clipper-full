// session-store.js —— Session 与 TabBinding 存储层（Phase 3.4 Step F8 Step 2）
// 职责：webRagSessions / webRagSession_<id> / webRagTabBindings 的读写与配额。
// 禁止：Side Panel / Popup / Background 直接操作上述 storage key。
"use strict";

// STORAGE_KEYS 已在 config.js 全局声明，禁止重复声明（避免页面/SW 解析失败）。
const SESSION_PREFIX = STORAGE_KEYS.SESSION_PREFIX;
const MAX_MESSAGES_PER_SESSION = WEB_RAG_CLIPPER_CONFIG.LIMITS.MAX_MESSAGES_PER_SESSION;
const MAX_SESSIONS_PER_USER = WEB_RAG_CLIPPER_CONFIG.LIMITS.MAX_SESSIONS_PER_USER;

function newId() {
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

const sessionStore = (() => {
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
    const map = await getTabBindingsMap();
    map[String(tabId)] = Object.assign({}, binding, { updatedAt: Date.now() });
    await setTabBindingsMap(map);
    return true;
  }
  async function removeTabBinding(tabId) {
    const map = await getTabBindingsMap();
    if (map[String(tabId)]) {
      delete map[String(tabId)];
      await setTabBindingsMap(map);
    }
  }
  async function clearTabBindingsByUser(userId) {
    const map = await getTabBindingsMap();
    let changed = false;
    for (const key of Object.keys(map)) {
      if (map[key] && map[key].userId === userId) {
        delete map[key];
        changed = true;
      }
    }
    if (changed) await setTabBindingsMap(map);
  }

  // ------------------------------------------------------------ sessions
  async function getSessionIndex() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.SESSIONS);
    return stored[STORAGE_KEYS.SESSIONS] || {};
  }
  async function setSessionIndex(index) {
    await chrome.storage.local.set({ [STORAGE_KEYS.SESSIONS]: index });
  }
  async function getSession(sessionId) {
    if (!sessionId) return null;
    const stored = await chrome.storage.local.get(SESSION_PREFIX + sessionId);
    return stored[SESSION_PREFIX + sessionId] || null;
  }
  async function createSession(userId, metadata) {
    const now = Date.now();
    const session = {
      sessionId: newId(),
      userId: userId,
      title: (metadata && metadata.title) || "新会话",
      createdAt: now,
      updatedAt: now,
      messages: [],
    };
    await saveSession(session);
    return session;
  }
  async function saveSession(session) {
    if (!session || !session.sessionId) return false;
    session.updatedAt = Date.now();
    if (Array.isArray(session.messages) && session.messages.length > MAX_MESSAGES_PER_SESSION) {
      session.messages = session.messages.slice(-MAX_MESSAGES_PER_SESSION);
    }
    try {
      await chrome.storage.local.set({ [SESSION_PREFIX + session.sessionId]: session });
      const index = await getSessionIndex();
      index[session.sessionId] = {
        userId: session.userId,
        title: session.title,
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        messageCount: (session.messages || []).length,
      };
      await setSessionIndex(index);
      return true;
    } catch (_err) {
      // 写失败不崩溃（仅记录）；调用方负责继续当前会话
      return false;
    }
  }
  async function deleteSession(sessionId) {
    if (!sessionId) return;
    await chrome.storage.local.remove(SESSION_PREFIX + sessionId);
    const index = await getSessionIndex();
    if (index[sessionId]) {
      delete index[sessionId];
      await setSessionIndex(index);
    }
  }
  async function getSessionsByUser(userId) {
    const index = await getSessionIndex();
    const ids = Object.keys(index).filter((id) => index[id] && index[id].userId === userId);
    const sessions = [];
    for (const id of ids) {
      const s = await getSession(id);
      if (s && s.userId === userId) sessions.push(s);
    }
    sessions.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    return sessions;
  }
  // 每用户最多 MAX_SESSIONS_PER_USER；超过删除 updatedAt 最小的旧 session（跳过 keepSessionId）
  async function enforceSessionLimit(userId, keepSessionId) {
    const index = await getSessionIndex();
    const userSessions = Object.keys(index)
      .filter((id) => index[id] && index[id].userId === userId)
      .sort((a, b) => (index[a].updatedAt || 0) - (index[b].updatedAt || 0));
    const excess = userSessions.length - MAX_SESSIONS_PER_USER;
    if (excess <= 0) return;
    let removed = 0;
    for (const id of userSessions) {
      if (removed >= excess) break;
      if (id === keepSessionId) continue;
      await deleteSession(id);
      removed++;
    }
  }

  return {
    newId,
    getTabBinding,
    setTabBinding,
    removeTabBinding,
    clearTabBindingsByUser,
    getSession,
    createSession,
    saveSession,
    deleteSession,
    getSessionsByUser,
    enforceSessionLimit,
  };
})();

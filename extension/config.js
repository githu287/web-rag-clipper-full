// Web RAG Clipper 统一配置
// 所有 JS 只从这里读取后端地址，禁止在其他文件中散落硬编码。
// 如需指向其他后端（如局域网 / 公网部署），只需修改 API_BASE_URL，
// 并同步 manifest.json 中 host_permissions 的对应地址。
//
// 职责边界（Phase 3.4 Step F5 / F8 Step 2）：
//   - 只负责 API_BASE_URL 与必要的 storage key 常量；
//   - 绝不在此处写入：API Key、用户密码、token 明文常量；
//   - 动态 token 从 chrome.storage.local 获取（见 api-client.js）。
const WEB_RAG_CLIPPER_CONFIG = {
  API_BASE_URL: "http://localhost:8000",
  STORAGE_KEYS: {
    // 已登录身份：{ token, user_id, username }（token 必须用于业务请求）
    AUTH: "webRagAuth",
    // 是否已配置百炼 API Key（登录状态 ≠ API Key 状态）
    API_KEY_CONFIGURED: "webRagApiKeyConfigured",
    // 旧版（Phase 3.4 Step 4）残留 key，加载时清理，避免误用
    LEGACY_TOKEN: "webRagToken",
    LEGACY_USER_ID: "webRagUserId",
    // 会话索引（轻量）：{ [sessionId]: { userId, title, createdAt, updatedAt, messageCount } }
    SESSIONS: "webRagSessions",
    // 会话明细前缀：webRagSession_<sessionId>
    SESSION_PREFIX: "webRagSession_",
    // Tab → 上下文绑定：{ [tabId]: { userId, sessionId, documentId, pageUrl, pageTitle, mode, stale, updatedAt } }
    TAB_BINDINGS: "webRagTabBindings",
  },
  LIMITS: {
    // 每个 session 最大消息数（超限裁剪最旧）
    MAX_MESSAGES_PER_SESSION: 100,
    // 每个用户最大 session 数（超限删除 updatedAt 最小的旧 session）
    MAX_SESSIONS_PER_USER: 20,
  },
};

// 全局唯一声明：STORAGE_KEYS 仅此一处定义。
// 其他脚本（session-store.js / api-client.js / background.js）直接引用全局，禁止重复声明。
const STORAGE_KEYS = WEB_RAG_CLIPPER_CONFIG.STORAGE_KEYS;

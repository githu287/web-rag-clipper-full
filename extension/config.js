// Web RAG Clipper 统一配置
// 所有 JS 只从这里读取后端地址，禁止在其他文件中散落硬编码。
// 如需指向其他后端（如局域网 / 公网部署），只需修改 API_BASE_URL，
// 并同步 manifest.json 中 host_permissions 的对应地址。
//
// 职责边界（Phase 3.5 Step 2-F）：
//   - 只负责 API_BASE_URL 与必要的 storage key 常量；
//   - 绝不在此处写入：API Key、plugin_secret 明文常量；
//   - 动态 plugin_secret 从 chrome.storage.local 获取（见 api-client.js）。
const WEB_RAG_CLIPPER_CONFIG = {
  API_BASE_URL: "http://localhost:8000",
  STORAGE_KEYS: {
    // 插件身份：{ plugin_id, plugin_secret, plugin_name }
    // plugin_secret 仅用于 X-Plugin-Secret 请求头，禁止落日志 / URL / DOM / 会话。
    PLUGIN: "webRagPlugin",
    // 每个 Plugin 的当前 Session ID：{ [pluginId]: sessionId }
    // Phase 3.6 Step 2-H：全局 Session，所有 Tab 共享同一个聊天会话
    CURRENT_SESSION: "webRagCurrentSession",
    // 会话索引（轻量）：{ [sessionId]: { pluginId, title, createdAt, updatedAt, messageCount } }
    SESSIONS: "webRagSessions",
    // 会话明细前缀：webRagSession_<sessionId>
    SESSION_PREFIX: "webRagSession_",
    // Tab → 上下文绑定：{ [tabId]: { pluginId, documentId, pageUrl, pageTitle, mode, stale, updatedAt } }
    // Phase 3.6 Step 2-H：Tab Binding 不再包含 sessionId，只负责网页上下文
    TAB_BINDINGS: "webRagTabBindings",
  },
  LIMITS: {
    // 每个 session 最大消息数（超限裁剪最旧）
    MAX_MESSAGES_PER_SESSION: 100,
    // 每个插件最大 session 数（超限删除 updatedAt 最小的旧 session）
    MAX_SESSIONS_PER_PLUGIN: 20,
  },
};

// 全局唯一声明：STORAGE_KEYS 仅此一处定义。
// 其他脚本（session-store.js / api-client.js / background.js）直接引用全局，禁止重复声明。
const STORAGE_KEYS = WEB_RAG_CLIPPER_CONFIG.STORAGE_KEYS;

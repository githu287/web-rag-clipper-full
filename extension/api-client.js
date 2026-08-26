// api-client.js —— Web RAG Clipper 统一 API Client（Phase 3.4 Step F8 Step 2）
// 将 popup.js 中原有的认证请求逻辑统一抽离，Popup / Side Panel 共用。
// 职责：统一 auth（Bearer token）、统一错误分类、统一 JSON/204/非 JSON 处理。
// 禁止：自动 retry（clips/upload/ask/search 可能产生重复请求）；保存 API Key / password。
"use strict";

const API_BASE_URL = WEB_RAG_CLIPPER_CONFIG.API_BASE_URL;
// STORAGE_KEYS 已在 config.js 全局声明，禁止重复声明（避免页面解析失败）。

class ApiRequestError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

function isApiKeyNotConfiguredError(data) {
  return !!(data && typeof data.detail === "string" && /API\s?Key/i.test(data.detail));
}

function parseErrorMessage(data, fallback) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail) && data.detail.length > 0) {
    const first = data.detail[0];
    const loc = first && Array.isArray(first.loc) ? first.loc.join(".") : "";
    const msg = first && typeof first.msg === "string" ? first.msg : fallback;
    return loc ? msg + "（字段: " + loc + "）" : msg;
  }
  return fallback;
}

const webRagApiClient = (() => {
  let authToken = null;
  let authUserId = null;
  let authUsername = null;
  let apiKeyConfigured = false;
  let unauthenticatedHandler = null;

  async function loadAuth() {
    await chrome.storage.local.remove([STORAGE_KEYS.LEGACY_TOKEN, STORAGE_KEYS.LEGACY_USER_ID]);
    const stored = await chrome.storage.local.get([STORAGE_KEYS.AUTH, STORAGE_KEYS.API_KEY_CONFIGURED]);
    const auth = stored && stored[STORAGE_KEYS.AUTH];
    if (auth && typeof auth.token === "string" && auth.token) {
      authToken = auth.token;
      authUserId = auth.user_id != null ? Number(auth.user_id) : null;
      authUsername = typeof auth.username === "string" ? auth.username : null;
    } else {
      authToken = null;
      authUserId = null;
      authUsername = null;
    }
    apiKeyConfigured = !!(stored && stored[STORAGE_KEYS.API_KEY_CONFIGURED]);
  }

  async function persistAuth() {
    await chrome.storage.local.set({
      [STORAGE_KEYS.AUTH]: { token: authToken, user_id: authUserId, username: authUsername },
      [STORAGE_KEYS.API_KEY_CONFIGURED]: apiKeyConfigured,
    });
  }

  async function clearAuth() {
    authToken = null;
    authUserId = null;
    authUsername = null;
    apiKeyConfigured = false;
    await chrome.storage.local.remove([STORAGE_KEYS.AUTH, STORAGE_KEYS.API_KEY_CONFIGURED]);
  }

  function getAuth() {
    return { token: authToken, userId: authUserId, username: authUsername, apiKeyConfigured: apiKeyConfigured };
  }

  function setAuthDetails(partial) {
    if (!partial) return;
    if (partial.token != null) authToken = partial.token;
    if (partial.userId != null) authUserId = Number(partial.userId);
    if (typeof partial.username === "string") authUsername = partial.username;
    if (typeof partial.apiKeyConfigured === "boolean") apiKeyConfigured = partial.apiKeyConfigured;
  }

  function setUnauthenticatedHandler(handler) {
    unauthenticatedHandler = typeof handler === "function" ? handler : null;
  }

  async function request(path, options) {
    const opts = options || {};
    const method = opts.method || "GET";
    const headers = Object.assign({}, opts.headers || {});
    let body = opts.body;
    if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      if (typeof body !== "string") body = JSON.stringify(body);
    }
    if (opts.auth !== false) {
      if (!authToken) {
        throw new ApiRequestError(401, "UNAUTHENTICATED", "登录已失效，请重新登录");
      }
      headers["Authorization"] = "Bearer " + authToken;
    }
    let response;
    try {
      response = await fetch(API_BASE_URL + path, { method: method, headers: headers, body: body });
    } catch (_err) {
      throw new ApiRequestError(0, "NETWORK", "网络连接失败，请重试");
    }
    if (response.status === 204) {
      return { response: response, data: null };
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_err) {
      data = null;
    }
    if (opts.auth !== false && response.status === 401) {
      const userId = authUserId;
      await clearAuth();
      if (unauthenticatedHandler) {
        await unauthenticatedHandler({ userId: userId });
      }
      throw new ApiRequestError(401, "UNAUTHENTICATED", "登录已失效，请重新登录");
    }
    if (response.status === 403) {
      throw new ApiRequestError(403, "DISABLED", "账号已被禁用，请联系管理员");
    }
    if (response.status === 409 && isApiKeyNotConfiguredError(data)) {
      throw new ApiRequestError(409, "API_KEY_NOT_CONFIGURED", "请前往设置配置阿里云百炼 API Key");
    }
    if (!response.ok) {
      if (response.status === 401) {
        throw new ApiRequestError(401, "BAD_CREDENTIALS", "用户名或密码错误");
      }
      if (response.status === 409) {
        throw new ApiRequestError(409, "USERNAME_EXISTS", "用户名已存在，请直接登录");
      }
      if (response.status === 422) {
        throw new ApiRequestError(422, "VALIDATION", parseErrorMessage(data, "输入不合法，请检查后重试"));
      }
      throw new ApiRequestError(
        response.status,
        "HTTP",
        data
          ? parseErrorMessage(data, "请求失败（HTTP " + response.status + "）")
          : "服务器返回了无法解析的响应（HTTP " + response.status + "）"
      );
    }
    return { response: response, data: data };
  }

  return {
    ApiRequestError: ApiRequestError,
    getAuth: getAuth,
    setAuthDetails: setAuthDetails,
    loadAuth: loadAuth,
    persistAuth: persistAuth,
    clearAuth: clearAuth,
    setUnauthenticatedHandler: setUnauthenticatedHandler,
    request: request,
    auth: {
      async login(username, password) {
        const result = await request("/auth/login", { method: "POST", body: { username: username, password: password }, auth: false });
        return result.data;
      },
      async register(username, password) {
        const result = await request("/auth/register", { method: "POST", body: { username: username, password: password }, auth: false });
        return result.data;
      },
      async logout() {
        await request("/auth/logout", { method: "POST" });
      },
    },
    users: {
      async me() {
        const result = await request("/users/me", { method: "GET" });
        return result.data;
      },
      async updateApiKey(apiKey) {
        await request("/users/me/api-key", { method: "PUT", body: { api_key: apiKey } });
      },
      async removeApiKey() {
        await request("/users/me/api-key", { method: "DELETE" });
      },
    },
    clips: {
      async clip(payload) {
        const result = await request("/clips", { method: "POST", body: payload });
        return result.data;
      },
    },
    rag: {
      // 全部知识库模式：不传 document_id（后端按 Bearer token → users.id → SUCCESS documents）
      // 当前网页模式：传 document_id（仅当前 tab 的有效绑定）
      async ask(queryObj) {
        const body = { query: queryObj.query };
        if (queryObj.document_id != null) body.document_id = queryObj.document_id;
        const result = await request("/rag/ask", { method: "POST", body: body });
        return result.data;
      },
      async search(queryObj) {
        const body = { query: queryObj.query };
        if (queryObj.limit != null) body.limit = queryObj.limit;
        if (queryObj.document_id != null) body.document_id = queryObj.document_id;
        const result = await request("/rag/search", { method: "POST", body: body });
        return result.data;
      },
    },
  };
})();

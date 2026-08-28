// api-client.js —— Web RAG Clipper 统一 API Client（Phase 3.5 Step 2-F）
// 职责：统一 Plugin Workspace 双凭证认证（X-Plugin-ID / X-Plugin-Secret）、
//       统一错误分类、统一 JSON/204/非 JSON 处理。
// 禁止：自动 retry（clips/upload/ask/search 可能产生重复请求）；
//       保存 API Key / password；plugin_secret 落日志 / URL / DOM / 会话。
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
  // 内存态身份：仅 X-Plugin-ID / X-Plugin-Secret 参与请求认证，
  // plugin_secret 严禁 console.log / 拼 URL / 渲染到 DOM / 写入会话。
  let pluginId = null;
  let pluginSecret = null;
  let pluginName = null;
  let apiKeyConfigured = false; // 以 GET /plugins/me 返回为准，不持久化
  let unauthenticatedHandler = null;

  // 读取 webRagPlugin（唯一身份来源）。
  async function loadPlugin() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.PLUGIN);
    const plugin = stored && stored[STORAGE_KEYS.PLUGIN];
    if (plugin && typeof plugin.plugin_id === "string" && plugin.plugin_id) {
      pluginId = plugin.plugin_id;
      pluginSecret = typeof plugin.plugin_secret === "string" ? plugin.plugin_secret : null;
      pluginName = typeof plugin.plugin_name === "string" ? plugin.plugin_name : null;
    } else {
      pluginId = null;
      pluginSecret = null;
      pluginName = null;
    }
    apiKeyConfigured = false;
    return getPlugin();
  }

  // 仅持久化 { plugin_id, plugin_secret, plugin_name }（webRagPlugin）
  async function persistPlugin() {
    if (!pluginId || !pluginSecret) return false;
    await chrome.storage.local.set({
      [STORAGE_KEYS.PLUGIN]: {
        plugin_id: pluginId,
        plugin_secret: pluginSecret,
        plugin_name: pluginName,
      },
    });
    return true;
  }

  // 清除插件身份（不触碰 sessions；调用方负责清 tabBindings）
  async function clearPlugin() {
    pluginId = null;
    pluginSecret = null;
    pluginName = null;
    apiKeyConfigured = false;
    await chrome.storage.local.remove(STORAGE_KEYS.PLUGIN);
  }

  function getPlugin() {
    return {
      pluginId: pluginId,
      pluginSecret: pluginSecret,
      pluginName: pluginName,
      apiKeyConfigured: apiKeyConfigured,
    };
  }

  function setPluginDetails(partial) {
    if (!partial) return;
    if (typeof partial.pluginId === "string" && partial.pluginId) pluginId = partial.pluginId;
    if (typeof partial.pluginSecret === "string" && partial.pluginSecret) pluginSecret = partial.pluginSecret;
    if (typeof partial.pluginName === "string") pluginName = partial.pluginName;
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
      if (!pluginId || !pluginSecret) {
        throw new ApiRequestError(401, "UNAUTHENTICATED", "插件凭证缺失，请重新创建插件");
      }
      // 双凭证：X-Plugin-ID + X-Plugin-Secret（唯一认证来源）
      headers["X-Plugin-ID"] = pluginId;
      headers["X-Plugin-Secret"] = pluginSecret;
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
    // 401：凭证失效 → 清除本地插件身份，回到 Welcome
    if (opts.auth !== false && response.status === 401) {
      const currentPluginId = pluginId;
      await clearPlugin();
      if (unauthenticatedHandler) {
        await unauthenticatedHandler({ pluginId: currentPluginId });
      }
      throw new ApiRequestError(401, "UNAUTHENTICATED", "插件凭证已失效，请重新创建插件");
    }
    // 403：插件被禁用 → 不清理 secret、不创建新 workspace
    if (response.status === 403) {
      if (data && data.type === "PluginDisabledError") {
        throw new ApiRequestError(403, "PLUGIN_DISABLED", "插件已被禁用，请联系管理员");
      }
      throw new ApiRequestError(403, "DISABLED", "插件已被禁用，请联系管理员");
    }
    // 409 + API Key 文案 → API Key 未配置
    if (response.status === 409 && isApiKeyNotConfiguredError(data)) {
      throw new ApiRequestError(409, "API_KEY_NOT_CONFIGURED", "请前往设置配置阿里云百炼 API Key");
    }
    if (!response.ok) {
      if (response.status === 401) {
        throw new ApiRequestError(401, "UNAUTHENTICATED", "插件凭证已失效，请重新创建插件");
      }
      if (response.status === 409) {
        if (data && data.type === "PluginNameTakenError") {
          throw new ApiRequestError(409, "PLUGIN_NAME_TAKEN", "这个插件名称已经被使用，请换一个名称");
        }
        throw new ApiRequestError(
          response.status,
          "HTTP",
          data ? parseErrorMessage(data, "请求冲突（HTTP 409）") : "请求冲突（HTTP 409）"
        );
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

  // multipart/form-data 上传（POST /documents/upload）。
  // 与 request() 分离：FormData 需要浏览器自动生成 boundary，禁止手动设置 Content-Type。
  // 认证 / 错误分类 / 401→clearPlugin 等逻辑与 request() 保持一致。
  async function uploadRequest(path, formData) {
    if (!pluginId || !pluginSecret) {
      throw new ApiRequestError(401, "UNAUTHENTICATED", "插件凭证缺失，请重新创建插件");
    }
    const headers = {};
    headers["X-Plugin-ID"] = pluginId;
    headers["X-Plugin-Secret"] = pluginSecret;
    let response;
    try {
      response = await fetch(API_BASE_URL + path, { method: "POST", headers: headers, body: formData });
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
    if (response.status === 401) {
      const currentPluginId = pluginId;
      await clearPlugin();
      if (unauthenticatedHandler) {
        await unauthenticatedHandler({ pluginId: currentPluginId });
      }
      throw new ApiRequestError(401, "UNAUTHENTICATED", "插件凭证已失效，请重新创建插件");
    }
    if (response.status === 403) {
      if (data && data.type === "PluginDisabledError") {
        throw new ApiRequestError(403, "PLUGIN_DISABLED", "插件已被禁用，请联系管理员");
      }
      throw new ApiRequestError(403, "DISABLED", "插件已被禁用，请联系管理员");
    }
    if (response.status === 409 && isApiKeyNotConfiguredError(data)) {
      throw new ApiRequestError(409, "API_KEY_NOT_CONFIGURED", "请前往设置配置阿里云百炼 API Key");
    }
    if (response.status === 413) {
      throw new ApiRequestError(413, "FILE_TOO_LARGE", parseErrorMessage(data, "文件过大，超出限制"));
    }
    if (response.status === 415) {
      throw new ApiRequestError(415, "UNSUPPORTED_FILE_TYPE", parseErrorMessage(data, "不支持的文件类型"));
    }
    if (!response.ok) {
      if (response.status === 422) {
        throw new ApiRequestError(422, "VALIDATION", parseErrorMessage(data, "输入不合法，请检查后重试"));
      }
      throw new ApiRequestError(
        response.status,
        "HTTP",
        data
          ? parseErrorMessage(data, "上传失败（HTTP " + response.status + "）")
          : "服务器返回了无法解析的响应（HTTP " + response.status + "）"
      );
    }
    return { response: response, data: data };
  }

  return {
    ApiRequestError: ApiRequestError,
    getPlugin: getPlugin,
    setPluginDetails: setPluginDetails,
    loadPlugin: loadPlugin,
    persistPlugin: persistPlugin,
    clearPlugin: clearPlugin,
    setUnauthenticatedHandler: setUnauthenticatedHandler,
    request: request,
    plugins: {
      // POST /plugins/register：创建插件 workspace（无需凭证）
      async register(pluginName) {
        const result = await request("/plugins/register", {
          method: "POST",
          body: { plugin_name: pluginName },
          auth: false,
        });
        return result.data;
      },
      // GET /plugins/me：启动校验 + 获取 api_key_configured
      async me() {
        const result = await request("/plugins/me", { method: "GET" });
        return result.data;
      },
      // PUT /plugins/me：修改插件名称（plugin_id / plugin_secret / 知识库不变）
      async updateName(pluginName) {
        const result = await request("/plugins/me", { method: "PUT", body: { plugin_name: pluginName } });
        return result.data;
      },
      // PUT /plugins/me/api-key：保存并校验阿里云百炼 API Key
      async updateApiKey(apiKey) {
        const result = await request("/plugins/me/api-key", { method: "PUT", body: { api_key: apiKey } });
        return result.data;
      },
      // DELETE /plugins/me/api-key：移除 API Key
      async removeApiKey() {
        await request("/plugins/me/api-key", { method: "DELETE" });
      },
      // DELETE /plugins/me：删除插件 workspace（需 confirm + plugin_name）
      async delete(pluginName) {
        await request("/plugins/me", {
          method: "DELETE",
          body: { confirm: true, plugin_name: pluginName },
        });
      },
    },
    clips: {
      async clip(payload) {
        const result = await request("/clips", { method: "POST", body: payload });
        return result.data;
      },
    },
    rag: {
      // 全部知识库模式：不传 document_id（后端按 X-Plugin-ID → plugin_id → SUCCESS documents）
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
    documents: {
      // GET /documents：分页列出当前 Plugin 的知识库（「我的知识库」）
      // 归属唯一来自后端 current_plugin.plugin_id；禁止前端传 plugin_id / user_id。
      async list(params) {
        const p = params || {};
        const query = [];
        if (p.page != null) query.push("page=" + encodeURIComponent(String(p.page)));
        if (p.page_size != null) query.push("page_size=" + encodeURIComponent(String(p.page_size)));
        if (p.keyword) query.push("keyword=" + encodeURIComponent(p.keyword));
        if (p.status) query.push("status=" + encodeURIComponent(p.status));
        if (p.source_type) query.push("source_type=" + encodeURIComponent(p.source_type));
        const qs = query.length > 0 ? "?" + query.join("&") : "";
        const result = await request("/documents" + qs, { method: "GET" });
        return result.data;
      },
      // GET /documents/{id}：文档详情（跨 Workspace 后端统一 404）
      async get(documentId) {
        const result = await request(
          "/documents/" + encodeURIComponent(String(documentId)),
          { method: "GET" }
        );
        return result.data;
      },
      // DELETE /documents/{id}：删除文档（幂等 204；走统一双凭证认证）
      async delete(documentId) {
        await request(
          "/documents/" + encodeURIComponent(String(documentId)),
          { method: "DELETE" }
        );
      },
      // POST /documents/{id}/ingest：重试 ingest（chunks 由调用方提供，
      // 与现有剪藏/上传链路共用同一契约；走统一双凭证认证）
      async ingest(documentId, chunks) {
        const result = await request(
          "/documents/" + encodeURIComponent(String(documentId)) + "/ingest",
          { method: "POST", body: { chunks: chunks } }
        );
        return result.data;
      },
      // POST /documents/upload：multipart/form-data 文件上传（走 uploadRequest 独立通道）
      async uploadFile(file) {
        const formData = new FormData();
        formData.append("file", file);
        const result = await uploadRequest("/documents/upload", formData);
        return result.data;
      },
    },
  };
})();

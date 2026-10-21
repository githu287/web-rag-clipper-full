"use strict";

// api-client.js 错误分类回归测试：request（JSON + 超时）与 uploadRequest（FormData）
// 共用同一套响应分类逻辑，本文件锁住两条通道的状态码 → ApiRequestError 映射，
// 保证后续重构不会让两者的错误语义各自漂移。

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionDir = path.resolve(__dirname, "..");

function loadApiClient(fetchImpl) {
  const stored = {};
  const chrome = {
    storage: {
      local: {
        async get(key) {
          if (typeof key === "string") return { [key]: stored[key] };
          return Object.assign({}, stored);
        },
        async set(items) {
          Object.assign(stored, items);
        },
        async remove(key) {
          delete stored[key];
        },
      },
    },
  };
  const context = vm.createContext({
    chrome,
    console,
    fetch: fetchImpl,
    AbortController,
    setTimeout,
    clearTimeout,
    // 上传通道内部会 new FormData()，沙箱需要提供可记录的替身。
    FormData: function RecordingFormData() {
      this.entries = [];
      this.append = function (name, value) {
        this.entries.push([name, value]);
      };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "config.js"), "utf8"), context);
  vm.runInContext(
    fs.readFileSync(path.join(extensionDir, "api-client.js"), "utf8") +
      "\nglobalThis.__api = webRagApiClient;",
    context,
  );
  return context.__api;
}

function makeResponse(status, body, options) {
  const opts = options || {};
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() {
      if (opts.invalidJson) throw new TypeError("body is not JSON");
      return body;
    },
  };
}

function respondingWith(response) {
  const calls = [];
  const api = loadApiClient(async function (url, init) {
    calls.push({ url, init });
    return response;
  });
  api.setPluginDetails({ pluginId: "plugin-a", pluginSecret: "secret-a" });
  return { api, calls };
}

// uploadRequest 是模块内私有函数，上传通道只能通过公共表面触发。
function uploadVia(api) {
  return api.documents.uploadFile({ name: "notes.txt" });
}

async function expectApiError(promise, status, code, messagePattern) {
  let caught = null;
  try {
    await promise;
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, "expected the call to reject");
  assert.equal(caught.name, "ApiRequestError");
  assert.equal(caught.status, status);
  assert.equal(caught.code, code);
  if (messagePattern) assert.match(caught.message, messagePattern);
  return caught;
}

(async function run() {
  // ---------------------------------------------------------------- 成功路径
  {
    const { api, calls } = respondingWith(makeResponse(200, { ok: true }));
    const result = await api.request("/plugins/me", { method: "GET" });
    assert.deepEqual(result.data, { ok: true });
    assert.equal(calls[0].url, "http://localhost:18000/plugins/me");
    assert.equal(calls[0].init.headers["X-Plugin-ID"], "plugin-a");
    assert.equal(calls[0].init.headers["X-Plugin-Secret"], "secret-a");
  }
  {
    const { api, calls } = respondingWith(makeResponse(200, { ok: true }));
    await api.request("/clips", { method: "POST", body: { raw_text: "正文" } });
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");
    assert.equal(calls[0].init.body, JSON.stringify({ raw_text: "正文" }));
  }
  {
    const { api, calls } = respondingWith(makeResponse(204, null));
    const result = await api.request("/documents/1", { method: "DELETE" });
    assert.equal(result.data, null);
    assert.equal(calls.length, 1);
  }

  // ---------------------------------------------------------------- FormData 通道
  {
    const { api, calls } = respondingWith(makeResponse(200, { id: 9 }));
    const data = await uploadVia(api);
    assert.deepEqual(data, { id: 9 });
    assert.equal(calls[0].url, "http://localhost:18000/documents/upload");
    assert.equal(calls[0].init.method, "POST");
    const body = calls[0].init.body;
    assert.equal(body.entries.length, 1);
    assert.equal(body.entries[0][0], "file");
    assert.equal(body.entries[0][1].name, "notes.txt");
    assert.equal(calls[0].init.headers["Content-Type"], undefined);
    assert.equal(calls[0].init.headers["X-Plugin-Secret"], "secret-a");
  }

  // ------------------------------------------------------- 无凭证 / 认证关闭
  {
    const anonymous = loadApiClient(async () => makeResponse(200, {}));
    await expectApiError(anonymous.request("/plugins/me"), 401, "UNAUTHENTICATED", /凭证缺失/);
  }
  {
    const { api, calls } = respondingWith(makeResponse(401, { detail: "bad secret" }));
    await expectApiError(
      api.request("/plugins/register", { method: "POST", auth: false }),
      401,
      "UNAUTHENTICATED",
      /已失效/,
    );
    assert.equal(api.getPlugin().pluginId, "plugin-a");
    assert.equal(calls[0].init.headers["X-Plugin-ID"], undefined);
  }

  // ------------------------------------------------- 401：清除身份并回调处理器
  {
    const { api } = respondingWith(makeResponse(401, { detail: "bad secret" }));
    let notified = null;
    api.setUnauthenticatedHandler(function (payload) {
      notified = payload;
    });
    await expectApiError(api.request("/plugins/me"), 401, "UNAUTHENTICATED", /已失效/);
    // payload 由沙箱内构造，逐属性断言避免跨 realm 的原型比较。
    assert.equal(notified.pluginId, "plugin-a");
    assert.equal(Object.keys(notified).join(","), "pluginId");
    assert.equal(api.getPlugin().pluginId, null);
  }
  {
    const { api } = respondingWith(makeResponse(401, { detail: "bad secret" }));
    await expectApiError(
      uploadVia(api),
      401,
      "UNAUTHENTICATED",
      /已失效/,
    );
    assert.equal(api.getPlugin().pluginId, null);
  }

  // ---------------------------------------------------------------- 403 禁用
  {
    const { api } = respondingWith(makeResponse(403, { type: "PluginDisabledError" }));
    await expectApiError(api.request("/clips"), 403, "PLUGIN_DISABLED", /禁用/);
  }
  {
    const { api } = respondingWith(makeResponse(403, { detail: "forbidden" }));
    await expectApiError(uploadVia(api), 403, "DISABLED", /禁用/);
  }

  // ------------------------------------------------------- 409：API Key / 名称占用
  // 以下两条走「无结构化字段」的文案兜底：响应既没有 type 也没有 code 时仍要识别得出。
  {
    const { api } = respondingWith(makeResponse(409, { detail: "API Key not configured" }));
    await expectApiError(api.request("/clips"), 409, "API_KEY_NOT_CONFIGURED", /配置模型服务/);
  }
  {
    const { api } = respondingWith(makeResponse(409, { detail: "API Key not configured" }));
    await expectApiError(
      uploadVia(api),
      409,
      "API_KEY_NOT_CONFIGURED",
      /配置模型服务/,
    );
  }
  {
    const { api } = respondingWith(
      makeResponse(409, { type: "ApiKeyNotConfiguredError", detail: "后端换了文案" }),
    );
    await expectApiError(api.request("/clips"), 409, "API_KEY_NOT_CONFIGURED", /配置模型服务/);
  }
  {
    const { api } = respondingWith(
      makeResponse(409, { type: "PluginNameTakenError", detail: "API Key 无关的占用文案" }),
    );
    await expectApiError(
      api.request("/plugins/register", { method: "POST", auth: false }),
      409,
      "PLUGIN_NAME_TAKEN",
      /已经被使用/,
    );
  }
  {
    const { api } = respondingWith(
      makeResponse(409, { code: "INGEST_JOB_CONFLICT", detail: "API Key 相关的冲突文案" }),
    );
    await expectApiError(api.request("/clips/async"), 409, "HTTP");
  }

  // ---------------------------------------------------------------- 422 校验
  {
    const { api } = respondingWith(
      makeResponse(422, { detail: [{ loc: ["body", "chunks"], msg: "invalid" }] }),
    );
    const error = await expectApiError(api.request("/clips"), 422, "VALIDATION");
    assert.match(error.message, /字段: body\.chunks/);
  }

  // ---------------------------------------------------------- 上传专有状态码
  {
    const { api } = respondingWith(makeResponse(413, { detail: "too large" }));
    await expectApiError(uploadVia(api), 413, "FILE_TOO_LARGE");
  }
  {
    const { api } = respondingWith(makeResponse(415, null, { invalidJson: true }));
    await expectApiError(
      uploadVia(api),
      415,
      "UNSUPPORTED_FILE_TYPE",
      /不支持的文件类型/,
    );
  }

  // ---------------------------------------------------------------- 其它错误
  {
    const { api } = respondingWith(makeResponse(500, null, { invalidJson: true }));
    await expectApiError(api.request("/rag/search"), 500, "HTTP", /无法解析的响应/);
  }
  {
    const { api } = respondingWith(makeResponse(502, { detail: "upstream down" }));
    const error = await expectApiError(uploadVia(api), 502, "HTTP");
    assert.equal(error.message, "upstream down");
  }
  {
    const { api } = respondingWith(makeResponse(409, { detail: "conflict reason" }));
    await expectApiError(uploadVia(api), 409, "HTTP");
  }
  {
    const aborted = loadApiClient(async function () {
      const error = new Error("signal aborted");
      error.name = "AbortError";
      throw error;
    });
    aborted.setPluginDetails({ pluginId: "plugin-a", pluginSecret: "secret-a" });
    await expectApiError(aborted.request("/clips"), 0, "TIMEOUT", /超时/);
  }
  {
    const offline = loadApiClient(async function () {
      throw new TypeError("Failed to fetch");
    });
    offline.setPluginDetails({ pluginId: "plugin-a", pluginSecret: "secret-a" });
    await expectApiError(offline.request("/clips"), 0, "NETWORK", /网络连接失败/);
    await expectApiError(uploadVia(offline), 0, "NETWORK");
  }

  console.log("api-client error classification tests passed");
})().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});

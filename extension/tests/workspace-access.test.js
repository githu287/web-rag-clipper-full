"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

async function main() {
  const stored = {}, calls = [];
  let storageFails = false, response = async () => ({ status: 200, ok: true, json: async () => ({ plugin_id: "shared-id", plugin_name: "共同知识库" }) });
  const context = vm.createContext({ URL, TextEncoder, TextDecoder, Uint8Array, crypto: webcrypto, btoa, atob, AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => { calls.push({ url, options }); return response(); },
    chrome: { storage: { local: {
      async get() { return { ...stored }; },
      async set(data) { if (storageFails) throw new Error("fixture storage failure"); Object.assign(stored, structuredClone(data)); },
      async remove(key) { delete stored[key]; },
    } } } });
  for (const name of ["config.js", "api-client.js", "workspace-backup.js"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "..", name), "utf8"), context);
  const api = vm.runInContext("webRagApiClient", context), backup = vm.runInContext("webRagWorkspaceBackup", context);
  assert.equal(api.serverUrl(" HTTPS://KB.EXAMPLE.COM/api/ "), "https://kb.example.com/api");
  for (const invalid of ["http://kb.example.com", "javascript:alert(1)", "https://user:pass@kb.example.com", "https://kb.example.com?a=1", "https://kb.example.com/#key", "https://kb.example.com/a b", "https://kb.example.com\\else"]) assert.throws(() => api.serverUrl(invalid));
  for (const url of ["http://localhost:18001", "http://127.0.0.1:18002", "http://[::1]:18001"]) assert.equal(api.serverUrl(url), url);
  const original = { server_url: "https://kb.example.com/api", plugin_id: "shared-id", plugin_secret: "private-fixture-secret", plugin_name: "共同知识库",
    api_key: "NEVER_EXPORT_API_KEY", documents: ["NEVER_EXPORT_DOCUMENT"], messages: ["NEVER_EXPORT_CHAT"] };
  const verified = await api.verifyConnection(original);
  assert.equal(api.getPlugin().pluginId, null, "probe cannot replace the active connection");
  assert.equal(calls[0].url, "https://kb.example.com/api/plugins/me");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.headers["X-Plugin-Secret"], original.plugin_secret);
  await api.saveConnection(verified);
  await api.plugins.me();
  assert.equal(calls.at(-1).url, "https://kb.example.com/api/plugins/me");
  const saved = JSON.stringify(stored);
  response = async () => ({ status: 401, ok: false, json: async () => ({}) });
  await assert.rejects(api.verifyConnection({ ...original, server_url: "https://other.example.com" }));
  assert.equal(JSON.stringify(stored), saved);
  assert.equal(api.getPlugin().pluginId, original.plugin_id);
  storageFails = true;
  await assert.rejects(api.saveConnection({ ...original, server_url: "https://other.example.com" }));
  assert.equal(api.getServerUrl(), original.server_url);
  storageFails = false;
  let finish;
  response = () => new Promise(resolve => { finish = resolve; });
  const late = api.plugins.me().catch(error => error);
  await api.saveConnection({ ...original, server_url: "https://new.example.com" });
  finish({ status: 401, ok: false, json: async () => ({}) });
  assert.equal((await late).status, 401);
  assert.equal(api.getPlugin().pluginId, original.plugin_id, "old server 401 cannot clear same-ID credentials on a new server");
  assert.equal(api.getServerUrl(), "https://new.example.com");
  const password = "自己的长文件密码-2026";
  const encrypted = await backup.encode(original, password);
  assert(!encrypted.includes(original.plugin_secret) && !encrypted.includes(original.plugin_id) && !encrypted.includes("NEVER_EXPORT"));
  const decoded = await backup.decode(encrypted, password);
  assert.equal(Object.keys(decoded).sort().join(), "plugin_id,plugin_name,plugin_secret,server_url");
  assert.equal(decoded.plugin_secret, original.plugin_secret);
  await assert.rejects(backup.decode(encrypted, "wrong-password-long"), /文件或密码/);
  const tampered = JSON.parse(encrypted); tampered.ciphertext = "AAAA" + tampered.ciphertext.slice(4);
  await assert.rejects(backup.decode(JSON.stringify(tampered), password), /文件或密码/);
  tampered.iterations = 999999999;
  await assert.rejects(backup.decode(JSON.stringify(tampered), password));
  await assert.rejects(backup.decode("x".repeat(32769), password), /32KB/);
  await assert.rejects(backup.encode(original, "short"), /10至256/);
  assert.notEqual(JSON.parse(await backup.encode(original, password)).iv, JSON.parse(encrypted).iv);
  await api.clearPlugin();
  assert(!stored.webRagPlugin && stored.webRagServer === "https://new.example.com");
  await api.loadPlugin();
  assert.equal(api.getServerUrl(), "https://new.example.com");
  assert.equal(api.getPlugin().pluginId, null);
  stored.webRagPlugin = { plugin_id: "legacy-id", plugin_secret: "legacy-secret", plugin_name: "旧工作区" };
  delete stored.webRagServer;
  await api.loadPlugin();
  assert.equal(api.getServerUrl(), "http://localhost:18001");
  assert.equal(api.getPlugin().pluginId, "legacy-id");
  console.log("Workspace access: server binding, safe probes, redirects, late 401 isolation, storage failure, encrypted whitelist backup, tamper/password/size bounds and legacy load passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });

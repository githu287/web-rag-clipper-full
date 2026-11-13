"use strict";

const webRagWorkspacePanel = (() => {
  const el = id => document.getElementById("workspace-" + id);
  let busy = false, committing = false, pending = null;
  const snapshot = () => {
    const p = webRagApiClient.getPlugin();
    return { server_url: webRagApiClient.getServerUrl(), plugin_id: p.pluginId, plugin_secret: p.pluginSecret, plugin_name: p.pluginName };
  };
  const message = (text, settings = false) => { el(settings ? "settings-status" : "status").textContent = text; };
  function refresh() {
    el("server").value = webRagApiClient.getServerUrl();
    el("server-current").textContent = webRagApiClient.getServerUrl();
    el("retry").hidden = !webRagApiClient.getPlugin().pluginId;
    el("export-welcome").disabled = !pending && !webRagApiClient.getPlugin().pluginSecret;
  }
  async function run(action, settings = false) {
    if (busy) return;
    busy = true;
    const controls = [...document.querySelectorAll("[data-workspace-action], #plugin-register-btn")];
    const states = controls.map(node => node.disabled);
    controls.forEach(node => { node.disabled = true; });
    message("正在处理…", settings);
    try { await action(); }
    catch (error) { message(error.message || "操作失败，请重试", settings); }
    finally {
      busy = false;
      controls.forEach((node, index) => { node.disabled = states[index]; });
      el("export-welcome").disabled = !pending && !webRagApiClient.getPlugin().pluginSecret;
    }
  }
  async function backup(password, settings) {
    const source = pending || snapshot(), ticket = JSON.stringify(source);
    const content = await webRagWorkspaceBackup.encode(source, password);
    if (ticket !== JSON.stringify(pending || snapshot())) throw new Error("工作区已变化，请重新导出");
    const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
    try {
      const link = document.createElement("a"); link.href = url; link.download = "web-rag-workspace-login.json"; link.click();
      message("已导出加密登录文件。请另行保存文件密码；文件不包含知识库正文。", settings);
    } finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
  }
  async function created(address, data) {
    pending = webRagApiClient.connection({ ...data, server_url: address });
    try { await commit(pending); pending = null; }
    catch (_error) { refresh(); throw new webRagApiClient.ApiRequestError(0, "LOCAL_STORAGE", "工作区已创建，但本机保存失败。请在下方设置文件密码并先导出登录文件，再尝试登录。"); }
  }
  async function commit(value) {
    committing = true;
    try { await webRagApiClient.saveConnection(value); }
    finally { committing = false; }
  }
  function init(beforeLogout, retry) {
    el("login").addEventListener("submit", event => {
      event.preventDefault();
      run(async () => {
        const candidate = { server_url: el("server").value, plugin_id: el("id").value.trim(), plugin_secret: el("secret").value.trim() };
        // A failed login never overwrites the current connection or clears its credentials.
        const verified = await webRagApiClient.verifyConnection(candidate);
        await commit(verified);
        el("secret").value = ""; location.reload();
      });
    });
    el("import").addEventListener("click", () => run(async () => {
      const file = el("file").files[0];
      if (!file || file.size > 32768) throw new Error("请选择不超过32KB的加密登录文件");
      const recovered = await webRagWorkspaceBackup.decode(await file.text(), el("file-password").value);
      el("server").value = recovered.server_url; el("id").value = recovered.plugin_id; el("secret").value = recovered.plugin_secret;
      message("已解密，尚未连接。请核对服务端地址，再点击“登录已有知识库”。");
    }).finally(() => { el("file-password").value = ""; el("file").value = ""; }));
    el("export").addEventListener("click", () => run(() => backup(el("backup-password").value, true), true).finally(() => { el("backup-password").value = ""; }));
    el("export-welcome").addEventListener("click", () => run(() => backup(el("file-password").value, false)).finally(() => { el("file-password").value = ""; }));
    el("logout").addEventListener("click", () => run(async () => {
      await beforeLogout();
      await webRagApiClient.clearPlugin(); location.reload();
    }, true));
    el("retry").addEventListener("click", () => run(retry));
    chrome.storage.onChanged.addListener((changes, area) => {
      if (committing || area !== "local" || !changes[STORAGE_KEYS.PLUGIN]) return;
      const saved = changes[STORAGE_KEYS.PLUGIN].newValue;
      const now = snapshot();
      if ((saved?.plugin_id || null) !== now.plugin_id || (saved?.plugin_secret || null) !== now.plugin_secret ||
        (saved?.server_url && saved.server_url !== now.server_url)) location.reload();
    });
    refresh();
  }
  function address() {
    try { return webRagApiClient.serverUrl(el("server").value); }
    catch (error) { throw new webRagApiClient.ApiRequestError(0, "VALIDATION", error.message); }
  }
  return { init, refresh, created, run, address };
})();

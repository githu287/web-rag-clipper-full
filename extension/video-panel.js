"use strict";

// Dedicated panel. Only job IDs live on the server; keys and video bytes never enter chrome.storage.
const webRagVideoPanel = (() => {
  const el = id => document.getElementById("video-" + id);
  let owner = null, epoch = 0, active = false, timer = null, jobs = [], selected = null;
  let submitting = false, saving = false, saved = false, edited = false, ingestId = null, config = null;
  const pending = job => ["UPLOADING", "QUEUED", "PROCESSING"].includes(job.status);
  const identity = () => {
    const plugin = webRagApiClient.getPlugin();
    return { id: plugin.pluginId, secret: plugin.pluginSecret };
  };
  function current(ticket) {
    const now = identity();
    return active && ticket === epoch && owner && owner.id === now.id && owner.secret === now.secret;
  }
  function status(message, error = false) {
    el("status").textContent = message;
    el("status").classList.toggle("err", error);
  }
  function leave() { active = false; epoch++; clearTimeout(timer); }
  function reset() {
    leave(); owner = null; jobs = []; selected = null; ingestId = null; config = null;
    submitting = false; saving = false; saved = false; edited = false;
    el("key").value = ""; el("url").value = ""; el("file").value = "";
    el("jobs").replaceChildren(); el("preview-text").value = ""; el("preview").hidden = true;
    el("confirm").checked = false; el("ingest-status").textContent = ""; status("");
  }
  function buttons() {
    el("upload-btn").disabled = submitting || saving || !config || !config.configured || !config.media_tools_available;
    el("link-btn").disabled = el("upload-btn").disabled || !config.link_tools_available;
    el("config-save").disabled = submitting;
    el("config-remove").disabled = submitting || !config || !config.configured;
    el("save").disabled = !selected || !el("confirm").checked || saving || saved || !el("preview-text").value.trim();
    el("preview-text").disabled = saving || saved;
    el("confirm").disabled = saving || saved;
  }
  function preview(job) {
    if (!job.result) return;
    const changed = selected !== job.id;
    if (changed) {
      selected = job.id; edited = false; saved = false; ingestId = null;
      el("confirm").checked = false; el("ingest-status").textContent = "";
      el("confirm").checked = false; el("ingest-status").textContent = "";
    }
    el("preview").hidden = false;
    el("preview-title").textContent = job.result.title;
    const result = job.result;
    el("preview-meta").textContent = `${Math.round(result.duration_seconds)} 秒 · ${result.basis} · ${result.chapters.length} 个分段 · ${result.model}`;
    if (changed || !edited) el("preview-text").value = result.markdown;
    buttons();
  }
  async function action(job, verb) {
    const ticket = epoch;
    try {
      if (verb === "preview") { preview(job); return; }
      await webRagApiClient.videos[verb](job.id);
      if (!current(ticket)) return;
      if (verb === "delete" && selected === job.id) {
        selected = null; el("preview").hidden = true; el("preview-text").value = "";
        ingestId = null; saved = false; saving = false;
      }
      await refresh(ticket);
    } catch (error) { if (current(ticket)) status(error.message, true); }
  }
  function renderJobs() {
    el("jobs").replaceChildren();
    if (!jobs.length) { const p = document.createElement("p"); p.className = "hint"; p.textContent = "还没有视频。第一份笔记，从上方开始。"; el("jobs").append(p); }
    for (const job of jobs) {
      const card = document.createElement("article"); card.className = "video-job";
      const title = document.createElement("strong"); title.textContent = job.result ? job.result.title : job.filename;
      const info = document.createElement("p"); info.textContent = `${job.stage} · ${job.progress}%`;
      const progress = document.createElement("progress"); progress.max = 100; progress.value = job.progress; progress.setAttribute("aria-label", "视频处理进度");
      card.append(title, info, progress);
      if (job.error) { const error = document.createElement("p"); error.className = "err"; error.textContent = job.error; card.append(error); }
      if (job.status === "QUEUED" && Date.now() - Date.parse(job.created_at) > 60000) {
        const wait = document.createElement("p"); wait.className = "hint"; wait.textContent = "排队超过 1 分钟，请检查后端视频处理服务是否运行。"; card.append(wait);
      }
      const actions = document.createElement("div"); actions.className = "video-actions";
      const choices = [["delete", "删除"]];
      if (job.result) choices.unshift(["preview", "查看总结"]);
      if (pending(job)) choices.unshift(["cancel", "取消处理"]);
      if (job.status === "FAILED" && job.attempts < 3 && Date.parse(job.expires_at) > Date.now()) choices.unshift(["retry", "重试（可能产生模型用量）"]);
      for (const [verb, label] of choices) {
        const button = document.createElement("button"); button.type = "button"; button.className = "btn-secondary"; button.textContent = label;
        button.disabled = saving; button.addEventListener("click", () => action(job, verb)); actions.append(button);
      }
      card.append(actions); el("jobs").append(card);
    }
  }
  async function refresh(ticket = epoch) {
    clearTimeout(timer);
    try {
      const data = await webRagApiClient.videos.list();
      if (!current(ticket)) return;
      jobs = data.items || []; renderJobs();
      const chosen = jobs.find(job => job.id === selected);
      if (chosen && chosen.result) preview(chosen);
      if (ingestId) {
        const ingest = await webRagApiClient.jobs.get(ingestId);
        if (!current(ticket)) return;
        el("ingest-status").textContent = ingest.status === "SUCCEEDED" ? "已保存到知识库，可以在 AI 聊天中提问。" :
          ingest.status === "FAILED" ? "入库失败，请在知识库中查看失败记录并重试。" : "总结正在入库，请稍候…";
        if (["SUCCEEDED", "FAILED"].includes(ingest.status)) { ingestId = null; saving = false; buttons(); }
      }
    } catch (error) { if (current(ticket)) status(error.message, true); }
    if (current(ticket) && (jobs.some(pending) || ingestId)) timer = setTimeout(() => refresh(ticket), 3000);
  }
  async function enter() {
    const now = identity();
    if (!owner || owner.id !== now.id || owner.secret !== now.secret) { reset(); owner = now; }
    active = true; const ticket = ++epoch;
    submitting = false; buttons();
    try {
      const data = await webRagApiClient.videos.config();
      if (!current(ticket)) return;
      config = data;
      el("base-url").value = data.base_url;
      el("model").replaceChildren();
      for (const name of data.models) { const option = document.createElement("option"); option.value = name; option.textContent = name; el("model").append(option); }
      el("model").value = data.model;
      el("config-state").textContent = data.configured ? "已保存" : "待配置";
      el("config-details").open = !data.configured;
      el("limits").textContent = `最大 ${Math.floor(data.max_upload_bytes / 1048576)} MB、${Math.floor(data.max_duration_seconds / 60)} 分钟。原素材最多保留 ${data.raw_retention_hours} 小时，总结 ${data.result_retention_days} 天；成功后删除原素材。`;
      if (!data.media_tools_available) status("服务端尚未安装视频处理工具，请安装 requirements-video.txt。", true);
      buttons(); await refresh(ticket);
    } catch (error) { if (current(ticket)) status(error.message, true); }
  }
  async function submit(kind, event) {
    event.preventDefault(); if (submitting) return;
    const ticket = epoch;
    try {
      const file = el("file").files[0];
      if (kind === "upload" && (!file || file.size > config.max_upload_bytes || !file.size)) throw new Error("请选择非空且未超出大小限制的视频文件。");
      if (kind === "link" && !el("url").value.trim()) throw new Error("请先粘贴视频链接。");
      submitting = true; buttons(); status(kind === "upload" ? "正在上传，请保持面板打开…" : "正在创建链接解析任务…");
      const job = kind === "upload" ? await webRagApiClient.videos.upload(file) : await webRagApiClient.videos.link(el("url").value.trim());
      if (!current(ticket)) return;
      status("任务已创建；处理进度保存在后端，重新打开面板可继续查看。");
      selected = job.id; edited = false; saved = false; ingestId = null;
      el("preview").hidden = true; el("preview-text").value = "";
      el("file").value = ""; el("url").value = ""; await refresh(ticket);
    } catch (error) { if (current(ticket)) status(error.message, true); }
    finally { if (current(ticket)) { submitting = false; buttons(); } }
  }
  el("upload-form").addEventListener("submit", event => submit("upload", event));
  el("link-form").addEventListener("submit", event => submit("link", event));
  el("refresh").addEventListener("click", () => enter());
  el("confirm").addEventListener("change", buttons);
  el("preview-text").addEventListener("input", () => { edited = true; el("confirm").checked = false; buttons(); });
  el("config-form").addEventListener("submit", async event => {
    event.preventDefault(); if (submitting) return;
    const ticket = epoch; submitting = true; buttons();
    try {
      await webRagApiClient.videos.setConfig({ api_key: el("key").value, base_url: el("base-url").value.trim(), model: el("model").value });
      if (current(ticket)) { status("配置已加密保存。首次视频分析会验证模型权限。请确认地域与服务地址一致。"); await enter(); }
    } catch (error) { if (current(ticket)) status(error.message, true); }
    finally { el("key").value = ""; if (current(ticket)) { submitting = false; buttons(); } }
  });
  el("config-remove").addEventListener("click", async () => {
    const ticket = epoch;
    try { await webRagApiClient.videos.clearConfig(); if (current(ticket)) { status("视频配置已移除，未完成任务已取消；已发出的云端请求可能仍会产生用量。"); await enter(); } }
    catch (error) { if (current(ticket)) status(error.message, true); }
  });
  el("save").addEventListener("click", async () => {
    if (el("save").disabled) return;
    const ticket = epoch; saving = true; buttons(); renderJobs();
    try {
      const file = new File([el("preview-text").value], "video-summary-" + selected + ".md", { type: "text/markdown" });
      const job = await webRagApiClient.documents.uploadFileAsync(file);
      if (!current(ticket)) return;
      saved = true; ingestId = job.id; el("ingest-status").textContent = "已提交总结，正在入库…"; await refresh(ticket);
    } catch (error) {
      if (current(ticket)) { saving = false; el("ingest-status").textContent = error.message + "；若请求超时，请先在知识库中确认，避免重复保存。"; }
    } finally { if (current(ticket)) { buttons(); renderJobs(); } }
  });
  el("download").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([el("preview-text").value], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "video-summary.md"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  if (chrome.storage.onChanged) chrome.storage.onChanged.addListener((_changes, area) => {
    if (area === "local" && owner) { const now = identity(); if (now.id !== owner.id || now.secret !== owner.secret) reset(); }
  });
  window.addEventListener("pagehide", reset);
  return { enter, leave, reset };
})();

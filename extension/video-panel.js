"use strict";

// Dedicated panel. Only job IDs live on the server; keys and video bytes never enter chrome.storage.
const webRagVideoPanel = (() => {
  const el = id => document.getElementById("video-" + id);
  let owner = null, epoch = 0, active = false, timer = null, jobs = [], selected = null;
  let submitting = false, saving = false, saved = false, edited = false, ingestId = null, config = null;
  let chatBusy = false, chatToken = 0;
  const chats = new Map();
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
  function stopWaitingForAnswer() {
    if (chatBusy) el("chat-status").textContent = "已停止等待回答；云端请求可能仍在处理中，可稍后重新提问。";
    chatToken++; chatBusy = false;
  }
  function leave() { active = false; epoch++; clearTimeout(timer); stopWaitingForAnswer(); }
  function reset() {
    leave(); owner = null; jobs = []; selected = null; ingestId = null; config = null;
    submitting = false; saving = false; saved = false; edited = false;
    chats.clear(); clearChatView(); el("part-options").replaceChildren();
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
    el("save-parts").disabled = saving;
    el("ask").disabled = !selected || chatBusy || !config || !config.configured || !el("question").value.trim();
    el("question").disabled = chatBusy;
  }
  function clearChatView() {
    chatToken++; chatBusy = false;
    el("question").value = ""; el("chat-status").textContent = ""; el("chat-messages").replaceChildren();
  }
  function renderChat() {
    el("chat-messages").replaceChildren();
    for (const message of chats.get(selected) || []) {
      const card = document.createElement("article"); card.className = "video-message " + message.role;
      const label = document.createElement("strong"); label.textContent = message.role === "user" ? "你" : "视频助手";
      const text = document.createElement("p"); text.textContent = message.content;
      card.append(label, text);
      if (message.sources && message.sources.length) {
        const sources = document.createElement("small");
        sources.textContent = "依据：" + message.sources.map(source => source.start_seconds == null ? source.label :
          `${source.label} ${formatTime(source.start_seconds)}–${formatTime(source.end_seconds)}（分段范围）`).join("；");
        card.append(sources);
      }
      el("chat-messages").append(card);
    }
    el("chat-messages").scrollTop = el("chat-messages").scrollHeight;
  }
  function formatTime(seconds) {
    const value = Math.floor(seconds);
    return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  }
  function selectedMarkdown(job) {
    const result = job.result;
    const sections = [];
    const options = el("part-options").querySelectorAll("input[type=checkbox]");
    for (const option of options) {
      if (!option.checked) continue;
      if (option.value === "overview") sections.push("## 整体摘要\n\n" + result.overview);
      else {
        const index = Number(option.value), chapter = result.chapters[index];
        sections.push(`## 片段 ${index + 1} · ${formatTime(chapter.start_seconds)}–${formatTime(chapter.end_seconds)}\n\n${chapter.summary}`);
      }
    }
    if (!sections.length) return "";
    return `# ${result.title}\n\n> AI 生成的视频总结节选，请核对原视频。\n\n来源：${job.source_url || job.filename}\n\n依据：${result.basis}。时间为分段边界，不是逐句字幕时间。\n\n` + sections.join("\n\n");
  }
  function renderParts(job) {
    el("part-options").replaceChildren();
    const choices = [["overview", "整体摘要"], ...job.result.chapters.map((chapter, index) =>
      [String(index), `片段 ${index + 1} · ${formatTime(chapter.start_seconds)}–${formatTime(chapter.end_seconds)}`])];
    for (const [value, text] of choices) {
      const label = document.createElement("label"); label.className = "video-confirm";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = value; input.checked = true;
      input.addEventListener("change", () => {
        el("preview-text").value = selectedMarkdown(job); edited = true; saved = false;
        el("confirm").checked = false; el("ingest-status").textContent = "已按所选部分重新生成待保存内容，可继续编辑。"; buttons();
      });
      label.append(input, document.createTextNode(text)); el("part-options").append(label);
    }
  }
  function preview(job) {
    if (!job.result) return;
    const changed = selected !== job.id;
    if (changed) {
      selected = job.id; edited = false; saved = false; ingestId = null;
      el("confirm").checked = false; el("ingest-status").textContent = "";
      clearChatView();
    }
    if (changed || !el("part-options").children.length) renderParts(job);
    el("preview").hidden = false;
    el("preview-title").textContent = job.result.title;
    const result = job.result;
    el("preview-meta").textContent = `${Math.round(result.duration_seconds)} 秒 · ${result.basis} · ${result.chapters.length} 个分段 · ${result.model}`;
    if (changed || !edited) el("preview-text").value = result.markdown;
    el("retention-note").textContent = `此总结临时保留至 ${new Date(job.expires_at).toLocaleString()}。可先直接问答；勾选需要的部分再长期入库。调整勾选会重新生成下方内容。`;
    renderChat();
    buttons();
  }
  async function action(job, verb) {
    const ticket = epoch;
    try {
      if (verb === "preview") { preview(job); return; }
      await webRagApiClient.videos[verb](job.id);
      if (!current(ticket)) return;
      if (verb === "delete" && selected === job.id) {
        chats.delete(job.id); clearChatView(); el("part-options").replaceChildren();
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
      if (selected && !chosen) {
        chats.delete(selected); selected = null; clearChatView(); el("part-options").replaceChildren();
        el("preview").hidden = true; el("preview-text").value = ""; buttons();
      }
      if (ingestId) {
        const ingest = await webRagApiClient.jobs.get(ingestId);
        if (!current(ticket)) return;
        el("ingest-status").textContent = ingest.status === "SUCCEEDED" ? "已保存到知识库，可以在 AI 聊天中提问。" :
          ingest.status === "FAILED" ? "入库失败，请在知识库中查看失败记录并重试。" : "总结正在入库，请稍候…";
        if (["SUCCEEDED", "FAILED"].includes(ingest.status)) { ingestId = null; saving = false; saved = ingest.status === "SUCCEEDED"; buttons(); }
      }
    } catch (error) { if (current(ticket)) status(error.message, true); }
    if (current(ticket) && (jobs.some(pending) || ingestId)) timer = setTimeout(() => refresh(ticket), 3000);
  }
  async function enter() {
    const now = identity();
    if (!owner || owner.id !== now.id || owner.secret !== now.secret) { reset(); owner = now; }
    stopWaitingForAnswer();
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
      clearChatView(); el("part-options").replaceChildren(); el("confirm").checked = false;
      el("preview").hidden = true; el("preview-text").value = "";
      el("file").value = ""; el("url").value = ""; await refresh(ticket);
    } catch (error) { if (current(ticket)) status(error.message, true); }
    finally { if (current(ticket)) { submitting = false; buttons(); } }
  }
  el("upload-form").addEventListener("submit", event => submit("upload", event));
  el("link-form").addEventListener("submit", event => submit("link", event));
  el("refresh").addEventListener("click", () => enter());
  el("confirm").addEventListener("change", buttons);
  el("question").addEventListener("input", buttons);
  el("chat-clear").addEventListener("click", () => { chats.delete(selected); clearChatView(); buttons(); });
  el("chat-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (el("ask").disabled) return;
    const ticket = epoch, jobId = selected, token = ++chatToken;
    const query = el("question").value.trim();
    const messages = chats.get(jobId) || [];
    // A small recent window for follow-up references; answers never become evidence on the server.
    const history = [];
    let length = 0;
    for (const message of messages.slice(-10).reverse()) {
      const content = message.content.slice(0, 4000);
      if (length + content.length > 16000) break;
      history.unshift({ role: message.role, content }); length += content.length;
    }
    chatBusy = true; buttons(); el("chat-status").textContent = "正在根据这份视频总结回答…";
    try {
      const response = await webRagApiClient.videos.ask(jobId, query, history);
      if (!current(ticket) || selected !== jobId || token !== chatToken) return;
      messages.push({ role: "user", content: query }, { role: "assistant", content: response.answer, sources: response.sources });
      chats.set(jobId, messages.slice(-40)); el("question").value = ""; renderChat();
      el("chat-status").textContent = "回答依据 AI 视频总结。未保存到知识库，也未保存对话。";
    } catch (error) {
      if (current(ticket) && selected === jobId && token === chatToken) el("chat-status").textContent = error.message;
    } finally {
      if (current(ticket) && selected === jobId && token === chatToken) { chatBusy = false; buttons(); }
    }
  });
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

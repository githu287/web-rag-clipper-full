"use strict";

// Keys and raw video bytes never enter chrome.storage; player URLs live only in this panel.
const webRagVideoPanel = (() => {
  const el = id => document.getElementById("video-" + id);
  let owner = null, epoch = 0, active = false, timer = null, jobs = [], selected = null;
  let submitting = false, saving = false, saved = false, edited = false, ingestId = null, config = null;
  let chatBusy = false, chatToken = 0;
  const chats = new Map();
  const localMedia = new Map();
  let transcriptJob = null, pendingSeek = null;
  const pending = job => ["UPLOADING", "QUEUED", "PROCESSING"].includes(job.status);
  const identity = () => {
    const plugin = webRagApiClient.getPlugin();
    return { id: plugin.pluginId, secret: plugin.pluginSecret };
  };
  function current(ticket) {
    const now = identity();
    return active && ticket === epoch && owner && owner.id === now.id && owner.secret === now.secret;
  }
  function sameOwner(expected) {
    const now = identity();
    return owner === expected && owner && owner.id === now.id && owner.secret === now.secret;
  }
  function status(message, error = false) {
    el("status").textContent = message;
    el("status").classList.toggle("err", error);
  }
  function stopWaitingForAnswer() {
    if (chatBusy) el("chat-status").textContent = "已停止等待回答；云端请求可能仍在处理中，可稍后重新提问。";
    chatToken++; chatBusy = false;
  }
  function leave() { active = false; epoch++; clearTimeout(timer); stopWaitingForAnswer(); el("player").pause(); }
  function reset() {
    leave(); owner = null; jobs = []; selected = null; ingestId = null; config = null;
    submitting = false; saving = false; saved = false; edited = false;
    chats.clear(); clearChatView(); el("part-options").replaceChildren();
    clearMedia(); transcriptJob = null; el("transcript-list").replaceChildren(); el("transcript-state").textContent = "";
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
    el("transcript-choices").disabled = saving;
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
        sources.append(document.createTextNode("依据："));
        for (const source of message.sources) {
          if (source.start_seconds == null) { sources.append(document.createTextNode(source.label)); continue; }
          const link = document.createElement("button"); link.type = "button"; link.className = "video-time-link";
          link.textContent = `${source.label} ${formatTime(source.start_seconds)}–${formatTime(source.end_seconds)}（${source.timestamp_basis === "asr_alignment" ? "ASR时间范围" : "分段范围"}）`;
          link.addEventListener("click", () => seekTo(source.start_seconds)); sources.append(link);
          if (source.text) { const quote = document.createElement("span"); quote.className = "video-source-quote"; quote.textContent = source.text; sources.append(quote); }
        }
        card.append(sources);
      }
      el("chat-messages").append(card);
    }
    el("chat-messages").scrollTop = el("chat-messages").scrollHeight;
  }
  function formatTime(seconds) {
    const value = Math.floor(seconds);
    return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}.${Math.floor((seconds - value) * 10)}`;
  }
  function clearPlayer() {
    const player = el("player"); player.pause(); player.removeAttribute("src"); player.load(); player.hidden = true;
    player.dataset.job = ""; pendingSeek = null; el("local-source").value = "";
    el("playback-status").textContent = "请选择同一原视频用于定位，文件不会再次上传。";
  }
  function clearMedia() {
    clearPlayer();
    for (const media of localMedia.values()) URL.revokeObjectURL(media.url);
    localMedia.clear();
  }
  function rememberMedia(jobId, file) {
    if (localMedia.has(jobId)) URL.revokeObjectURL(localMedia.get(jobId).url);
    localMedia.set(jobId, { url: URL.createObjectURL(file), name: file.name });
    while (localMedia.size > 3) { const oldest = localMedia.keys().next().value; URL.revokeObjectURL(localMedia.get(oldest).url); localMedia.delete(oldest); }
  }
  function renderPlayer(job) {
    const player = el("player"), media = localMedia.get(job.id);
    if (!media) { if (player.dataset.job !== job.id) clearPlayer(); return; }
    if (player.getAttribute("src") !== media.url) {
      clearPlayer(); player.dataset.job = job.id; player.src = media.url; player.hidden = false;
      el("playback-status").textContent = "视频仅在当前面板本机播放；点击时间可回看核对。关闭面板后释放。";
    }
  }
  function seekTo(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0 || !selected) return;
    const player = el("player");
    if (!localMedia.has(selected) || player.dataset.job !== selected || player.error) {
      el("playback-status").textContent = `请先选择同一原视频，再定位到 ${formatTime(seconds)}；文件不会再次上传。`;
      el("local-source").focus(); return;
    }
    if (player.readyState < 1) { pendingSeek = seconds; return; }
    const job = jobs.find(item => item.id === selected);
    if (!job || !Number.isFinite(player.duration) || Math.abs(player.duration - job.result.duration_seconds) > 1) {
      const media = localMedia.get(selected); if (media) URL.revokeObjectURL(media.url);
      localMedia.delete(selected); clearPlayer(); el("playback-status").textContent = "所选视频时长与当前任务不一致，请选择同一原视频。"; return;
    }
    player.currentTime = Math.min(seconds, Math.max(0, player.duration - .01));
    player.scrollIntoView({ block: "nearest" });
    player.play().catch(() => { el("playback-status").textContent = "已定位，请点击播放核对原视频。"; });
  }
  el("player").addEventListener("loadedmetadata", () => {
    const player = el("player"), job = jobs.find(item => item.id === selected);
    if (!active || player.dataset.job !== selected || !job || !job.result) return;
    if (!Number.isFinite(player.duration) || Math.abs(player.duration - job.result.duration_seconds) > 1) {
      const media = localMedia.get(selected); if (media) URL.revokeObjectURL(media.url);
      localMedia.delete(selected); clearPlayer(); el("playback-status").textContent = "所选视频时长与当前任务不一致，请选择同一原视频。"; return;
    }
    if (pendingSeek != null) { const time = pendingSeek; pendingSeek = null; seekTo(time); }
  });
  el("player").addEventListener("error", () => {
    if (el("player").getAttribute("src")) el("playback-status").textContent = "浏览器无法播放该视频格式，可在外部播放器按时间核对；建议使用 MP4。";
  });
  el("local-source").addEventListener("change", () => {
    const file = el("local-source").files[0], job = jobs.find(item => item.id === selected);
    if (!file || !job) return;
    rememberMedia(job.id, file); renderPlayer(job);
  });
  function regenerate(job) {
    el("preview-text").value = selectedMarkdown(job); edited = true; saved = false;
    el("confirm").checked = false; el("ingest-status").textContent = "已按所选部分重新生成待保存内容，可继续编辑。"; buttons();
  }
  function renderTranscript(job) {
    if (transcriptJob === job.id) return;
    transcriptJob = job.id; el("transcript-list").replaceChildren();
    const transcript = job.result.transcript || {};
    const states = { ready: "云端 ASR 识别原文，时间为对齐估计，可能误识别。可逐段勾选保存，点击时间回看。",
      disabled: "此任务未启用原文转写。可在视频模型配置中启用，再上传生成。", no_audio: "原视频没有音轨，仅提供画面总结。",
      no_speech: "云端未识别到可转写语音。", failed: "原文转写失败，当前仅提供 AI 总结。" };
    el("transcript-state").textContent = (states[transcript.status] || states.disabled) + (transcript.error ? " " + transcript.error : "");
    for (const row of transcript.status === "ready" ? transcript.sentences : []) {
      const item = document.createElement("article"); item.className = "video-transcript-row";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = String(row.id); input.setAttribute("aria-label", `保存原文 ${row.id}`);
      input.addEventListener("change", () => {
        const all = [...el("transcript-list").querySelectorAll("input")], option = el("part-options").querySelector('input[value="transcript"]');
        if (option) { option.checked = all.every(box => box.checked); option.indeterminate = !option.checked && all.some(box => box.checked); }
        regenerate(job);
      });
      const body = document.createElement("div"), time = document.createElement("button"); time.type = "button"; time.className = "video-time-link";
      time.textContent = `原文 ${row.id} · ${formatTime(row.start_seconds)}–${formatTime(row.end_seconds)}`;
      time.addEventListener("click", () => seekTo(row.start_seconds));
      const text = document.createElement("p"); text.textContent = row.text; body.append(time, text); item.append(input, body); el("transcript-list").append(item);
    }
  }
  function selectedMarkdown(job) {
    const result = job.result;
    const sections = [];
    const options = el("part-options").querySelectorAll("input[type=checkbox]");
    for (const option of options) {
      if (!option.checked) continue;
      if (option.value === "overview") sections.push("## 整体摘要\n\n" + result.overview);
      else if (option.value === "transcript") continue;
      else {
        const index = Number(option.value), chapter = result.chapters[index];
        sections.push(`## 片段 ${index + 1} · ${formatTime(chapter.start_seconds)}–${formatTime(chapter.end_seconds)}\n\n${chapter.summary}`);
      }
    }
    const selectedRows = new Set([...el("transcript-list").querySelectorAll("input:checked")].map(input => Number(input.value)));
    const original = (result.transcript && result.transcript.sentences || []).filter(row => selectedRows.has(row.id));
    if (original.length) sections.push("## 云端 ASR 转写原文\n\n> 时间为 ASR 对齐估计，原文可能误识别。\n\n" + original.map(row =>
      `### 原文 ${row.id} · ${formatTime(row.start_seconds)}–${formatTime(row.end_seconds)}\n\n${row.text}`).join("\n\n"));
    if (!sections.length) return "";
    return `# ${result.title}\n\n> 视频笔记节选，请核对原视频。\n\n来源：${job.source_url || job.filename}\n\n总结依据：${result.basis}。总结时间为分段边界；原文时间为 ASR 对齐估计。\n\n` + sections.join("\n\n");
  }
  function renderParts(job) {
    el("part-options").replaceChildren();
    const choices = [["overview", "整体摘要"], ...job.result.chapters.map((chapter, index) =>
      [String(index), `片段 ${index + 1} · ${formatTime(chapter.start_seconds)}–${formatTime(chapter.end_seconds)}`])];
    if (job.result.transcript && job.result.transcript.status === "ready") choices.push(["transcript", "全部转写原文（也可在上方逐段勾选）"]);
    for (const [value, text] of choices) {
      const label = document.createElement("label"); label.className = "video-confirm";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = value; input.checked = value !== "transcript";
      input.addEventListener("change", () => {
        if (value === "transcript") for (const box of el("transcript-list").querySelectorAll("input")) box.checked = input.checked;
        regenerate(job);
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
    renderTranscript(job); renderPlayer(job);
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
      if (verb === "delete") {
        chats.delete(job.id);
        if (localMedia.has(job.id)) { URL.revokeObjectURL(localMedia.get(job.id).url); localMedia.delete(job.id); }
      }
      if (verb === "delete" && selected === job.id) {
        clearPlayer(); transcriptJob = null; el("transcript-list").replaceChildren();
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
        if (localMedia.has(selected)) { URL.revokeObjectURL(localMedia.get(selected).url); localMedia.delete(selected); }
        clearPlayer(); transcriptJob = null; el("transcript-list").replaceChildren();
        chats.delete(selected); selected = null; clearChatView(); el("part-options").replaceChildren();
        el("preview").hidden = true; el("preview-text").value = ""; buttons();
      }
      if (ingestId) {
        const ingest = await webRagApiClient.jobs.get(ingestId);
        if (!current(ticket)) return;
        el("ingest-status").textContent = ingest.status === "SUCCEEDED" ? "已保存到知识库，可以在 AI 聊天中提问。" :
          ingest.status === "FAILED" ? "入库失败，请在知识库中查看失败记录并重试。" : "总结正在入库，请稍候…";
        if (["SUCCEEDED", "FAILED"].includes(ingest.status)) {
          ingestId = null; saving = false; saved = ingest.status === "SUCCEEDED"; buttons(); renderJobs();
        }
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
      el("asr-enabled").checked = data.asr_enabled === true;
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
      clearPlayer(); selected = job.id; edited = false; saved = false; ingestId = null;
      if (kind === "upload") rememberMedia(job.id, file);
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
    chatBusy = true; buttons(); el("chat-status").textContent = "正在根据这份视频资料回答…";
    try {
      const response = await webRagApiClient.videos.ask(jobId, query, history);
      if (!current(ticket) || selected !== jobId || token !== chatToken) return;
      messages.push({ role: "user", content: query }, { role: "assistant", content: response.answer, sources: response.sources });
      chats.set(jobId, messages.slice(-40)); el("question").value = ""; renderChat();
      el("chat-status").textContent = `回答依据：${response.basis || "视频资料"}。未保存到知识库，也未保存对话。`;
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
      await webRagApiClient.videos.setConfig({ api_key: el("key").value, base_url: el("base-url").value.trim(), model: el("model").value, asr_enabled: el("asr-enabled").checked });
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
    // Navigation changes the display epoch, but a submitted document still needs
    // its receipt. Only a Workspace reset invalidates this save operation.
    const saveOwner = owner; saving = true; buttons(); renderJobs();
    try {
      const file = new File([el("preview-text").value], "video-summary-" + selected + ".md", { type: "text/markdown" });
      const job = await webRagApiClient.documents.uploadFileAsync(file);
      if (!sameOwner(saveOwner)) return;
      saved = true; ingestId = job.id; el("ingest-status").textContent = "已提交总结，正在入库…";
      if (active) await refresh(epoch);
    } catch (error) {
      if (sameOwner(saveOwner)) { saving = false; el("ingest-status").textContent = error.message + "；若请求超时，请先在知识库中确认，避免重复保存。"; }
    } finally { if (sameOwner(saveOwner)) { buttons(); renderJobs(); } }
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

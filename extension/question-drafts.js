"use strict";

// 输入事件立即排队写入，不依赖面板关闭事件。身份只在内存比较，存储使用不可逆指纹。
const webRagQuestionDrafts = (() => {
  function create(options) {
    let owner = null, generation = 0, edit = 0, queue = Promise.resolve();
    const same = (a, b) => !!a && !!b && a.pluginId === b.pluginId &&
      a.pluginSecret === b.pluginSecret && a.sessionId === b.sessionId;
    const current = (value, token) => token === generation && same(value, options.owner());
    async function tag(value) {
      const bytes = new TextEncoder().encode(value.pluginSecret);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
    }
    function enqueue(task) {
      const pending = queue.then(task, task);
      queue = pending.catch(() => {});
      return pending;
    }
    function activate(value) {
      if (same(owner, value)) return;
      owner = value && { ...value };
      const token = ++generation, revision = ++edit;
      options.status("");
      if (!value || !value.pluginId || !value.pluginSecret || !value.sessionId) return;
      const snapshot = { ...value };
      // 等待此面板先前的输入落盘，但读取不阻塞新输入；迟到的读取不能覆盖编辑。
      return queue.then(async () => {
        const draft = await sessionStore.getQuestionDraft(snapshot.pluginId, snapshot.sessionId, await tag(snapshot));
        if (!current(snapshot, token) || revision !== edit || options.query().trim() || !draft) return;
        options.restore(draft);
        options.status("已恢复本地草稿 · 请确认范围后发送");
      }).catch(() => {
        if (current(snapshot, token) && revision === edit) options.status("草稿读取失败，可点击找回草稿重试");
      });
    }
    function save(query, context) {
      const snapshot = options.owner();
      if (!same(owner, snapshot) || !snapshot.pluginId || !snapshot.pluginSecret || !snapshot.sessionId) return Promise.resolve(false);
      const token = generation, revision = ++edit;
      // 捕获文本和范围，禁止在 await 后再读可变的输入框或 Tab Binding。
      const value = { query, context: context && { ...context } };
      options.status("正在保存草稿…");
      return enqueue(async () => {
        try {
          const ok = await sessionStore.saveQuestionDraft(snapshot.pluginId, snapshot.sessionId, await tag(snapshot), value);
          if (current(snapshot, token) && revision === edit) options.status(ok
            ? (query.trim() ? "草稿已保存在本机" : "") : "草稿未保存，请保留当前面板并重试编辑");
          return ok;
        } catch (_err) {
          if (current(snapshot, token) && revision === edit) options.status("草稿未保存，请保留当前面板并重试编辑");
          return false;
        }
      });
    }
    async function list() {
      const snapshot = options.owner(), token = generation;
      if (!snapshot || !snapshot.pluginId || !snapshot.pluginSecret) return [];
      await queue;
      const result = await sessionStore.getQuestionDrafts(snapshot.pluginId, await tag(snapshot));
      return current(snapshot, token) ? result : [];
    }
    async function clearPlugin(pluginId) {
      generation++; edit++;
      owner = null;
      await enqueue(() => sessionStore.clearQuestionDrafts(pluginId));
    }
    return { activate, save, list, clearPlugin, flush: () => queue };
  }
  return { create };
})();

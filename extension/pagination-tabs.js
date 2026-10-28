// Service-worker ownership of temporary pagination tabs. Disconnects clean up
// even when the sidepanel closes while chrome.tabs.create is still resolving.
"use strict";
(function () {
  const STORAGE = "webRagPaginationTabs";
  const records = new Map();
  const leases = new Set();
  let persistence = Promise.resolve();
  function persist() {
    const snapshot = Array.from(records.values());
    persistence = persistence.catch(function () {}).then(function () { return chrome.storage.session.set({ [STORAGE]: snapshot }); });
    return persistence;
  }
  async function close(record) {
    let released = false;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let tab;
      try { tab = await chrome.tabs.get(record.id); }
      catch (_error) { released = true; break; } // Already closed.
      if (record.claimed || tab.active || tab.pinned) { released = true; break; }
      try { await chrome.tabs.remove(record.id); released = true; break; }
      catch (_error) { await new Promise(function (resolve) { setTimeout(resolve, 200); }); }
    }
    if (released) records.delete(record.id);
    await persist();
    return released;
  }
  const recovered = (async function () {
    const saved = await chrome.storage.session.get(STORAGE);
    for (const record of saved[STORAGE] || []) {
      if (record && Number.isInteger(record.id)) records.set(record.id, record);
    }
    for (const record of Array.from(records.values())) await close(record);
    await persist();
  })();
  chrome.tabs.onActivated.addListener(function (info) {
    const record = records.get(info.tabId);
    if (!record) return;
    record.claimed = true;
    persist().catch(function () {});
    for (const lease of leases) if (lease.tabs.has(info.tabId)) {
      try { lease.port.postMessage({ event: "claimed" }); } catch (_error) {}
    }
  });
  chrome.runtime.onConnect.addListener(function (port) {
    if (port.name !== "web-rag-pagination" || !port.sender || port.sender.id !== chrome.runtime.id ||
        !String(port.sender.url || "").startsWith(chrome.runtime.getURL(""))) return;
    const lease = { port, tabs: new Set(), connected: true, busy: false };
    leases.add(lease);
    port.onDisconnect.addListener(function () {
      lease.connected = false;
      leases.delete(lease);
      for (const id of lease.tabs) { const record = records.get(id); if (record) close(record).catch(function () {}); }
    });
    port.onMessage.addListener(function (message) {
      async function handle() {
        await recovered;
        if (!lease.connected) return;
        if (message.action === "release") {
          if (lease.tabs.has(message.tabId)) {
            const record = records.get(message.tabId);
            if (record && !await close(record)) throw new Error("分页标签页暂时无法关闭");
            lease.tabs.delete(message.tabId);
          }
          return {};
        }
        if (message.action !== "create" || lease.busy || lease.tabs.size || !Number.isInteger(message.sourceTabId)) throw new Error("分页标签页请求无效");
        lease.busy = true;
        try {
          const source = await chrome.tabs.get(message.sourceTabId);
          if (webRagPagination.key(source.url) !== webRagPagination.key(message.sourceUrl) ||
              !webRagPagination.nextURL(message.previousUrl, message.url) || new URL(source.url).origin !== new URL(message.url).origin) throw new Error("分页来源或链接已变化");
          if (!lease.connected) return;
          const tab = await chrome.tabs.create({ url: message.url, active: false, windowId: source.windowId });
          const record = { id: tab.id, claimed: !!tab.active };
          records.set(tab.id, record);
          lease.tabs.add(tab.id);
          await persist();
          if (!lease.connected) { await close(record); return; }
          return { tabId: tab.id };
        } finally { lease.busy = false; }
      }
      handle().then(function (result) {
        if (lease.connected && result) port.postMessage({ id: message.id, ...result });
      }).catch(function () {
        if (lease.connected) { try { port.postMessage({ id: message.id, error: "无法创建或清理分页标签页" }); } catch (_error) {} }
      });
    });
  });
})();

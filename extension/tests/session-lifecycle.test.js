"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

function fixture() {
  const values = {}, locks = new Map();
  let gate = null, rejectDelete = false, rejectCreate = false;
  const sharedLocks = { request(name, task) {
    const next = (locks.get(name) || Promise.resolve()).then(task);
    locks.set(name, next.catch(() => {})); return next;
  } };
  function panel() {
    const context = vm.createContext({ console, navigator: { locks: sharedLocks }, chrome: { storage: { local: {
      async get(key) { return { [key]: clone(values[key]) }; },
      async set(items) {
        if (gate) { const current = gate; gate = null; await current.promise; }
        if (rejectDelete && Object.entries(items).some(([key, value]) => key.startsWith("webRagSession_") && value === null)) throw Error("delete storage failure");
        if (rejectCreate) throw Error("create storage failure");
        Object.assign(values, clone(items));
      },
      async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete values[key]; },
    } } } });
    for (const name of ["config", "session-store"]) vm.runInContext(fs.readFileSync(path.join(root, name + ".js"), "utf8"), context);
    return vm.runInContext("sessionStore", context);
  }
  return { values, panel, block() { gate = deferred(); return gate; },
    rejectDeletion(value) { rejectDelete = value; }, rejectCreation(value) { rejectCreate = value; } };
}

(async () => {
  {
    const f = fixture(), a = f.panel(), b = f.panel();
    const first = await a.createSession("a"), foreign = await a.createSession("b");
    await a.setCurrentSessionId("a", first.sessionId);
    await a.setCurrentSessionId("b", foreign.sessionId);
    assert.equal(await b.setCurrentSessionId("b", first.sessionId), false);
    assert.equal(await b.deleteSession(first.sessionId, "b"), false);
    assert.equal(await b.saveSession({ ...first, pluginId: "b", messages: [] }), false);
    await a.saveQuestionDraft("a", first.sessionId, "tag", { query: "私有草稿", context: { mode: "all" } });
    f.rejectDeletion(true);
    await assert.rejects(a.deleteSession(first.sessionId, "a"));
    assert(await a.getSession(first.sessionId));
    assert(await a.getQuestionDraft("a", first.sessionId, "tag"));
    assert.equal(await a.getCurrentSessionId("a"), first.sessionId);
    f.rejectDeletion(false);
    const gate = f.block();
    const saving = a.saveSession({ ...first, messages: [{ id: "m1", role: "assistant", content: "迟到回答" }] });
    const deleting = b.deleteSession(first.sessionId, "a");
    gate.resolve(); await Promise.all([saving, deleting]);
    assert.equal(await a.getSession(first.sessionId), null);
    assert.equal(await a.getCurrentSessionId("a"), null);
    assert.equal(await a.getCurrentSessionId("b"), foreign.sessionId);
    assert.equal(await a.saveSession(first), false);
    assert.equal(await b.saveQuestionDraft("a", first.sessionId, "tag", { query: "旧面板", context: null }), false);
    assert.equal((await a.getSessionsByPlugin("a")).length, 0);
    assert(!f.values["webRagQuestionDraft_" + first.sessionId]);
  }
  {
    const f = fixture(), a = f.panel(), b = f.panel();
    const original = await a.createSession("a");
    const gate = f.block();
    const first = a.saveSession({ ...original, messages: [{ id: "left", createdAt: 1 }] });
    const second = b.saveSession({ ...original, messages: [{ id: "right", createdAt: 2 }] });
    gate.resolve(); await Promise.all([first, second]);
    assert.equal((await a.getSession(original.sessionId)).messages.map(x => x.id).join(","), "left,right");
  }
  {
    const f = fixture(), a = f.panel();
    const oldest = await a.createSession("a");
    await a.saveQuestionDraft("a", oldest.sessionId, "tag", { query: "配额草稿", context: null });
    const foreign = await a.createSession("b");
    let latest;
    for (let i = 0; i < 20; i++) latest = await a.createSession("a");
    await a.enforceSessionLimit("a", latest.sessionId);
    assert.equal((await a.getSessionsByPlugin("a")).length, 20);
    assert.equal(await a.getSession(oldest.sessionId), null);
    assert.equal(await a.saveSession(oldest), false);
    assert(await a.getSession(foreign.sessionId));
    assert(!f.values["webRagQuestionDraft_" + oldest.sessionId]);
    f.rejectCreation(true);
    await assert.rejects(a.createSession("a"), /无法保存新会话/);
  }
  console.log("Session lifecycle: owner checks, cross-panel locks, atomic deletion payload, failed storage, late saves and quota eviction passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

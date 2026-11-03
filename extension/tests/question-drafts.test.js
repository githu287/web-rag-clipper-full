"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const root = path.resolve(__dirname, "..");
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

async function fixture() {
  const values = {};
  let gate = null, fail = false;
  const context = vm.createContext({ console, TextEncoder, crypto: webcrypto, chrome: { storage: { local: {
    async get(key) { return { [key]: clone(values[key]) }; },
    async set(items) {
      if (gate) { const pending = gate; gate = null; await pending.promise; }
      if (fail) throw Error("quota");
      Object.assign(values, clone(items));
    },
    async remove(key) { if (fail) throw Error("quota"); delete values[key]; },
  } } } });
  for (const name of ["config", "session-store", "question-drafts"]) vm.runInContext(fs.readFileSync(path.join(root, name + ".js"), "utf8"), context);
  const store = vm.runInContext("sessionStore", context);
  const draftFactory = vm.runInContext("webRagQuestionDrafts", context);
  const first = await store.createSession("a");
  let owner = { pluginId: "a", pluginSecret: "private-random-secret", sessionId: first.sessionId };
  let query = "", restored = [], status = "";
  const controller = draftFactory.create({ owner: () => ({ ...owner }), query: () => query,
    restore: value => { query = value.query; restored.push(clone(value)); }, status: value => { status = value; } });
  await controller.activate(owner);
  return { store, controller, values, first,
    get owner() { return owner; }, set owner(value) { owner = value; controller.activate(owner); },
    get query() { return query; }, set query(value) { query = value; },
    get restored() { return restored; }, get status() { return status; },
    fail(value) { fail = value; }, block() { gate = deferred(); return gate; },
  };
}
const scope = { mode: "current", documentId: 77, pageTitle: "原文档", pageUrl: "https://example.com/a" };
(async () => {
  {
    const f = await fixture();
    f.query = "中文草稿\nsecond line";
    await f.controller.save(f.query, scope);
    const records = await f.controller.list();
    assert.equal(records.length, 1);
    assert.equal(records[0].context.documentId, 77);
    const encoded = JSON.stringify(f.values);
    assert(!encoded.includes(f.owner.pluginSecret));
    assert.equal(records[0].credentialTag.length, 64);
    // A later message save must not overwrite or resurrect a draft.
    await f.store.saveSession({ ...f.first, messages: [{ id: "answer", role: "assistant", content: "迟到回答" }] });
    assert.equal((await f.controller.list())[0].query, f.query);
    f.query = "";
    f.controller.activate(null); await f.controller.activate(f.owner);
    assert.equal(f.query, "中文草稿\nsecond line");
    assert.equal(f.restored[0].context.documentId, 77);
    await f.controller.save("", null);
    await f.store.saveSession(f.first);
    assert.equal((await f.controller.list()).length, 0);
  }
  {
    const f = await fixture();
    const mutable = { ...scope };
    const gate = f.block();
    const one = f.controller.save("旧编辑", mutable);
    mutable.documentId = 999;
    const two = f.controller.save("新编辑", { mode: "all" });
    const clear = f.controller.save("", null);
    gate.resolve();
    await Promise.all([one, two, clear]);
    assert.equal((await f.controller.list()).length, 0, "queued old writes cannot resurrect a submitted draft");
    const oneMore = f.block();
    const saved = f.controller.save("发送后新输入", scope);
    oneMore.resolve(); await saved;
    assert.equal((await f.controller.list())[0].query, "发送后新输入");
  }
  {
    const f = await fixture();
    await f.controller.save("旧会话草稿", scope);
    const oldOwner = f.owner;
    const second = await f.store.createSession("a");
    f.owner = { ...oldOwner, sessionId: second.sessionId };
    await f.controller.save("第二会话", { mode: "all" });
    assert.equal((await f.controller.list()).length, 2);
    f.owner = { ...oldOwner, pluginSecret: "rotated-secret" };
    assert.equal((await f.controller.list()).length, 0);
    const foreign = await f.store.createSession("b");
    f.owner = { pluginId: "b", pluginSecret: "secret-b", sessionId: foreign.sessionId };
    assert.equal((await f.controller.list()).length, 0);
    assert.equal(await f.store.saveQuestionDraft("b", f.first.sessionId, "tag", { query: "污染", context: scope }), false);
    f.query = ""; f.owner = oldOwner;
    f.query = "读取期间已经输入";
    await f.controller.save(f.query, { mode: "all" });
    await tick();
    assert.equal(f.restored.length, 0, "late restore cannot overwrite typing or cross identities");
    assert.equal((await f.controller.list()).find(x => x.sessionId === f.first.sessionId).query, f.query);
  }
  {
    const f = await fixture();
    f.query = "保存失败也保留在输入框";
    f.fail(true);
    assert.equal(await f.controller.save(f.query, scope), false);
    assert.match(f.status, /未保存/);
    assert.equal(f.query, "保存失败也保留在输入框");
    f.fail(false);
    await f.controller.save(f.query, { mode: "current", documentId: -1, pluginSecret: "must-not-store" });
    assert.equal((await f.controller.list())[0].context, null);
    assert(!JSON.stringify(f.values).includes("must-not-store"));
    await f.store.deleteSession(f.first.sessionId);
    assert.equal(await f.controller.save("不能复活", scope), false);
    assert(!f.values["webRagQuestionDraft_" + f.first.sessionId]);
  }
  {
    const f = await fixture();
    const gate = f.block();
    const save = f.controller.save("删除空间前的输入", scope);
    const clear = f.controller.clearPlugin("a");
    gate.resolve(); await Promise.all([save, clear]);
    assert.equal(Object.keys(f.values).filter(k => k.startsWith("webRagQuestionDraft_")).length, 0);
  }
  console.log("Draft persistence, scope snapshots, queued clear, recovery races, identity isolation, storage failure and deletion passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

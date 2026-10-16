"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionDir = path.resolve(__dirname, "..");
const values = {};
const chrome = {
  storage: {
    local: {
      async get(key) {
        if (typeof key === "string") return { [key]: values[key] };
        return Object.assign({}, values);
      },
      async set(items) {
        Object.assign(values, items);
      },
      async remove(key) {
        delete values[key];
      },
    },
  },
};

const context = vm.createContext({ chrome, console });
vm.runInContext(
  fs.readFileSync(path.join(extensionDir, "config.js"), "utf8"),
  context,
);
vm.runInContext(
  fs.readFileSync(path.join(extensionDir, "session-store.js"), "utf8") +
    "\nglobalThis.__sessionStore = sessionStore;",
  context,
);

(async function run() {
  const store = context.__sessionStore;
  await store.setUploadJob("plugin-a", {
    jobId: "job-1",
    documentId: 11,
    filename: "one.txt",
  });
  await store.setUploadJob("plugin-a", {
    jobId: "job-2",
    documentId: 22,
    filename: "two.md",
  });

  assert.equal((await store.getUploadJobs("plugin-a")).length, 2);
  assert.equal((await store.getUploadJob("plugin-a", 11)).jobId, "job-1");
  assert.equal((await store.getUploadJob("plugin-a", 22)).jobId, "job-2");

  await store.clearUploadJob("plugin-a", 11);
  assert.equal(await store.getUploadJob("plugin-a", 11), null);
  assert.equal((await store.getUploadJobs("plugin-a")).length, 1);

  await store.clearUploadJob("plugin-a");
  assert.equal((await store.getUploadJobs("plugin-a")).length, 0);
  console.log("session-store upload job tests passed");

  // ------------------------------------------- saveSession 并发覆盖（roadmap #14）
  {
    const base = [{ id: "m-0", role: "user", content: "第一条", createdAt: 100 }];
    const shared = {
      sessionId: "s-concurrent",
      pluginId: "plugin-a",
      title: "并发会话",
      createdAt: 100,
      updatedAt: 100,
      messages: base.slice(),
    };
    assert.equal(await store.saveSession(shared), true);

    // 两个持有同一份旧副本的调用方各自追加一条消息后同时保存。
    const panelOne = Object.assign({}, shared, {
      messages: base.concat([{ id: "m-1", role: "assistant", content: "回复一", createdAt: 200 }]),
    });
    const panelTwo = Object.assign({}, shared, {
      messages: base.concat([{ id: "m-2", role: "user", content: "提问二", createdAt: 300 }]),
    });
    await Promise.all([store.saveSession(panelOne), store.saveSession(panelTwo)]);

    const saved = await store.getSession("s-concurrent");
    assert.deepEqual(
      saved.messages.map(function (m) { return m.id; }),
      ["m-0", "m-1", "m-2"],
      "并发保存必须保留双方各自追加的消息"
    );
    assert.equal(
      values.webRagSessions["s-concurrent"].messageCount,
      saved.messages.length,
      "索引 messageCount 必须与详情条数一致"
    );

    // 重复保存同一份消息不能产生重复项（合并按 id 去重）。
    await store.saveSession(saved);
    assert.equal((await store.getSession("s-concurrent")).messages.length, 3);
  }
  {
    // 合并后仍受配额约束：100 上限，两个调用方各加一条 → 保留最新 100 条。
    const MAX_MESSAGES_PER_SESSION = 100;
    const many = [];
    for (let i = 0; i < MAX_MESSAGES_PER_SESSION; i++) {
      many.push({ id: "c-" + i, role: "user", content: "c" + i, createdAt: i });
    }
    const seed = {
      sessionId: "s-cap",
      pluginId: "plugin-a",
      title: "配额会话",
      createdAt: 0,
      updatedAt: 0,
      messages: many.slice(),
    };
    await store.saveSession(seed);
    const storedSeed = await store.getSession("s-cap");
    const first = Object.assign({}, seed, {
      messages: storedSeed.messages.concat([{ id: "c-new", role: "assistant", content: "新回复", createdAt: 5000 }]),
    });
    const second = Object.assign({}, seed, {
      messages: storedSeed.messages.concat([{ id: "c-new-2", role: "user", content: "新提问", createdAt: 6000 }]),
    });
    await Promise.all([store.saveSession(first), store.saveSession(second)]);

    const capped = await store.getSession("s-cap");
    const ids = capped.messages.map(function (m) { return m.id; });
    assert.equal(ids.length, MAX_MESSAGES_PER_SESSION);
    assert.equal(new Set(ids).size, MAX_MESSAGES_PER_SESSION, "合并不得产生重复消息");
    assert.ok(ids.indexOf("c-new") !== -1);
    assert.ok(ids.indexOf("c-new-2") !== -1);
    assert.equal(ids[ids.length - 1], "c-new-2", "必须按 createdAt 保留最新一条");
    assert.ok(ids.indexOf("c-0") === -1, "最旧的消息应被裁掉");
    assert.equal(values.webRagSessions["s-cap"].messageCount, MAX_MESSAGES_PER_SESSION);
  }
  console.log("session-store save concurrency tests passed");
})().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});

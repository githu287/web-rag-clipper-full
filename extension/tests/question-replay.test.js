"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../question-replay.js"), "utf8") + "\nglobalThis.replay = webRagQuestionReplay;", context);
const replay = context.replay;
const scope = { mode: "current", documentId: 5, pageUrl: "https://example.com/old", pageTitle: "Old document" };
const user = { id: "u1", role: "user", content: " 原问题？ ", ...scope };
const answer = { id: "a1", role: "assistant", ...scope };
const plain = value => JSON.parse(JSON.stringify(value));

assert.deepEqual(plain(replay.resolve([user, answer], answer)), { query: "原问题？", context: scope });
const durable = { ...answer, replyTo: "evicted-parent", question: "Saved snapshot" };
assert.equal(replay.resolve([durable], durable).query, "Saved snapshot");
assert.equal(replay.resolve([answer], answer), null);
assert.equal(replay.resolve([user, { role: "assistant" }, answer], answer), null);
assert.equal(replay.resolve([{ ...user, documentId: 6 }, answer], answer), null);
assert.equal(replay.resolve([{ ...user, mode: "all" }, answer], answer), null);
assert.equal(replay.resolve([user, { ...answer, replyTo: "missing" }], { ...answer, replyTo: "missing" }), null);
for (const broken of [{ ...answer, question: "" }, { ...answer, mode: undefined, question: "q" },
  { ...answer, documentId: null, question: "q" }, { ...answer, documentId: true, question: "q" },
  { ...answer, documentId: "5", question: "q" }]) assert.equal(replay.resolve([user, broken], broken), null);
const global = { role: "assistant", mode: "all", documentId: 999, question: "Global question" };
assert.equal(replay.resolve([], global).context.documentId, null);
assert.equal(replay.resolve([], global).context.mode, "all");
const linked = { ...answer, replyTo: "u1" };
assert.equal(replay.resolve([user, { role: "assistant" }, linked], linked).query, "原问题？");
assert.equal(replay.resolve([user], user), null);
assert.equal(replay.resolve([user, { ...answer, replyTo: 123 }], { ...answer, replyTo: 123 }), null);
console.log("Replay question snapshots, explicit parents, strict legacy adjacency and original scopes passed");

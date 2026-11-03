"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../source-check.js"), "utf8") + "\nglobalThis.checker = webRagSourceCheck;", context);
const checker = context.checker;
const plain = value => JSON.parse(JSON.stringify(value));
const ref = (id, revision = 2) => ({ document_id: id, content_revision: revision });
const current = () => true;

(async () => {
  const refs = [ref(1), ref(1, 0), ref(2, null), ref(3), ref(4)];
  let calls = 0;
  const states = await checker.query(refs, async sent => {
    calls++;
    assert.deepEqual(plain(sent), refs);
    return { sources: sent.map((s, i) => ({ ...s, state: ["current", "changed", "unversioned", "missing", "unavailable"][i] })) };
  }, current);
  assert.deepEqual(plain(states), ["current", "changed", "unversioned", "missing", "unavailable"]);
  assert.equal(calls, 1);
  assert.equal(checker.overall(states), "changed");
  assert.match(checker.summary(states), /来源已变化/);
  assert.equal(checker.overall(["current", "unknown"]), "unknown");
  assert.equal(checker.overall(["current", "unversioned"]), "unversioned");
  assert.equal(checker.overall(["current"]), "current");

  // Missing/invalid historical values are never silently converted to version 0.
  let legacy;
  const legacyStates = await checker.query([{ document_id: 1 }, ref(0), ref("2"), ref(3, -1), ref(4, Number.MAX_SAFE_INTEGER + 1), null],
    async sent => { legacy = plain(sent); return { sources: [{ ...sent[0], state: "unversioned" }] }; }, current);
  assert.deepEqual(legacy, [ref(1, null)]);
  assert.deepEqual(plain(legacyStates), ["unversioned", "unknown", "unknown", "unknown", "unknown", "unknown"]);
  assert.deepEqual(plain(await checker.query([null], () => { throw new Error("must not send"); }, current)), ["unknown"]);
  await assert.rejects(checker.query(Array(101).fill(ref(1)), () => { throw new Error("must not send"); }, current), /limit/);

  // A response must cover each exact requested citation, in order.
  for (const data of [{}, { sources: [] }, { sources: [{ ...ref(9), state: "current" }] },
    { sources: [{ ...ref(1, 3), state: "current" }] }, { sources: [{ ...ref(1), state: "<img>" }] }]) {
    await assert.rejects(checker.query([ref(1)], async () => data, current));
  }
  await assert.rejects(checker.query([ref(1, null)], async sent => ({ sources: [{ ...sent[0], state: "current" }] }), current));
  let attempts = 0;
  await assert.rejects(checker.query([ref(1)], async () => { attempts++; throw new Error("offline"); }, current), /offline/);
  assert.equal(attempts, 1);

  // A stale workspace/view cannot send another request or apply a late reply.
  assert.equal(await checker.query([ref(1)], () => { throw new Error("must not send"); }, () => false), null);
  let resolve, active = true;
  const pending = checker.query([ref(1)], () => new Promise(yes => { resolve = yes; }), () => active);
  active = false;
  resolve({ sources: [{ ...ref(1), state: "current" }] });
  assert.equal(await pending, null);
  console.log("Historical source versions, bounded checks, malformed responses, failures and late-identity guards passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

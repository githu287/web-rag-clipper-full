"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  const elements = [], blobs = new Map(), revoked = [], timers = new Map(), states = [];
  const listeners = {};
  let sequence = 0;
  const player = { textTracks: { addEventListener: (name, fn) => { listeners[name] = fn; } },
    append(element) { elements.push(element); } };
  const context = vm.createContext({ Blob,
    URL: { createObjectURL(blob) { const url = "blob:subtitle-" + ++sequence; blobs.set(url, blob); return url; },
      revokeObjectURL(url) { revoked.push(url); blobs.delete(url); } },
    document: { createElement(name) {
      assert.equal(name, "track");
      const element = { dataset: {}, track: { mode: "disabled", cues: [{}, {}] },
        remove() { const index = elements.indexOf(element); if (index >= 0) elements.splice(index, 1); } };
      return element;
    } },
    setTimeout(fn, ms) { assert.equal(ms, 5000); const token = ++sequence; timers.set(token, fn); return token; },
    clearTimeout(token) { timers.delete(token); },
  });
  for (const file of ["video-subtitles.js", "video-captions.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", file), "utf8"), context);
  }
  const captions = vm.runInContext("webRagVideoCaptions", context).create(player, (state, count) => states.push([state, count]));
  const job = { status: "SUCCEEDED", result: { title: "自己的样例", duration_seconds: 11,
    transcript: { status: "ready", sentences: [
      { id: 1, start_seconds: .235, end_seconds: .835, text: "不要浇水80%。 <img src=x>" },
      { id: 2, start_seconds: 10.235, end_seconds: 10.835, text: "Keep English & 谢谢谢谢。" },
    ] } } };
  let current = true;
  captions.show(job, () => current);
  const first = elements[0], firstUrl = first.src;
  assert.equal(first.kind, "subtitles");
  assert.equal(first.track.mode, "showing");
  assert((await blobs.get(firstUrl).text()).includes("00:00:10.235 --> 00:00:10.835"));
  assert((await blobs.get(firstUrl).text()).includes("&lt;img src=x&gt;"));
  first.onload();
  assert.deepEqual(states.at(-1), ["shown", 2]);
  assert.equal(timers.size, 0);
  captions.clear();
  assert.equal(elements.length, 0);
  assert.equal(blobs.size, 0);
  assert(revoked.includes(firstUrl));

  captions.show(job, () => current);
  const stale = elements[0], oldTimeout = [...timers.values()][0];
  captions.show(job, () => current);
  const replacement = elements[0], stateCount = states.length;
  stale.onload(); stale.onerror(); oldTimeout();
  assert.equal(elements[0], replacement, "late callbacks cannot remove the replacement track");
  assert.equal(states.length, stateCount, "late callbacks cannot overwrite current status");
  current = false;
  replacement.onload();
  assert.equal(elements.length, 0, "identity/job changes before load must discard the track");
  assert.equal(blobs.size, 0);
  assert.equal(states.at(-1)[0], "off");
  const countBefore = sequence;
  captions.show(job, () => current);
  assert.equal(sequence, countBefore, "a stale context cannot even create a Blob URL");

  current = true;
  captions.show(job, () => current);
  const incomplete = elements[0]; incomplete.track.cues = [{}]; incomplete.onload();
  assert.deepEqual(states.at(-1), ["failed", 0]);
  assert.equal(elements.length, 0);
  assert.equal(timers.size, 0);

  captions.show(job, () => current);
  [...timers.values()][0]();
  assert.deepEqual(states.at(-1), ["failed", 0]);
  assert.equal(blobs.size, 0, "a load timeout releases the private subtitle Blob");

  captions.show(job, () => current);
  const switchedOffWhileLoading = elements[0]; switchedOffWhileLoading.track.mode = "disabled";
  switchedOffWhileLoading.onload();
  assert.equal(states.at(-1)[0], "off");

  captions.show(job, () => current);
  const native = elements[0]; native.onload(); native.track.mode = "disabled"; listeners.change();
  assert.equal(states.at(-1)[0], "off");
  assert.equal(blobs.size, 0, "switching off native captions must also release our track");

  player.append = () => { throw new Error("unsupported track"); };
  captions.show(job, () => current);
  assert.deepEqual(states.at(-1), ["failed", 0]);
  assert.equal(timers.size, 0);
  assert.equal(blobs.size, 0);
  assert.throws(() => captions.show({ status: "FAILED" }, () => true), /完整转写/);
  assert.equal(blobs.size, 0);
  console.log("Local captions: source ranges, native controls, private URL cleanup, timeout, partial parsing and stale identity/job callbacks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });

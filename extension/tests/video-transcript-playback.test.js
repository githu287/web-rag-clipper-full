"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const events = {}, actions = {};
let current = false, focused = null, writes = 0, text = "";
const player = { currentTime: 0, addEventListener: (name, fn) => { events[name] = fn; } };
const list = { scrollTop: 50, getBoundingClientRect: () => ({ top: 100 }) };
const status = { get textContent() { return text; }, set textContent(value) { text = value; writes++; } };
const locate = { disabled: false, addEventListener: (name, fn) => { actions[name] = fn; } };
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "video-transcript-playback.js"), "utf8"), context);
const playback = vm.runInContext("webRagVideoTranscriptPlayback", context).create(player, list, status, locate, () => current);
function row(id, start, end) {
  const classes = new Set();
  const element = { hidden: false,
    classList: { add: name => classes.add(name), remove: name => classes.delete(name) },
    getBoundingClientRect: () => ({ top: 250 }),
    querySelector: selector => { assert.equal(selector, ".video-time-link"); return {
      focus(options) { assert.equal(options.preventScroll, true); focused = id; }
    }; } };
  return { id, start, end, element, classes };
}
const first = row(1, .235, .835), overlap = row(2, .5, 1), next = row(3, 1, 2);
const bad = row(4, NaN, 3), reverse = row(5, 3, 2), negative = row(6, -1, 2);
playback.setRows([first, overlap, next, bad, reverse, negative]);
assert(locate.disabled);
current = true;
player.currentTime = .235; events.seeking();
assert(first.classes.has("is-current"));
assert(!overlap.classes.size && !bad.classes.size && !reverse.classes.size && !negative.classes.size);
const beforeWrites = writes;
events.timeupdate(); assert.equal(writes, beforeWrites, "stable ranges must not spam live status");
player.currentTime = .6; events.timeupdate();
assert(first.classes.has("is-current") && overlap.classes.has("is-current"));
assert(text.includes("1、2"));
first.element.hidden = overlap.element.hidden = true; playback.update();
assert(locate.disabled && text.includes("被搜索隐藏"));
actions.click();
assert.equal(list.scrollTop, 50); assert.equal(focused, null);
overlap.element.hidden = false; playback.update();
assert(!locate.disabled);
actions.click();
assert.equal(list.scrollTop, 200); assert.equal(focused, 2);
assert.equal(player.currentTime, .6, "locate cannot seek or start playback");
assert(first.element.hidden, "locate cannot clear a search filter");
player.currentTime = .835; events.seeked();
assert(!first.classes.size && overlap.classes.has("is-current"), "end times are exclusive");
player.currentTime = 1; events.timeupdate();
assert(!overlap.classes.size && next.classes.has("is-current"));
player.currentTime = 2; events.ended();
assert(!next.classes.size && locate.disabled && text.includes("没有对应"));
player.currentTime = .1; events.timeupdate();
assert(locate.disabled, "no approximate nearest sentence in gaps");
player.currentTime = Infinity; events.timeupdate(); assert(locate.disabled);
player.currentTime = 1.5; events.timeupdate(); assert(next.classes.size);
current = false; events.error();
assert(!next.classes.size && locate.disabled);
events.seeked(); actions.click();
assert(!next.classes.size && locate.disabled, "late media/click events cannot restore an old identity/source");
current = true; playback.setRows([]); events.timeupdate();
assert(!next.classes.size && locate.disabled, "replacing or clearing a task releases old row references");
const many = Array.from({ length: 3000 }, (_value, index) => row(index + 1, 1, 2));
playback.setRows(many);
assert(many.every(item => item.classes.has("is-current")));
assert(text.includes("等 3000 条") && text.length < 150, "overlapping ranges need bounded accessible status");
playback.clear();
assert(many.every(item => !item.classes.size));
console.log("Playback transcript: exact half-open ranges, overlaps, gaps, literal filter preservation, manual list scroll, stable live status and stale context cleanup passed");

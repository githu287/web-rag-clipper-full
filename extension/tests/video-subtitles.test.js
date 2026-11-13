"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../video-subtitles.js"), "utf8"), context);
const exporter = vm.runInContext("webRagVideoSubtitles", context);
const fixture = () => ({
  status: "SUCCEEDED", api_key: "DO-NOT-EXPORT-KEY", plugin_secret: "DO-NOT-EXPORT-SECRET",
  source_url: "https://example.com/video?token=DO-NOT-EXPORT-URL",
  result: { title: "中文 / English 📝 <字幕>", duration_seconds: 3700,
    overview: "DO-NOT-EXPORT-SUMMARY", markdown: "DO-NOT-EXPORT-PREVIEW",
    transcript: { status: "ready", api_key: "DO-NOT-EXPORT-ASR-KEY", sentences: [
      { id: 7, start_seconds: .235, end_seconds: 1.835, text: "不要浇水80%。" },
      { id: 12, start_seconds: 59.9996, end_seconds: 61.5, text: "Keep English & <img src=x> literal.\r\n\r\n \r\n00:01 --> 00:02\r\nnot a new cue" },
      { id: 19, start_seconds: 3599.9996, end_seconds: 3601.001, text: "谢谢谢谢，3.5元。" },
    ] } },
});
const job = fixture(), before = JSON.stringify(job);
const srt = exporter.render(job, { format: "srt" });
assert.equal(srt.count, 3);
assert(srt.content.startsWith("\ufeff1\n00:00:00,235 --> 00:00:01,835\n不要浇水80%。\n\n"));
assert(srt.content.includes("2\n00:01:00,000 --> 00:01:01,500"));
assert(srt.content.includes("3\n01:00:00,000 --> 01:00:01,001"));
assert(srt.content.includes("Keep English &amp; &lt;img src=x&gt; literal.\n00:01 --&gt; 00:02\nnot a new cue"));
assert(srt.content.includes("谢谢谢谢，3.5元。"));
assert(!srt.content.includes("DO-NOT-EXPORT"));
assert(srt.filename.startsWith("字幕-中文 _ English 📝 _字幕_"));
assert(srt.filename.endsWith(".srt"));
assert(!/[<>:"/\\|?*\r\n]/.test(srt.filename));
assert.equal(JSON.stringify(job), before, "export must not mutate the stored transcript or preview");

const vtt = exporter.render(job, { format: "vtt", selectedIds: [19, 7, 19] });
assert(vtt.content.startsWith("WEBVTT\n\nNOTE "));
assert(vtt.content.includes("1\n00:00:00.235 --> 00:00:01.835\n不要浇水80%。"));
assert(vtt.content.includes("2\n01:00:00.000 --> 01:00:01.001\n谢谢谢谢"));
assert(!vtt.content.includes("Keep English"));
assert.equal(vtt.count, 2, "selection preserves source order and original video times, without duplicating IDs");
assert.equal(vtt.mime, "text/vtt;charset=utf-8");
assert(vtt.filename.endsWith(".vtt"));

for (const status of ["disabled", "no_audio", "no_speech", "failed", "processing", null]) {
  const target = fixture(); target.result.transcript.status = status;
  assert.throws(() => exporter.render(target), /完整转写原文/);
}
for (const status of ["QUEUED", "PROCESSING", "FAILED", "CANCELLED"]) {
  const target = fixture(); target.status = status;
  assert.throws(() => exporter.render(target), /完整转写原文/);
}
assert.throws(() => exporter.render(null), /完整转写原文/);
assert.throws(() => exporter.render(job, { format: "ass" }), /格式/);
assert.throws(() => exporter.render(job, { selectedIds: [] }), /勾选/);
assert.throws(() => exporter.render(job, { selectedIds: [999] }), /已变化/);
assert.throws(() => exporter.render(job, { selectedIds: "7" }), /勾选/);
for (const change of [
  row => { row.start_seconds = NaN; }, row => { row.start_seconds = Infinity; },
  row => { row.start_seconds = -.1; }, row => { row.start_seconds = ".2"; },
  row => { row.end_seconds = true; }, row => { row.end_seconds = 3701; },
  row => { row.end_seconds = row.start_seconds; }, row => { row.end_seconds = row.start_seconds + .00001; },
  row => { row.text = ""; }, row => { row.text = "text\u0000"; }, row => { row.id = 0; },
]) {
  const target = fixture(); change(target.result.transcript.sentences[0]);
  assert.throws(() => exporter.render(target));
}
for (const damage of ["duplicate", "backwards", "too_many", "too_long", "empty", "bad_duration"]) {
  const target = fixture(), rows = target.result.transcript.sentences;
  if (damage === "duplicate") rows[1].id = 7;
  if (damage === "backwards") rows[1].start_seconds = .1;
  if (damage === "too_many") target.result.transcript.sentences = Array.from({ length: 3001 }, () => rows[0]);
  if (damage === "too_long") rows[0].text = "字".repeat(120001);
  if (damage === "empty") rows.length = 0;
  if (damage === "bad_duration") target.result.duration_seconds = Infinity;
  assert.throws(() => exporter.render(target));
}
const overlap = fixture();
overlap.result.duration_seconds = 2;
overlap.result.transcript.sentences = [
  { id: 1, start_seconds: 0, end_seconds: 1.201, text: "前句" },
  { id: 2, start_seconds: 1.199, end_seconds: 2.004, text: "后句" },
];
assert(exporter.render(overlap).content.includes("00:00:01,199 --> 00:00:02,004"), "preserve provider overlap and historic duration rounding");
console.log("SRT/VTT: Chinese/English text, exact source ranges, rollover, literal cue syntax, selection, bounds and credential isolation passed");

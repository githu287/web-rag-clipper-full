"use strict";

// Local export of completed ASR only. Never derive subtitles from AI summaries.
const webRagVideoSubtitles = (() => {
  function milliseconds(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || !Number.isSafeInteger(Math.round(value * 1000))) {
      throw new Error("原文时间戳无效，无法导出字幕。请核对原视频。");
    }
    return Math.round(value * 1000);
  }
  function timestamp(value, format) {
    const total = milliseconds(value), pad = (part, size = 2) => String(part).padStart(size, "0");
    return pad(Math.floor(total / 3600000)) + ":" + pad(Math.floor(total / 60000) % 60) + ":" +
      pad(Math.floor(total / 1000) % 60) + (format === "srt" ? "," : ".") + pad(total % 1000, 3);
  }
  function cueText(value) {
    // Empty lines terminate cues; markup and arrows must remain literal text.
    return value.replace(/\r\n?/g, "\n").trim().replace(/\n(?:[\t ]*\n)+/g, "\n")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function filename(title, format) {
    const safe = (typeof title === "string" ? title : "视频")
      .replace(/[<>:"/\\|?*\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "_")
      .replace(/\s+/g, " ").replace(/^[. ]+|[. ]+$/g, "");
    return "字幕-" + (Array.from(safe).slice(0, 60).join("") || "视频") + "." + format;
  }
  function render(job, { format = "srt", selectedIds = null } = {}) {
    if (format !== "srt" && format !== "vtt") throw new Error("请选择 SRT 或 VTT 字幕格式。");
    const result = job && job.result, transcript = result && result.transcript;
    if (!job || job.status !== "SUCCEEDED" || !transcript || transcript.status !== "ready" ||
        !Array.isArray(transcript.sentences) || !transcript.sentences.length) {
      throw new Error("此视频没有可导出的完整转写原文；AI 总结不能代替字幕。");
    }
    if (transcript.sentences.length > 3000) throw new Error("原文片段数量超过字幕导出上限。");
    if (typeof result.duration_seconds !== "number" || !Number.isFinite(result.duration_seconds) || result.duration_seconds <= 0) {
      throw new Error("视频时长无效，无法核对字幕时间。");
    }
    const ids = new Set(); let previous = -1, characters = 0;
    let rows = transcript.sentences.map(row => {
      if (!row || !Number.isSafeInteger(row.id) || row.id <= 0 || ids.has(row.id)) {
        throw new Error("原文编号无效或重复，请重新打开视频后再导出。");
      }
      ids.add(row.id);
      const start = milliseconds(row.start_seconds), end = milliseconds(row.end_seconds);
      // Result durations were historically rounded to 2 decimals, words to 3.
      if (row.start_seconds >= row.end_seconds || start >= end || row.start_seconds < previous ||
          row.end_seconds > result.duration_seconds + .01) {
        throw new Error("原文时间范围无效、倒退或不足1毫秒，无法可靠导出字幕。");
      }
      previous = row.start_seconds;
      if (typeof row.text !== "string" || !row.text.trim() || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(row.text)) {
        throw new Error("原文包含空内容或无效控制字符，无法导出字幕。");
      }
      characters += row.text.length;
      return { id: row.id, start: row.start_seconds, end: row.end_seconds, text: row.text };
    });
    if (characters > 120000) throw new Error("原文长度超过字幕导出上限。");
    if (selectedIds !== null) {
      if (!Array.isArray(selectedIds) || !selectedIds.length) throw new Error("请先勾选需要导出的原文片段。");
      if (selectedIds.some(id => !ids.has(id))) throw new Error("勾选原文已变化，请重新选择后导出。");
      const selected = new Set(selectedIds);
      rows = rows.filter(row => selected.has(row.id));
    }
    const cues = rows.map((row, index) => (index + 1) + "\n" + timestamp(row.start, format) + " --> " +
      timestamp(row.end, format) + "\n" + cueText(row.text)).join("\n\n");
    const header = format === "vtt" ? "WEBVTT\n\nNOTE 云端ASR识别字幕，时间为识别估计，请核对原视频。\n\n" : "\ufeff";
    return { content: header + cues + "\n", filename: filename(result.title, format), count: rows.length,
      mime: format === "vtt" ? "text/vtt;charset=utf-8" : "application/x-subrip;charset=utf-8" };
  }
  return { render };
})();

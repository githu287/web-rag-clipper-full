// Ordered overlap merging for successive forward-scroll snapshots. Repeated
// paragraphs within a snapshot remain intact; code fences are atomic units.
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagSnapshotMerge = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function units(text) {
    const output = [];
    let blank = "";
    let fence = 0;
    let code = "";
    let prefix = "";
    for (const line of String(text || "").split("\n")) {
      const marker = line.trim().match(/^(`{3,})(.*)$/);
      if (fence) {
        code += "\n" + line;
        if (marker && marker[1].length >= fence && !marker[2].trim()) {
          output.push({ key: code, text: prefix + code }); fence = 0; code = ""; blank = "\n";
        }
      } else if (marker) {
        fence = marker[1].length; code = line; prefix = blank; blank = "";
      } else if (!line.trim()) {
        blank += "\n";
      } else {
        output.push({ key: line, text: blank + line }); blank = "\n";
      }
    }
    if (fence) output.push({ key: code, text: prefix + code });
    return output;
  }

  function overlap(previous, next) {
    if (!previous.length || !next.length) return 0;
    const failure = new Array(next.length).fill(0);
    for (let i = 1, j = 0; i < next.length; i += 1) {
      while (j && next[i].key !== next[j].key) j = failure[j - 1];
      if (next[i].key === next[j].key) j += 1;
      failure[i] = j;
    }
    let matched = 0;
    for (const item of previous) {
      while (matched && (matched === next.length || item.key !== next[matched].key)) matched = failure[matched - 1];
      if (item.key === next[matched].key) matched += 1;
    }
    return matched;
  }

  function create(maxChars) {
    const limit = Math.min(500000, Math.max(1, Number(maxChars) || 500000));
    let previous = [];
    let text = "";
    let count = 0;
    let uncertain = 0;
    let truncated = false;
    return {
      add(snapshot) {
        const next = units(String(snapshot || "").slice(0, limit));
        let prefix = 0;
        while (prefix < previous.length && prefix < next.length && previous[prefix].key === next[prefix].key) prefix += 1;
        const oldTail = previous.slice(prefix);
        const newTail = next.slice(prefix);
        const shared = overlap(oldTail, newTail);
        if (count && oldTail.length && newTail.length && !shared) uncertain += 1;
        const extra = newTail.slice(shared).map(function (item) { return item.text; }).join("");
        const addition = text && extra && !extra.startsWith("\n") ? "\n\n" + extra : extra;
        if (text.length + addition.length > limit || String(snapshot || "").length > limit) truncated = true;
        text += addition.slice(0, Math.max(0, limit - text.length));
        previous = next;
        count += 1;
        return { text: text.trim(), snapshots: count, uncertain_overlaps: uncertain, truncated };
      },
    };
  }
  return { create };
});

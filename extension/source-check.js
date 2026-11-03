"use strict";

// Historical source checks are read-only. Unknown/malformed results never imply
// that evidence is current; a workspace/view change discards a late response.
const webRagSourceCheck = (() => {
  const labels = Object.freeze({
    current: "与回答时版本一致",
    changed: "资料已更新，请重新提问",
    missing: "资料已删除或不可访问",
    unavailable: "资料暂不可用，请稍后核对",
    unversioned: "旧回答未记录版本，无法确认是否更新",
    unknown: "暂时无法核对此条来源",
  });
  const serverStates = new Set(["current", "changed", "missing", "unavailable", "unversioned"]);
  async function query(sources, send, isCurrent) {
    if (!Array.isArray(sources) || sources.length > 100) throw new Error("Source check limit exceeded");
    const states = sources.map(() => "unknown"), refs = [], indices = [];
    sources.forEach((source, index) => {
      if (!source || !Number.isSafeInteger(source.document_id) || source.document_id < 1) return;
      const revision = source.content_revision == null ? null : source.content_revision;
      if (revision !== null && (!Number.isSafeInteger(revision) || revision < 0)) return;
      refs.push({ document_id: source.document_id, content_revision: revision });
      indices.push(index);
    });
    if (!isCurrent()) return null;
    if (!refs.length) return states;
    const data = await send(refs);
    if (!isCurrent()) return null;
    if (!data || !Array.isArray(data.sources) || data.sources.length !== refs.length) throw new Error("Invalid source check response");
    data.sources.forEach((row, index) => {
      const ref = refs[index];
      if (!row || row.document_id !== ref.document_id || row.content_revision !== ref.content_revision ||
          !serverStates.has(row.state) || (ref.content_revision === null && ["current", "changed"].includes(row.state))) {
        throw new Error("Mismatched source check response");
      }
      states[indices[index]] = row.state;
    });
    return states;
  }
  function summary(states) {
    if (states.some(state => ["changed", "missing", "unavailable"].includes(state))) return "来源已变化，建议重新提问";
    if (states.length && states.every(state => state === "current")) return "来源与回答时版本一致";
    if (states.some(state => state === "unknown")) return "部分来源未能核对，请重试";
    return "旧回答未完整记录来源版本";
  }
  function overall(states) {
    for (const state of ["changed", "missing", "unavailable"]) if (states.includes(state)) return state;
    if (states.length && states.every(state => state === "current")) return "current";
    return !states.length || states.includes("unknown") ? "unknown" : "unversioned";
  }
  return { query, labels, summary, overall };
})();

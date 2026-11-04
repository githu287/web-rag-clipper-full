"use strict";

// 仅构造本地文件：白名单复制消息，不读取身份、配置、草稿或网页绑定。
const webRagChatExport = (() => {
  const text = value => typeof value === "string" ? value : "";
  const isMessage = value => value && (value.role === "user" || value.role === "assistant") &&
    typeof value.content === "string" && !!value.content.trim();
  const positiveId = value => Number.isSafeInteger(value) && value > 0 ? value : null;
  const revision = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const validDate = value => typeof value === "number" && Number.isFinite(value) &&
    !Number.isNaN(new Date(value).getTime());
  function date(value) {
    if (!validDate(value)) return "时间未记录";
    const d = new Date(value), pad = part => String(part).padStart(2, "0");
    const offset = -d.getTimezoneOffset(), absolute = Math.abs(offset);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + " " +
      (offset >= 0 ? "+" : "-") + pad(Math.floor(absolute / 60)) + ":" + pad(absolute % 60);
  }
  function literal(value) {
    return text(value).replace(/[\r\n\t]+/g, " ").replace(/&/g, "&amp;")
      .replace(/[\\`*_{}\[\]()#+.!|<>~-]/g, "\\$&");
  }
  function safeUrl(value) {
    if (typeof value !== "string" || /[\u0000-\u001f\u007f]/.test(value)) return null;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return url.href.replace(/[<>\s]/g, char => encodeURIComponent(char));
    } catch (_err) { return null; }
  }
  function snapshot(target, options = {}) {
    return {
      title: text(target && target.title) || "新会话",
      workspace: text(options.workspace) || "当前空间",
      exportedAt: validDate(options.exportedAt) ? options.exportedAt : Date.now(),
      inProgress: options.inProgress === true,
      unsaved: false,
      messages: (Array.isArray(target && target.messages) ? target.messages : []).filter(isMessage).map(message => ({
        id: text(message.id), role: message.role, content: message.content,
        createdAt: validDate(message.createdAt) ? message.createdAt : null,
        question: text(message.question), replyTo: text(message.replyTo),
        mode: message.mode === "current" || message.mode === "all" ? message.mode : null,
        documentId: positiveId(message.documentId), pageTitle: text(message.pageTitle), pageUrl: safeUrl(message.pageUrl),
        sources: (Array.isArray(message.sources) ? message.sources : [])
          .filter(source => source && typeof source === "object").map(source => ({
            title: text(source.title), url: safeUrl(source.url), documentId: positiveId(source.document_id),
            revision: revision(source.content_revision), score: typeof source.score === "number" && Number.isFinite(source.score) ? source.score : null,
          })),
      })),
    };
  }
  function hasMessages(target) {
    return Array.isArray(target && target.messages) && target.messages.some(isMessage);
  }
  function containsUnsaved(copy, stored) {
    const records = Array.isArray(stored && stored.messages) ? stored.messages : [];
    return copy.messages.some(message => !records.some(record => isMessage(record) &&
      (message.id && text(record.id) ? message.id === record.id :
        message.role === record.role && message.content === record.content && message.createdAt === record.createdAt)));
  }
  function scope(message) {
    if (message.mode === "all") return "全部知识库";
    if (message.mode !== "current") return "范围未记录";
    const label = literal(message.pageTitle) || "标题未记录";
    return "单篇文档 · " + label + (message.documentId ? "（文档 " + message.documentId + "）" : "（编号未记录）");
  }
  function quote(value) {
    // 每条正文独立引用，正文中的标题及未闭合代码块不会吞掉后续消息。
    return text(value).replace(/\r\n?/g, "\n").split("\n").map(line => "> " + line).join("\n");
  }
  function filename(copy) {
    const title = text(copy.title).replace(/[<>:"/\\|?*\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "_")
      .replace(/\s+/g, " ").replace(/^[. ]+|[. ]+$/g, "");
    // 固定前缀避免 Windows 保留文件名；按码点截断，保留中文及完整 emoji。
    return "会话-" + (Array.from(title).slice(0, 50).join("") || "新会话") + "-" +
      date(copy.exportedAt).slice(0, 19).replace(/[-: ]/g, "") + ".md";
  }
  function render(copy) {
    const lines = ["# " + literal(copy.title), "", "- 所属空间：" + literal(copy.workspace),
      "- 导出时间：" + date(copy.exportedAt), "- 消息数量：" + copy.messages.length,
      "- 记录范围：当前面板保留的消息，不含未发送草稿。", ""];
    if (copy.inProgress) lines.push("**导出时仍有回答生成中，此文件不包含之后返回的内容。**", "");
    if (copy.unsaved) lines.push("**包含面板中尚未保存的消息，请保留此文件。**", "");
    copy.messages.forEach((message, index) => {
      lines.push("---", "", "## " + (index + 1) + ". " + (message.role === "user" ? "你" : "Web RAG"), "",
        "- 时间：" + date(message.createdAt), "- 提问范围：" + scope(message));
      if (message.mode === "current" && message.pageUrl) lines.push("- 原网页：[打开原网页](<" + message.pageUrl + ">)");
      // 用户消息被配额裁剪时，仍可保留回答里明确记录的问题快照。
      const parent = message.replyTo ? copy.messages.find(item => item.id === message.replyTo) : copy.messages[index - 1];
      if (message.role === "assistant" && message.question.trim() &&
        !(parent && parent.role === "user" && parent.content.trim() === message.question.trim())) {
        lines.push("", "### 原问题（历史快照）", "", quote(message.question));
      }
      lines.push("", "### 正文", "", quote(message.content), "");
      if (message.role === "assistant" && message.sources.length) {
        lines.push("### 来源（保存记录）", "");
        message.sources.forEach((source, sourceIndex) => {
          const title = literal(source.title) || "来源 " + (sourceIndex + 1);
          const label = source.url ? "[" + title + "](<" + source.url + ">)" : title + "（链接未记录或不可导出）";
          const details = [];
          if (source.documentId) details.push("文档 " + source.documentId);
          details.push(source.revision === null ? "版本未记录" : "记录版本 " + source.revision);
          if (source.score !== null) details.push("检索分数 " + source.score.toFixed(3));
          lines.push((sourceIndex + 1) + ". " + label + " · " + details.join(" · "));
        });
        lines.push("");
      }
    });
    lines.push("---", "", "来源为历史保存信息，导出未重新联网核对当前状态。", "");
    return { content: lines.join("\n"), filename: filename(copy), count: copy.messages.length };
  }
  return { snapshot, render, hasMessages, containsUnsaved };
})();

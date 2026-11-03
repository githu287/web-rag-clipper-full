"use strict";

// Recover the question and its original scope, never the currently active tab.
const webRagQuestionReplay = (() => {
  function context(value) {
    if (!value || !["current", "all"].includes(value.mode)) return null;
    if (value.mode === "current" && (!Number.isSafeInteger(value.documentId) || value.documentId < 1)) return null;
    return {
      mode: value.mode,
      documentId: value.mode === "current" ? value.documentId : null,
      pageUrl: typeof value.pageUrl === "string" ? value.pageUrl : null,
      pageTitle: typeof value.pageTitle === "string" ? value.pageTitle : null,
    };
  }
  function resolve(messages, answer) {
    if (!answer || answer.role !== "assistant") return null;
    const scope = context(answer);
    if (!scope) return null;
    if (Object.hasOwn(answer, "question")) {
      return typeof answer.question === "string" && answer.question.trim()
        ? { query: answer.question.trim(), context: scope } : null;
    }
    if (!Array.isArray(messages)) return null;
    if (Object.hasOwn(answer, "replyTo") && (typeof answer.replyTo !== "string" || !answer.replyTo)) return null;
    const index = messages.findIndex(message => message && (message === answer || (answer.id && message.id === answer.id)));
    const parent = typeof answer.replyTo === "string"
      ? messages.find(message => message && message.id === answer.replyTo)
      : index > 0 ? messages[index - 1] : null;
    const parentScope = context(parent);
    if (!parent || parent.role !== "user" || !parentScope || parentScope.mode !== scope.mode ||
        parentScope.documentId !== scope.documentId || typeof parent.content !== "string" || !parent.content.trim()) return null;
    return { query: parent.content.trim(), context: scope };
  }
  return { context, resolve };
})();

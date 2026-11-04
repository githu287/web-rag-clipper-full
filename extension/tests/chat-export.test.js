"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const context = vm.createContext({ URL });
vm.runInContext(fs.readFileSync(path.join(__dirname, "../chat-export.js"), "utf8"), context);
const exporter = vm.runInContext("webRagChatExport", context);
const target = {
  title: "中文笔记 / English 📝 <img src=x>", pluginSecret: "DO-NOT-EXPORT-SECRET", draft: "UNSENT-DRAFT",
  messages: [
    { id: "u", role: "user", content: "分三点解释？\nKeep English terms", createdAt: 1793750400000,
      mode: "current", documentId: 77, pageTitle: "资料 A", pageUrl: "https://example.com/a?chapter=1" },
    { id: "a", role: "assistant", question: "分三点解释？\nKeep English terms", replyTo: "u", content: "## 要点\n\n1. 中文\n2. English\n\n```python\nprint('你好')\n```",
      createdAt: 1793750401000, mode: "current", documentId: 77, pageTitle: "资料 A", sources: [
        { title: "资料 [A] <b>", url: "https://example.com/a?q=(test)", document_id: 77, content_revision: 0, score: .8755, api_key: "DO-NOT-EXPORT-KEY" },
        { title: "禁止的链接", url: "javascript:alert(1)", content_revision: -1 },
        { title: "带凭证的链接", url: "https://user:password@example.com/private" },
      ], plugin_secret: "DO-NOT-EXPORT-MESSAGE-SECRET" },
    { role: "system", content: "DO-NOT-EXPORT-SYSTEM" },
    null, { role: "user", content: " " },
  ],
};
const options = { workspace: "测试空间\n# spoof", exportedAt: 1793750405000 };
const copy = exporter.snapshot(target, options);
const before = JSON.stringify(target);
const result = exporter.render(copy);
assert.equal(JSON.stringify(target), before, "export must not mutate the conversation");
assert.equal(result.count, 2);
assert(result.content.includes("分三点解释？"));
assert(result.content.includes("> ```python\n> print('你好')\n> ```"));
assert(result.content.includes("单篇文档 · 资料 A（文档 77）"));
assert(result.content.includes("记录版本 0"));
assert(result.content.includes("版本未记录"));
assert(result.content.includes("https://example.com/a?q=(test)"));
assert(!result.content.includes("javascript:"));
assert(!result.content.includes("user:password"));
assert(!result.content.includes("历史快照"), "a present parent question must not be duplicated");
assert(result.content.includes("\\<img"), "HTML-like metadata must stay literal Markdown text");
assert(!result.content.includes("\n# spoof"));
for (const forbidden of ["DO-NOT-EXPORT", "UNSENT-DRAFT"]) {
  assert(!JSON.stringify(copy).includes(forbidden));
  assert(!result.content.includes(forbidden));
}
assert(result.filename.endsWith(".md"));
assert(!/[<>:"/\\|?*\u0000-\u001f]/.test(result.filename));
assert(result.filename.includes("📝"));
assert.equal(exporter.containsUnsaved(copy, target), false);
target.messages[0].content = "Changed after click";
target.messages[1].sources[0].title = "Changed source";
assert.equal(exporter.render(copy).content, result.content, "click-time text and sources are immutable");
assert.equal(exporter.containsUnsaved(copy, { messages: [] }), true);
const orphan = exporter.snapshot({ title: "CON", messages: [{ role: "assistant", content: "未闭合代码：\n````js\nconst x = 1;",
  question: "已裁剪的原问题", mode: "all", createdAt: -Infinity }, { role: "assistant", content: "下一条答案", mode: "unsupported" }] }, options);
orphan.inProgress = true; orphan.unsaved = true;
const output = exporter.render(orphan);
assert(output.content.includes("原问题（历史快照）"));
assert(output.content.includes("生成中"));
assert(output.content.includes("尚未保存"));
assert(output.content.includes("范围未记录"));
assert(output.content.includes("时间未记录"));
assert(output.content.includes("## 2. Web RAG"));
assert(output.filename.startsWith("会话-CON-"));
const unusual = exporter.snapshot({ title: "../...\u202e" + "长".repeat(300), messages: [] }, options);
assert(exporter.render(unusual).filename.length < 100);
assert.equal(exporter.hasMessages({ messages: [{ role: "assistant", content: "回答" }] }), true);
assert.equal(exporter.hasMessages({ messages: [null, { role: "system", content: "private" }] }), false);
console.log("Chat export: immutable whitelist snapshots, Chinese/English Markdown, sources, safe metadata links/filenames, missing scopes, cropped parents and pending/unsaved notices passed");

"use strict";

// Execute the actual sidepanel chat handlers with the real session store, using
// deferred storage/model responses to reproduce tab/session switches at awaits.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const extensionDir = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(extensionDir, "sidepanel.js"), "utf8").replace(/\r\n/g, "\n");
const handlers = source.slice(source.indexOf("function questionDraftController("), source.indexOf("// ================================================================ 剪藏\n"));
const render = source.slice(source.indexOf("function renderChat()"), source.indexOf("function emptyText()"));
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));

async function fixture() {
  const values = {};
  const reply = deferred();
  const calls = [];
  const drawn = [];
  let pluginId = "plugin-a";
  let pluginSecret = "secret-a";
  let writeGate = null;
  class ApiRequestError extends Error {}
  const context = vm.createContext({
    console,
    crypto: require("node:crypto").webcrypto,
    TextEncoder,
    chrome: { storage: { local: {
      async get(key) { return { [key]: clone(values[key]) }; },
      async set(items) {
        if (writeGate) { const gate = writeGate; writeGate = null; await gate.promise; }
        Object.assign(values, clone(items));
      },
      async remove(key) { delete values[key]; },
    } } },
    webRagApiClient: {
      getPlugin: () => ({ pluginId, pluginSecret }), ApiRequestError,
      rag: { ask: payload => { calls.push(payload); return reply.promise; } },
    },
    session: null,
    binding: { mode: "current", documentId: 101, pageUrl: "https://example.com/a", pageTitle: "Page A" },
    isSending: false, activeChatRequest: null, questionReplayDraft: null, questionComposerOwner: null,
    questionDraftPersistence: null, contextLoadToken: 0, chatSessionListToken: 0, deletingChatSessionId: null,
    chatExportBusy: false, chatExportToken: 0,
    els: {
      chatTextarea: { value: "Question A", focus() {} },
      chatReplayNotice: { hidden: true, textContent: "" },
      chatDraftStatus: { textContent: "" }, chatDraftList: { hidden: true },
      chatSessions: { hidden: true }, chatSessionsToggle: { setAttribute() {} },
      chatSessionList: { replaceChildren() {} }, chatSessionsStatus: { textContent: "" },
      chatExportStatus: { hidden: true, textContent: "" },
      chatEmpty: { style: {} },
      chatArea: { replaceChildren() { drawn.length = 0; } },
    },
    updateSendState() {}, renderWelcomeView() {}, showInlineHint() {},
    switchView() {}, renderClipView() {}, scrollToBottom() {},
    appendModeSeparatorToDom() {}, emptyText: () => "empty",
    appendMessageToDom: message => drawn.push(clone(message)),
    appendLoading: () => drawn.push({ loading: true }),
    errorText: error => error.message,
  });
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "config.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "session-store.js"), "utf8") + "\nglobalThis.store = sessionStore;", context);
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "question-replay.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(extensionDir, "question-drafts.js"), "utf8"), context);
  vm.runInContext(render + "\n" + handlers, context);
  const original = await context.store.createSession(pluginId, { title: "新会话" });
  await context.store.setCurrentSessionId(pluginId, original.sessionId);
  context.session = original;
  return {
    context, original, reply, calls, drawn,
    blockWrite() { writeGate = deferred(); return writeGate; },
    resetIdentity(id = null) { pluginId = id; context.session = null; context.activeChatRequest = null; context.isSending = false; },
    switchTab() { context.binding = { mode: "all", documentId: 202, pageUrl: "https://example.com/b", pageTitle: "Page B" }; },
    rotateSecret() { pluginSecret = "rotated-secret"; },
  };
}

async function assertOriginalAnswer(f, content) {
  const saved = await f.context.store.getSession(f.original.sessionId);
  assert.equal(saved.messages.length, 2);
  for (const message of saved.messages) {
    assert.equal(message.documentId, 101);
    assert.equal(message.pageUrl, "https://example.com/a");
    assert.equal(message.pageTitle, "Page A");
    assert.equal(message.mode, "current");
  }
  assert.equal(saved.messages[1].content, content);
}

(async function run() {
  // Switch while initial user-message persistence is blocked, before ask starts.
  {
    const f = await fixture();
    const gate = f.blockWrite();
    const sending = f.context.sendQuestion();
    await settle();
    const switching = f.context.newSession();
    f.switchTab();
    gate.resolve();
    await switching;
    await settle();
    assert.equal(f.calls[0].document_id, 101);
    f.reply.resolve({ answer: "Answer A", sources: [] });
    await sending;
    await assertOriginalAnswer(f, "Answer A");
    assert.equal(f.context.session.messages.length, 0);
    assert.equal(f.drawn.length, 0, "old reply must not render in the new session");
    assert.equal(f.context.isSending, false);
  }

  // Both successful replies and errors after a session switch stay in A.
  for (const fail of [false, true]) {
    const f = await fixture();
    const sending = f.context.sendQuestion();
    await settle();
    await f.context.newSession();
    f.switchTab();
    if (fail) f.reply.reject(new Error("Model unavailable"));
    else f.reply.resolve({ answer: "Answer A", sources: [{ document_id: 101 }] });
    await sending;
    await assertOriginalAnswer(f, fail ? "Model unavailable" : "Answer A");
    assert.equal(f.context.session.messages.length, 0);
    assert.equal(f.drawn.length, 0);
  }

  // Tab loading may replace the same session with a separately deserialized object.
  {
    const f = await fixture();
    const sending = f.context.sendQuestion();
    await settle();
    assert.equal(f.drawn.filter(m => m.loading).length, 1);
    f.context.session = await f.context.store.getSession(f.original.sessionId);
    f.switchTab();
    f.context.renderChat();
    assert.equal(f.drawn.filter(m => m.loading).length, 1);
    f.reply.resolve({ answer: "Answer A" });
    await sending;
    await assertOriginalAnswer(f, "Answer A");
    assert.equal(f.context.session.messages.length, 2);
    assert.equal(f.drawn.filter(m => m.loading).length, 0);
    assert.equal(f.drawn[1].content, "Answer A");
  }

  // Clearing identity suppresses late saves/rendering and leaves newer requests alone.
  for (const beforeAsk of [false, true]) {
    const f = await fixture();
    const gate = beforeAsk ? f.blockWrite() : null;
    const sending = f.context.sendQuestion();
    await settle();
    f.resetIdentity("plugin-b");
    const newerRequest = { target: { sessionId: "new-session" } };
    f.context.activeChatRequest = newerRequest;
    f.context.isSending = true;
    if (gate) gate.resolve();
    f.reply.resolve({ answer: "must be discarded" });
    await sending;
    const saved = await f.context.store.getSession(f.original.sessionId);
    assert.equal(saved.messages.length, 1);
    assert.equal(f.context.session, null);
    assert.equal(f.context.activeChatRequest, newerRequest);
    assert.equal(f.context.isSending, true);
    assert.equal(f.calls.length, beforeAsk ? 0 : 1);
  }

  // A normal same-session reply still persists title, sources and unlocks sending.
  {
    const f = await fixture();
    const sending = f.context.sendQuestion();
    await settle();
    f.reply.resolve({ answer: "Answer A", sources: [{ document_id: 101 }] });
    await sending;
    await assertOriginalAnswer(f, "Answer A");
    const saved = await f.context.store.getSession(f.original.sessionId);
    assert.equal(saved.title, "Question A");
    assert.equal(saved.messages[1].sources[0].document_id, 101);
    assert.equal(f.context.activeChatRequest, null);
    assert.equal(f.context.isSending, false);
  }
  // All-library mode never inherits a document filter; double-click sends once.
  {
    const f = await fixture();
    f.context.binding.mode = "all";
    const sending = f.context.sendQuestion();
    f.context.els.chatTextarea.value = "Second click";
    await f.context.sendQuestion();
    await settle();
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].document_id, null);
    f.reply.resolve({ answer: "All-library answer" });
    await sending;
    const saved = await f.context.store.getSession(f.original.sessionId);
    assert.equal(saved.messages.length, 2);
    assert.equal(saved.messages[1].mode, "all");
    assert.equal(saved.messages[1].documentId, null);
  }
  // A replay captures its old document scope even after the active tab changes.
  {
    const f = await fixture();
    const original = { query: "Old question", context: { mode: "current", documentId: 77, pageTitle: "Old document", pageUrl: "https://example.com/old" } };
    f.context.questionReplayDraft = { ...original, pluginId: "plugin-a", pluginSecret: "secret-a", sessionId: f.original.sessionId };
    f.context.els.chatTextarea.value = "Edited old question";
    f.switchTab();
    const sending = f.context.sendQuestion();
    await settle();
    assert.equal(f.calls[0].document_id, 77);
    assert.equal(f.calls[0].query, "Edited old question");
    f.reply.resolve({ answer: "New answer", sources: [] });
    await sending;
    const saved = await f.context.store.getSession(f.original.sessionId);
    assert.equal(saved.messages[1].question, "Edited old question");
    assert.equal(saved.messages[1].replyTo, saved.messages[0].id);
    assert.equal(saved.messages[1].documentId, 77);
    assert.equal(saved.messages[1].pageTitle, "Old document");
    assert.equal(f.context.questionReplayDraft, null);
  }
  // A global replay cannot inherit the new tab's document, including stale bindings.
  {
    const f = await fixture();
    f.context.questionReplayDraft = { context: { mode: "all", documentId: null }, pluginId: "plugin-a", pluginSecret: "secret-a", sessionId: f.original.sessionId };
    f.context.binding.stale = true;
    const sending = f.context.sendQuestion();
    await settle();
    assert.equal(f.calls[0].document_id, null);
    f.reply.resolve({ answer: "All-library replay" });
    await sending;
  }
  // Expired replay drafts never fall through into the new session/workspace scope.
  for (const change of ["session", "workspace", "secret"]) {
    const f = await fixture();
    f.context.questionReplayDraft = { context: { mode: "current", documentId: 77 }, pluginId: "plugin-a", pluginSecret: "secret-a", sessionId: f.original.sessionId };
    if (change === "session") f.context.session = { sessionId: "other", messages: [] };
    else if (change === "workspace") f.resetIdentity("plugin-b");
    else f.rotateSecret();
    await f.context.sendQuestion();
    assert.equal(f.calls.length, 0);
  }
  // A secret rotation also invalidates a late model reply and removes its spinner.
  {
    const f = await fixture();
    const sending = f.context.sendQuestion();
    await settle();
    f.rotateSecret();
    f.reply.resolve({ answer: "Discard old-credential answer" });
    await sending;
    const saved = await f.context.store.getSession(f.original.sessionId);
    assert.equal(saved.messages.length, 1);
    assert.equal(f.drawn.some(message => message.loading), false);
    assert.equal(f.context.isSending, false);
  }
  // Even after cancelling a replay scope, its input cannot cross an identity boundary.
  {
    const f = await fixture();
    f.context.syncQuestionComposerOwner();
    f.context.els.chatTextarea.value = "Private unsent question";
    f.rotateSecret();
    await f.context.sendQuestion();
    assert.equal(f.calls.length, 0);
    assert.equal(f.context.els.chatTextarea.value, "");
  }
  // Attaching the initial session does not erase typing in the same workspace.
  {
    const f = await fixture();
    f.context.session = null;
    f.context.syncQuestionComposerOwner();
    f.context.els.chatTextarea.value = "Initial draft";
    f.context.session = f.original;
    assert.equal(f.context.syncQuestionComposerOwner(), true);
    assert.equal(f.context.els.chatTextarea.value, "Initial draft");
    await f.context.questionDraftController().flush();
    assert.equal((await f.context.questionDraftController().list())[0].query, "Initial draft");
  }
  // Identity may change before async session loading finishes; no send may target the old owner.
  {
    const f = await fixture();
    f.resetIdentity("plugin-b");
    f.context.session = f.original;
    f.context.syncQuestionComposerOwner();
    f.context.els.chatTextarea.value = "New workspace question";
    await f.context.sendQuestion();
    assert.equal(f.calls.length, 0);
  }
  // A deleted conversation must not reappear after either a success or an error.
  for (const fail of [false, true]) {
    const f = await fixture();
    const sending = f.context.sendQuestion();
    await settle();
    await f.context.store.deleteSession(f.original.sessionId, "plugin-a");
    if (fail) f.reply.reject(new Error("Late failure"));
    else f.reply.resolve({ answer: "Late deleted answer" });
    await sending;
    assert.equal(await f.context.store.getSession(f.original.sessionId), null);
    assert.notEqual(f.context.session.sessionId, f.original.sessionId);
    assert.equal(f.context.session.messages.length, 0);
    assert.equal(f.context.isSending, false);
    assert.equal(f.calls.length, 1);
  }
  // Persistence failure before the model call must keep the input and stop billing.
  {
    const f = await fixture();
    f.context.store.saveSession = async () => false;
    await f.context.sendQuestion();
    assert.equal(f.calls.length, 0);
    assert.equal(f.context.els.chatTextarea.value, "Question A");
    assert.equal(f.context.session.messages.length, 0);
    assert.match(f.context.els.chatReplayNotice.textContent, /尚未发送/);
  }
  // Conversation navigation cannot bypass draft save failures or ownership.
  {
    const f = await fixture();
    const other = await f.context.store.createSession("plugin-a");
    const foreign = await f.context.store.createSession("plugin-b");
    f.context.syncQuestionComposerOwner();
    await f.context.questionDraftController().flush();
    await f.context.openChatSession(foreign.sessionId);
    assert.equal(f.context.session.sessionId, f.original.sessionId);
    f.context.store.saveQuestionDraft = async () => { throw Error("quota"); };
    await f.context.openChatSession(other.sessionId);
    assert.equal(f.context.session.sessionId, f.original.sessionId);
    assert.equal(f.context.els.chatTextarea.value, "Question A");
    assert.equal(f.context.els.chatTextarea.readOnly, false);
    assert.equal(f.calls.length, 0);
  }
  // Storage reads can also fail while handling a failed save; keep input and settle.
  {
    const f = await fixture();
    f.context.store.saveSession = async () => false;
    f.context.store.getSession = async () => { throw Error("storage unavailable"); };
    await f.context.sendQuestion();
    assert.equal(f.calls.length, 0);
    assert.equal(f.context.els.chatTextarea.value, "Question A");
    assert.equal(f.context.isSending, false);
    assert.equal(f.context.els.chatReplayNotice.hidden, false);
  }
  console.log("chat request races, original-scope replays, durable question links and credential rotation passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

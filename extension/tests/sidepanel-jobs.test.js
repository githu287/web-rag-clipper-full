"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const extensionDir = path.resolve(__dirname, "..");

function loadPanel() {
  const nodes = {};
  const state = {
    pluginId: "plugin-a", writes: 0, notifications: [], delays: [],
    records: [], libraryLoads: 0, stored: {
      pluginId: "plugin-a", ingestJobId: "job-1", ingestJobPageUrl: "https://example.com/#/one",
      pageUrl: "https://example.com/#/one", stale: false,
    },
  };
  class ApiRequestError extends Error {
    constructor(status, code) { super(code); this.status = status; this.code = code; }
  }
  const api = {
    ApiRequestError,
    getPlugin: () => ({ pluginId: state.pluginId }),
    jobs: { get: async () => ({ id: "job-1", status: "SUCCEEDED", document_id: 7 }) },
    documents: { get: async () => ({ id: 7, url: "https://example.com/#/one", title: "One" }) },
  };
  const store = {
    async updateTabBinding(_tabId, update) {
      const before = JSON.stringify(state.stored);
      state.stored = update(structuredClone(state.stored));
      if (JSON.stringify(state.stored) !== before) state.writes += 1;
      return state.stored;
    },
    async getUploadJobs() { return state.records; },
    async setUploadJob(pluginId, record) {
      assert.equal(pluginId, "plugin-a");
      state.records = state.records.filter(item => item.documentId !== record.documentId).concat(record);
    },
    async clearUploadJob(pluginId, documentId) {
      assert.equal(pluginId, "plugin-a");
      state.records = state.records.filter(item => item.documentId !== documentId);
    },
  };
  const context = vm.createContext({
    console, URL, setTimeout, clearTimeout,
    document: { getElementById(id) {
      return nodes[id] ||= { textContent: "", disabled: false, hidden: false,
        classList: { add() {}, remove() {} } };
    } },
    chrome: {
      tabs: { get: async () => ({ url: state.stored.pageUrl }) },
      runtime: { sendMessage(message) { state.notifications.push(message); } },
    },
    webRagApiClient: api, sessionStore: store, state,
  });
  for (const file of ["config.js", "url-utils.js"]) {
    vm.runInContext(fs.readFileSync(path.join(extensionDir, file), "utf8"), context);
  }
  const source = fs.readFileSync(path.join(extensionDir, "sidepanel.js"), "utf8");
  // Run the actual module with initialization disabled; stub unrelated rendering only.
  assert.match(source, /\ninit\(\);\s*$/);
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, "\n"), context);
  vm.runInContext(`
    currentTabId = 1; clipJobPollToken = 1; uploadJobPollToken = 1;
    renderClipView = function () {};
    updateSendState = function () {};
    loadLibrary = async function () { state.libraryLoads += 1; };
    syncClipDraftFromInputs = function () {};
    clearClipDraft = function () { clipDraft = null; };
    waitMilliseconds = async function (ms) { state.delays.push(ms); };
    globalThis.hooks = {
      completeClipJob, monitorClipJob, monitorUploadJob, resumeUploadJobMonitor, submitClipDraft,
      prepareDraft() {
        binding = state.stored;
        clipDraft = { url: state.stored.pageUrl, title: "One", raw_text: "body" };
        clipDraftTabId = currentTabId;
      },
      cancelTab() { currentTabId = 2; },
      snapshot() { return { activeUploadJob, uploadBusy, clipErrorMsg, binding }; },
    };
  `, context);
  return { hooks: context.hooks, api, state, nodes, context };
}

(async function run() {
  {
    const { hooks, api, state } = loadPanel();
    let calls = 0;
    api.jobs.get = async () => {
      if (++calls <= 2) throw new api.ApiRequestError(0, "NETWORK");
      return { id: "job-1", status: "SUCCEEDED", document_id: 7 };
    };
    await hooks.monitorClipJob("job-1", 1, 1);
    assert.equal(calls, 3);
    assert.deepEqual(state.delays, [1500, 3000]);
    assert.equal(state.stored.documentId, 7);
    assert.equal(state.stored.ingestJobId, null);
  }
  {
    const { hooks, api, state } = loadPanel();
    let calls = 0;
    api.jobs.get = async () => { calls++; throw new api.ApiRequestError(401, "UNAUTHENTICATED"); };
    await hooks.monitorClipJob("job-1", 1, 1);
    assert.equal(calls, 1);
    assert.equal(state.delays.length, 0);
    assert.equal(state.writes, 0);
  }
  {
    const { hooks, api, state } = loadPanel();
    let calls = 0;
    api.documents.get = async () => {
      if (++calls > 8) throw new Error("retry budget was not enforced");
      throw new api.ApiRequestError(503, "HTTP");
    };
    await hooks.monitorClipJob("job-1", 1, 1);
    assert.equal(calls, 6, "detail-read failures must also exhaust retries");
    assert.deepEqual(state.delays, [1500, 3000, 6000, 12000, 24000]);
    assert.equal(state.stored.ingestJobId, "job-1");
  }
  for (const replacement of ["new-job", "new-route", "new-workspace", "new-tab"]) {
    const { hooks, api, state } = loadPanel();
    const getDocument = api.documents.get;
    api.documents.get = async () => {
      if (replacement === "new-job") state.stored.ingestJobId = "job-2";
      if (replacement === "new-route") state.stored.pageUrl = "https://example.com/#/two";
      if (replacement === "new-workspace") state.pluginId = "plugin-b";
      if (replacement === "new-tab") hooks.cancelTab();
      return getDocument();
    };
    await hooks.completeClipJob({ id: "job-1", document_id: 7 }, 1, 1);
    assert.notEqual(state.stored.documentId, 7, replacement);
    assert.equal(state.notifications.length, 0, replacement);
    if (replacement === "new-job") assert.equal(state.stored.ingestJobId, "job-2");
    if (replacement === "new-route") assert.equal(state.stored.stale, true);
  }
  {
    const { hooks, api, state, nodes } = loadPanel();
    let calls = 0;
    state.records = [{ jobId: "upload-1", documentId: 9 }];
    api.jobs.get = async () => {
      if (++calls === 1) throw new api.ApiRequestError(0, "TIMEOUT");
      return { id: "upload-1", status: "SUCCEEDED", progress: 100 };
    };
    await hooks.monitorUploadJob({ ...state.records[0], filename: "a.md", pluginId: "plugin-a" }, 1);
    assert.equal(calls, 2);
    assert.equal(state.records.length, 0);
    assert.equal(hooks.snapshot().uploadBusy, false);
    assert.equal(nodes["library-upload-btn"].disabled, false);
  }
  {
    const { hooks, state, context } = loadPanel();
    state.records = [
      { jobId: "failed", documentId: 1, status: "FAILED" },
      { jobId: "pending", documentId: 2 },
    ];
    vm.runInContext("startUploadJobMonitor = function (record) { state.resumed = record.jobId; };", context);
    await hooks.resumeUploadJobMonitor();
    assert.equal(state.resumed, "pending", "latest failed task must not hide another pending upload");
  }
  for (const navigated of [false, true]) {
    const { hooks, api, state, context } = loadPanel();
    hooks.prepareDraft();
    api.clips = { clipAsync: async () => {
      if (navigated) {
        state.stored.pageUrl = "https://example.com/#/two";
        state.stored.stale = true;
        state.stored.ingestJobId = null;
      }
      return { id: "job-2", status: "QUEUED" };
    } };
    vm.runInContext("startClipJobMonitor = function (id) { state.monitored = id; };", context);
    await hooks.submitClipDraft();
    if (navigated) {
      assert.equal(state.stored.pageUrl, "https://example.com/#/two");
      assert.equal(state.stored.ingestJobId, null);
      assert.equal(state.monitored, undefined);
    } else {
      assert.equal(state.stored.ingestJobId, "job-2");
      assert.equal(state.monitored, "job-2");
    }
  }
  console.log("sidepanel job recovery and binding tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

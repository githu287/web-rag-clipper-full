"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "sidepanel.js"), "utf8").replace(/\r\n/g, "\n");
const start = source.indexOf("async function retryDocument(doc)");
const end = source.indexOf("// ================================================================ 文件上传", start);
assert.ok(start >= 0 && end > start);

class ApiRequestError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}

function fixture(status = "FAILED", sourceType = "webpage") {
  const calls = { lookup: [], retry: [], saved: [], monitors: [], statuses: [] };
  let pluginId = "plugin-a";
  const job = { id: "server-job", status, document_id: 101 };
  const context = vm.createContext({
    uploadBusy: false,
    els: { libraryUploadBtn: { disabled: false } },
    webRagApiClient: {
      ApiRequestError,
      getPlugin: () => ({ pluginId }),
      documents: { async job(id) { calls.lookup.push(id); return job; } },
      jobs: { async retry(id) { calls.retry.push(id); return { ...job, status: "QUEUED" }; } },
    },
    sessionStore: {
      getUploadJob() { throw new Error("retry must not depend on local upload mapping"); },
      async setUploadJob(owner, record) { calls.saved.push({ owner, record }); },
    },
    setUploadStatus(text, type) { calls.statuses.push({ text, type }); },
    startUploadJobMonitor(record, initial) { calls.monitors.push({ record, initial }); },
    async loadLibrary() {},
    errorText: error => error.message,
  });
  vm.runInContext(source.slice(start, end), context);
  const doc = { id: 101, source_type: sourceType, title: "Article", filename: "notes.txt" };
  return { context, calls, doc, switchPlugin: () => { pluginId = "plugin-b"; } };
}

function installMonitor(context) {
  context.uploadJobPollToken = 1;
  context.activeUploadJob = { id: "server-job" };
  context.libraryState = { page: 2 };
  const begin = source.indexOf("function stopUploadJobMonitor()");
  const finish = source.indexOf("// ================================================================ Plugin 注册", begin);
  assert.ok(begin >= 0 && finish > begin);
  const helpers = source.indexOf("function jobPollRetryDelay(");
  const helpersEnd = source.indexOf("function stopJobMonitors(", helpers);
  vm.runInContext(source.slice(helpers, helpersEnd), context);
  vm.runInContext(source.slice(begin, finish), context);
}

(async function run() {
  // Both source types recover the server job even without browser records.
  for (const type of ["webpage", "upload"]) {
    const { context, calls, doc } = fixture("FAILED", type);
    await context.retryDocument(doc);
    assert.deepEqual(calls.lookup, [101]);
    assert.deepEqual(calls.retry, ["server-job"]);
    assert.equal(calls.saved[0].record.documentId, 101);
    assert.equal(calls.saved[0].record.sourceType, type);
    assert.equal(calls.monitors[0].initial.status, "QUEUED");
    assert.equal(context.uploadBusy, true);
  }
  // Queued/running tasks resume polling without a duplicate retry mutation.
  for (const status of ["QUEUED", "RUNNING"]) {
    const { context, calls, doc } = fixture(status);
    await context.retryDocument(doc);
    assert.equal(calls.retry.length, 0);
    assert.equal(calls.monitors.length, 1);
    assert.equal(calls.monitors[0].initial.status, status);
  }
  for (const type of ["webpage", "upload"]) {
    for (const missing of [true, false]) {
      const { context, calls, doc } = fixture("SUCCEEDED", type);
      if (missing) context.webRagApiClient.documents.job = async () => { throw new ApiRequestError(404, "HTTP"); };
      await context.retryDocument(doc);
      assert.equal(calls.retry.length, 0);
      assert.equal(calls.monitors.length, 0);
      assert.equal(context.uploadBusy, false);
      assert.equal(context.els.libraryUploadBtn.disabled, false);
      const notice = calls.statuses.at(-1);
      assert.equal(notice.type, "warn");
      if (type === "webpage") {
        assert.match(notice.text, /打开原网页重新剪藏/);
        assert.doesNotMatch(notice.text, /上传/);
      } else assert.match(notice.text, /重新上传原文件/);
    }
  }
  // Retry limit / server errors unlock controls and do not start polling.
  {
    const { context, calls, doc } = fixture();
    context.webRagApiClient.jobs.retry = async () => { throw new ApiRequestError(409, "retry limit reached"); };
    await context.retryDocument(doc);
    assert.equal(context.uploadBusy, false);
    assert.equal(calls.monitors.length, 0);
    assert.match(calls.statuses.at(-1).text, /retry limit reached/);
  }
  // Identity changes while looking up a task cannot retry into the new workspace.
  {
    const { context, calls, doc, switchPlugin } = fixture();
    context.webRagApiClient.documents.job = async () => { switchPlugin(); return { id: "old", status: "FAILED" }; };
    await context.retryDocument(doc);
    assert.equal(calls.retry.length, 0);
    assert.equal(calls.saved.length, 0);
    assert.equal(calls.monitors.length, 0);
  }
  // Double clicking during lookup cannot send a second request.
  {
    const { context, calls, doc } = fixture();
    let finish;
    context.webRagApiClient.documents.job = () => new Promise(resolve => { finish = resolve; });
    const pending = context.retryDocument(doc);
    await context.retryDocument(doc);
    finish({ id: "server-job", status: "FAILED" });
    await pending;
    assert.equal(calls.retry.length, 1);
  }
  // Local persistence failure must not turn an accepted server retry into failure.
  {
    const { context, calls, doc } = fixture();
    context.sessionStore.setUploadJob = async () => { throw new Error("storage unavailable"); };
    await context.retryDocument(doc);
    assert.equal(calls.retry.length, 1);
    assert.equal(calls.monitors.length, 1);
  }
  // Polling an old workspace must not clear records or render into a new one.
  {
    const { context, calls, switchPlugin } = fixture();
    installMonitor(context);
    context.webRagApiClient.jobs.get = async () => { switchPlugin(); return { status: "SUCCEEDED" }; };
    context.sessionStore.clearUploadJob = () => { throw new Error("must not clear another workspace"); };
    await context.monitorUploadJob({ jobId: "old", documentId: 101, pluginId: "plugin-a" }, 1);
    assert.equal(calls.statuses.length, 0);
  }
  // A superseded poll cannot unlock controls belonging to the next task.
  {
    const { context, calls } = fixture();
    installMonitor(context);
    context.webRagApiClient.jobs.get = async () => {
      context.stopUploadJobMonitor();
      context.uploadBusy = true;
      context.els.libraryUploadBtn.disabled = true;
      return { status: "SUCCEEDED" };
    };
    await context.monitorUploadJob({ jobId: "old", pluginId: "plugin-a" }, 1);
    assert.equal(context.uploadBusy, true);
    assert.equal(context.els.libraryUploadBtn.disabled, true);
    assert.equal(calls.statuses.length, 0);
  }
  // Server success remains visible even if removing the local polling hint fails.
  {
    const { context, calls } = fixture();
    installMonitor(context);
    context.uploadBusy = true;
    context.webRagApiClient.jobs.get = async () => ({ status: "SUCCEEDED" });
    context.sessionStore.clearUploadJob = async () => { throw new Error("storage unavailable"); };
    await context.monitorUploadJob({ jobId: "job", pluginId: "plugin-a", documentId: 101, filename: "Article" }, 1);
    assert.equal(context.uploadBusy, false);
    assert.equal(context.els.libraryUploadBtn.disabled, false);
    assert.equal(calls.statuses.at(-1).text, "入库成功：Article（100%）");
  }
  console.log("library retry recovery tests passed (15 scenarios)");
})().catch(error => { console.error(error); process.exitCode = 1; });

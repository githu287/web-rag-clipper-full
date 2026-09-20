"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extensionDir = path.resolve(__dirname, "..");
const values = {};
const chrome = {
  storage: {
    local: {
      async get(key) {
        if (typeof key === "string") return { [key]: values[key] };
        return Object.assign({}, values);
      },
      async set(items) {
        Object.assign(values, items);
      },
      async remove(key) {
        delete values[key];
      },
    },
  },
};

const context = vm.createContext({ chrome, console });
vm.runInContext(
  fs.readFileSync(path.join(extensionDir, "config.js"), "utf8"),
  context,
);
vm.runInContext(
  fs.readFileSync(path.join(extensionDir, "session-store.js"), "utf8") +
    "\nglobalThis.__sessionStore = sessionStore;",
  context,
);

(async function run() {
  const store = context.__sessionStore;
  await store.setUploadJob("plugin-a", {
    jobId: "job-1",
    documentId: 11,
    filename: "one.txt",
  });
  await store.setUploadJob("plugin-a", {
    jobId: "job-2",
    documentId: 22,
    filename: "two.md",
  });

  assert.equal((await store.getUploadJobs("plugin-a")).length, 2);
  assert.equal((await store.getUploadJob("plugin-a", 11)).jobId, "job-1");
  assert.equal((await store.getUploadJob("plugin-a", 22)).jobId, "job-2");

  await store.clearUploadJob("plugin-a", 11);
  assert.equal(await store.getUploadJob("plugin-a", 11), null);
  assert.equal((await store.getUploadJobs("plugin-a")).length, 1);

  await store.clearUploadJob("plugin-a");
  assert.equal((await store.getUploadJobs("plugin-a")).length, 0);
  console.log("session-store upload job tests passed");
})().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});

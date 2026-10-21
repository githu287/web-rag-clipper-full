"use strict";

const assert = require("node:assert/strict");

require("../url-utils.js");

const normalize = globalThis.webRagUrlUtils.normalizeWebUrl;

assert.equal(
  normalize(" HTTPS://Example.COM.:443 "),
  "https://example.com/"
);
assert.equal(
  normalize("https://example.com/article?id=7&utm_source=x&FBCLID=y#part"),
  "https://example.com/article?id=7"
);
// 与 backend/tests/test_url_normalization.py 共享同一组向量：只保留路由型 fragment。
assert.equal(
  normalize("https://spa.example/#/users/1"),
  "https://spa.example/#/users/1"
);
assert.equal(
  normalize("https://spa.example/#/detail?id=7"),
  "https://spa.example/#/detail?id=7"
);
assert.notEqual(
  normalize("https://spa.example/#/users/1"),
  normalize("https://spa.example/#/users/2")
);
assert.equal(
  normalize("https://example.com/docs/page#section-2"),
  "https://example.com/docs/page"
);
assert.equal(
  normalize("https://example.com/docs/page#"),
  "https://example.com/docs/page"
);
assert.equal(
  normalize("https://spa.example#!/legacy/list"),
  "https://spa.example/#/legacy/list"
);
assert.equal(
  normalize("https://spa.example/#!/legacy/list"),
  normalize("https://spa.example/#/legacy/list")
);
assert.equal(
  normalize("https://spa.example/#/用户/1"),
  "https://spa.example/#/%E7%94%A8%E6%88%B7/1"
);
// 2048 字符上限只由权威的后端强制，这里不重复实现。
assert.equal(
  normalize("https://example.com/search?b=2&a=&b=3"),
  "https://example.com/search?b=2&a=&b=3"
);
assert.equal(
  normalize("https://example.com/?q=~ hello&x=%2F"),
  "https://example.com/?q=~+hello&x=%2F"
);
assert.equal(
  normalize("https://例子.测试/文章"),
  "https://xn--fsqu00a.xn--0zwm56d/%E6%96%87%E7%AB%A0"
);
assert.throws(() => normalize("file:///tmp/page.html"), /http or https/);
assert.throws(
  () => normalize("https://user:secret@example.com/private"),
  /credentials/
);

console.log("url-utils tests passed");

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

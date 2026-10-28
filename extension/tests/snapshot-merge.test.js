"use strict";
const assert = require("node:assert/strict");
const merge = require("../snapshot-merge.js");

{
  const capture = merge.create();
  const first = "# 标题\n\n第一段\n\n重复段落\n\n重复段落";
  assert.equal(capture.add(first).text, first);
  assert.equal(capture.add(first).text, first, "identical snapshots must not accumulate");
  const expanded = first + "\n\n下一段";
  assert.equal(capture.add(expanded).text, expanded, "appended DOM retains original order and legitimate repetition");
}
{
  const capture = merge.create();
  capture.add("# 列表\n\n- 行1\n- 行2\n- 行3");
  capture.add("# 列表\n\n- 行2\n- 行3\n- 行4");
  const result = capture.add("# 列表\n\n- 行3\n- 行4\n- 行5");
  assert.equal(result.text, "# 列表\n\n- 行1\n- 行2\n- 行3\n- 行4\n- 行5");
  assert.equal(result.uncertain_overlaps, 0);
}
{
  const capture = merge.create();
  const code = "````md\n```js\n  const x = 1\n\n```\n````";
  capture.add("# 示例\n\n" + code + "\n\n段落A");
  const result = capture.add("# 示例\n\n段落A\n\n段落B");
  assert(result.text.includes(code), "fenced examples remain atomic with their indentation and blank lines");
  assert.equal(result.text.split("段落A").length, 2);
}
{
  const capture = merge.create();
  capture.add("# 标题\n\n旧窗口");
  const result = capture.add("# 标题\n\n新窗口");
  assert(result.text.includes("旧窗口") && result.text.includes("新窗口"));
  assert.equal(result.uncertain_overlaps, 1, "a recycled window without overlap must be reported");
}
{
  const capture = merge.create(100);
  capture.add("a".repeat(90));
  const result = capture.add("b".repeat(80));
  assert.equal(result.text.length, 100);
  assert.equal(result.truncated, true);
}
console.log("ordered snapshot merge, virtual windows, code fences, repetition and size limits passed");

"use strict";

const assert = require("node:assert/strict");
const extractor = require("../extractor.js");

function test(name, callback) {
  try {
    callback();
    console.log("✓ " + name);
  } catch (error) {
    console.error("✗ " + name);
    throw error;
  }
}

test("识别常见导航、推荐和分享区域", function () {
  assert.equal(extractor.isNoiseName("page-sidebar related-posts"), true);
  assert.equal(extractor.isNoiseName("social-share-toolbar"), true);
  assert.equal(extractor.isNoiseName("article-content"), false);
  assert.equal(extractor.isNoiseName("main-navigation"), true);
  assert.equal(extractor.isNoiseName("VPSidebar"), true);
  assert.equal(extractor.isNoiseName("VPNav"), true);
  assert.equal(extractor.isNoiseName("edit-link-button"), true);
});

test("正文候选评分会压低链接密集的导航区域", function () {
  const articleScore = extractor.scoreCandidateMetrics({
    textLength: 1200,
    linkTextLength: 80,
    semanticBonus: 700,
    paragraphCount: 8,
    headingCount: 3,
    listItemCount: 5,
    codeBlockCount: 1,
    tableCount: 0,
  });
  const navigationScore = extractor.scoreCandidateMetrics({
    textLength: 1500,
    linkTextLength: 1400,
    semanticBonus: 100,
    paragraphCount: 1,
    headingCount: 0,
    listItemCount: 20,
    codeBlockCount: 0,
    tableCount: 0,
  });
  assert.ok(articleScore > navigationScore);
});

test("代码块内保留缩进，正文空白被规范化", function () {
  const input = "  标题   文本  \n\n\n```\n  const x = 1;  \n    return x;\n```\n\n正文   内容  ";
  assert.equal(
    extractor.normalizeStructuredText(input),
    "标题 文本\n\n```\n  const x = 1;\n    return x;\n```\n\n正文 内容",
  );
});

test("代码围栏保留语言标识", function () {
  assert.equal(
    extractor.normalizeStructuredText("```vue\n  const value = 1;  \n```"),
    "```vue\n  const value = 1;\n```",
  );
});

test("结构化内容会获得额外评分", function () {
  const plain = extractor.scoreCandidateMetrics({ textLength: 500 });
  const structured = extractor.scoreCandidateMetrics({
    textLength: 500,
    headingCount: 2,
    paragraphCount: 4,
    codeBlockCount: 1,
    tableCount: 1,
  });
  assert.ok(structured > plain);
});

test("限制超长网页正文的最大字符数", function () {
  assert.equal(extractor.MAX_EXTRACTED_CHARS, 500000);
});

console.log("extractor tests passed");

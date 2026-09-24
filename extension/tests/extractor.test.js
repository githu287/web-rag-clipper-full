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

test("识别内联隐藏状态和无障碍隐藏状态", function () {
  const inlineHidden = {
    nodeType: 1,
    hidden: false,
    style: { display: "none", visibility: "" },
    hasAttribute: function () { return false; },
    getAttribute: function () { return null; },
  };
  const ariaHidden = {
    nodeType: 1,
    hidden: false,
    style: {},
    hasAttribute: function () { return false; },
    getAttribute: function (name) { return name === "aria-hidden" ? "true" : null; },
  };
  assert.equal(extractor.isElementHidden(inlineHidden), true);
  assert.equal(extractor.isElementHidden(ariaHidden), true);
});

test("质量诊断可同时报告降级、短内容和截断", function () {
  assert.deepEqual(
    extractor.buildQualityWarnings({
      fallback: true,
      extractedCharCount: 100,
      headingCount: 0,
      truncated: true,
    }),
    ["fallback_root", "short_content", "truncated"],
  );
});

test("Markdown 链接文字会转义方括号和反斜杠", function () {
  assert.equal(extractor.escapeMarkdownLinkLabel("Vue [SFC] \\ guide"), "Vue \\[SFC\\] \\\\ guide");
});

test("行内代码会选择比内容中反引号更长的分隔符", function () {
  assert.equal(extractor.formatInlineCode("*.vue"), "`*.vue`");
  assert.equal(extractor.formatInlineCode("use `code` here"), "``use `code` here``");
});

test("正文规范化会保留嵌套列表缩进并移除孤立装饰星号", function () {
  assert.equal(
    extractor.normalizeStructuredText("- 父项目\n  - 子项目\n\n*\n\n正文"),
    "- 父项目\n  - 子项目\n\n正文",
  );
});

test("星号形式的装饰列表会规范为 Markdown 列表", function () {
  assert.equal(extractor.normalizeStructuredText("* 第一项\n* 第二项"), "- 第一项\n- 第二项");
});

console.log("extractor tests passed");

// extractor.js —— 可测试的网页正文提取引擎。
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPageExtractor = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const VERSION = "1.0.0";
  const MIN_MEANINGFUL_CHARS = 180;
  const MAX_EXTRACTED_CHARS = 500000;

  const CANDIDATE_SELECTORS = [
    "article",
    "main",
    "[role='main']",
    ".article-body",
    ".article-content",
    ".post-content",
    ".entry-content",
    ".markdown-body",
    ".theme-doc-markdown",
    "#content",
    ".content",
  ];

  const REMOVE_SELECTOR = [
    "script", "style", "noscript", "template", "nav", "footer", "aside",
    "iframe", "form", "button", "svg", "canvas", "dialog",
    "[hidden]", "[aria-hidden='true']", "[role='navigation']",
    "[role='complementary']", "[role='banner']", "[role='contentinfo']",
    "[role='dialog']", "[style*='display: none']", "[style*='display:none']",
    "[style*='visibility: hidden']", "[style*='visibility:hidden']",
  ].join(", ");

  const NOISE_PATTERN = /(^|[\s_-])(ad|ads|advert|advertisement|banner|promo|sponsor|sidebar|related|recommend(?:ed|ation)?|cookie|consent|modal|popup|newsletter|subscribe|social|share|breadcrumb|pagination|pager|comments?|toolbar|menu|nav|navigation|footer|outline|skip|edit)([\s_-]|$)/i;

  const BLOCK_TAGS = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DIV", "DL", "FIELDSET",
    "FIGCAPTION", "FIGURE", "FOOTER", "HEADER", "HR", "MAIN", "P",
    "SECTION",
  ]);

  function isNoiseName(value) {
    const normalized = String(value || "")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2");
    return NOISE_PATTERN.test(normalized);
  }

  function isNoiseElement(element) {
    return isNoiseName(element.id) || isNoiseName(element.className);
  }

  function normalizeStructuredText(value) {
    const source = String(value || "")
      .replace(/\r\n?/g, "\n")
      .replace(/[\u00a0\u200b\u200c\u200d\ufeff]/g, " ");
    const lines = source.split("\n");
    const output = [];
    let inCodeFence = false;
    let previousBlank = true;
    for (const originalLine of lines) {
      let line = originalLine;
      if (line.trim().startsWith("```")) {
        line = inCodeFence ? "```" : line.trim();
        inCodeFence = !inCodeFence;
      } else if (inCodeFence) {
        line = line.replace(/[ \t]+$/g, "");
      } else {
        line = line.replace(/[ \t\f\v]+/g, " ").trim();
      }
      const blank = line.length === 0;
      if (blank && previousBlank) continue;
      output.push(line);
      previousBlank = blank;
    }
    while (output.length > 0 && output[output.length - 1] === "") output.pop();
    return output.join("\n").trim();
  }

  function scoreCandidateMetrics(metrics) {
    const textLength = Math.max(0, Number(metrics.textLength) || 0);
    const linkTextLength = Math.max(0, Number(metrics.linkTextLength) || 0);
    const linkDensity = textLength > 0 ? Math.min(1, linkTextLength / textLength) : 1;
    const semanticBonus = Math.max(0, Number(metrics.semanticBonus) || 0);
    const structureBonus =
      (Number(metrics.paragraphCount) || 0) * 45 +
      (Number(metrics.headingCount) || 0) * 35 +
      (Number(metrics.listItemCount) || 0) * 18 +
      (Number(metrics.codeBlockCount) || 0) * 100 +
      (Number(metrics.tableCount) || 0) * 80;
    const linkPenalty = textLength * Math.max(0, linkDensity - 0.2) * 1.8;
    return textLength + structureBonus + semanticBonus - linkPenalty;
  }

  function semanticBonus(element) {
    const tag = element.tagName;
    if (tag === "ARTICLE") return 700;
    if (tag === "MAIN") return 600;
    if (element.getAttribute && element.getAttribute("role") === "main") return 550;
    const marker = `${element.id || ""} ${element.className || ""}`;
    if (/(article|post|entry|markdown|doc)[-_\s]?(body|content)?/i.test(marker)) return 350;
    return 100;
  }

  function collectMetrics(element) {
    const textLength = (element.textContent || "").trim().length;
    let linkTextLength = 0;
    element.querySelectorAll("a").forEach(function (link) {
      linkTextLength += (link.textContent || "").trim().length;
    });
    return {
      textLength,
      linkTextLength,
      semanticBonus: semanticBonus(element),
      paragraphCount: element.querySelectorAll("p").length,
      headingCount: element.querySelectorAll("h1,h2,h3,h4,h5,h6").length,
      listItemCount: element.querySelectorAll("li").length,
      codeBlockCount: element.querySelectorAll("pre").length,
      tableCount: element.querySelectorAll("table").length,
    };
  }

  function describeElement(element) {
    const tag = (element.tagName || "element").toLowerCase();
    if (element.id) return `${tag}#${String(element.id).slice(0, 60)}`;
    const classes = String(element.className || "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
    return classes.length > 0 ? `${tag}.${classes.join(".")}` : tag;
  }

  function selectRoot(doc) {
    const candidates = [];
    const seen = new Set();
    CANDIDATE_SELECTORS.forEach(function (selector) {
      doc.querySelectorAll(selector).forEach(function (element) {
        if (seen.has(element) || isNoiseElement(element)) return;
        seen.add(element);
        const metrics = collectMetrics(element);
        if (metrics.textLength === 0) return;
        candidates.push({
          element,
          metrics,
          score: scoreCandidateMetrics(metrics),
        });
      });
    });
    candidates.sort(function (a, b) { return b.score - a.score; });
    const best = candidates[0];
    if (best && best.metrics.textLength >= MIN_MEANINGFUL_CHARS) {
      return { element: best.element, metrics: best.metrics, candidateCount: candidates.length, fallback: false };
    }
    const body = doc.body || (doc.querySelector && doc.querySelector("body"));
    return {
      element: body,
      metrics: body ? collectMetrics(body) : { textLength: 0, linkTextLength: 0 },
      candidateCount: candidates.length,
      fallback: true,
    };
  }

  function cleanClone(rootElement) {
    const clone = rootElement.cloneNode(true);
    let removedNodes = 0;
    clone.querySelectorAll(REMOVE_SELECTOR).forEach(function (node) {
      node.remove();
      removedNodes += 1;
    });
    clone.querySelectorAll("*").forEach(function (node) {
      if (isNoiseElement(node)) {
        node.remove();
        removedNodes += 1;
      }
    });
    if ((rootElement.tagName || "").toUpperCase() === "BODY") {
      Array.from(clone.children || []).filter(function (node) {
        return node.tagName === "HEADER";
      }).forEach(function (node) {
        node.remove();
        removedNodes += 1;
      });
    }
    return { clone, removedNodes };
  }

  function serializeNode(node, context) {
    if (!node) return "";
    if (node.nodeType === 3) return String(node.nodeValue || "").replace(/\s+/g, " ");
    if (node.nodeType !== 1) return "";
    const tag = node.tagName;
    if (tag === "BR") return "\n";
    if (tag === "HR") return "\n\n---\n\n";
    if (tag === "PRE") {
      const code = String(node.textContent || "").replace(/^\n+|\n+$/g, "");
      let language = "";
      let ancestor = node.parentElement;
      for (let depth = 0; ancestor && depth < 3 && !language; depth += 1) {
        const match = String(ancestor.className || "").match(/(?:^|\s)language-([a-z0-9_+-]+)/i);
        language = match ? match[1].toLowerCase() : "";
        ancestor = ancestor.parentElement;
      }
      return code ? `\n\n\`\`\`${language}\n${code}\n\`\`\`\n\n` : "";
    }
    if (/^H[1-6]$/.test(tag)) {
      const level = Number(tag.slice(1));
      return `\n\n${"#".repeat(level)} ${String(node.textContent || "").trim()}\n\n`;
    }
    if (tag === "BLOCKQUOTE") {
      const quote = serializeChildren(node, context).trim().split("\n").map(function (line) {
        return line ? `> ${line}` : ">";
      }).join("\n");
      return `\n\n${quote}\n\n`;
    }
    if (tag === "LI") {
      const ordered = node.parentElement && node.parentElement.tagName === "OL";
      const index = ordered ? Array.from(node.parentElement.children).indexOf(node) + 1 : null;
      const prefix = ordered ? `${index}. ` : "- ";
      return `\n${prefix}${serializeChildren(node, context).trim()}`;
    }
    if (tag === "TABLE") {
      const rows = [];
      node.querySelectorAll("tr").forEach(function (row) {
        const cells = Array.from(row.children || [])
          .filter(function (cell) { return cell.tagName === "TH" || cell.tagName === "TD"; })
          .map(function (cell) { return String(cell.textContent || "").replace(/\s+/g, " ").trim(); })
          .filter(Boolean);
        if (cells.length > 0) rows.push(cells.join(" | "));
      });
      return rows.length > 0 ? `\n\n${rows.join("\n")}\n\n` : "";
    }
    if (tag === "IMG") {
      const alt = String(node.getAttribute("alt") || "").trim();
      return alt ? ` [图片：${alt}] ` : "";
    }
    if (tag === "SPAN" && node.classList && node.classList.contains("lang") && node.parentElement && node.parentElement.querySelector("pre")) {
      return "";
    }

    const content = serializeChildren(node, context);
    if (tag === "P" || BLOCK_TAGS.has(tag)) return `\n\n${content}\n\n`;
    return content;
  }

  function serializeChildren(element, context) {
    return Array.from(element.childNodes || []).map(function (child) {
      return serializeNode(child, context);
    }).join("");
  }

  function extract(doc) {
    const selected = selectRoot(doc);
    if (!selected.element) {
      return {
        text: "",
        diagnostics: {
          extractor_version: VERSION,
          strategy: "none",
          candidate_count: selected.candidateCount,
          source_char_count: 0,
          extracted_char_count: 0,
          removed_node_count: 0,
          fallback: true,
          truncated: false,
        },
      };
    }
    const cleaned = cleanClone(selected.element);
    let text = normalizeStructuredText(serializeChildren(cleaned.clone, {}));
    let truncated = false;
    if (text.length > MAX_EXTRACTED_CHARS) {
      text = text.slice(0, MAX_EXTRACTED_CHARS).trimEnd();
      truncated = true;
    }
    return {
      text,
      diagnostics: {
        extractor_version: VERSION,
        strategy: describeElement(selected.element),
        candidate_count: selected.candidateCount,
        source_char_count: selected.metrics.textLength || 0,
        extracted_char_count: text.length,
        removed_node_count: cleaned.removedNodes,
        link_density: selected.metrics.textLength > 0
          ? Number(((selected.metrics.linkTextLength || 0) / selected.metrics.textLength).toFixed(3))
          : 0,
        fallback: selected.fallback,
        truncated,
      },
    };
  }

  return {
    VERSION,
    MAX_EXTRACTED_CHARS,
    extract,
    isNoiseName,
    normalizeStructuredText,
    scoreCandidateMetrics,
  };
});

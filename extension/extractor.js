// extractor.js —— 可测试的网页正文提取引擎。
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPageExtractor = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const VERSION = "1.2.0";
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
    ".vp-doc",
    ".s-prose",
    ".js-post-body",
    ".story-body",
    ".article__body",
    ".notion-page-content",
    "[itemprop='articleBody']",
    "#mw-content-text",
    "#content",
    ".content",
  ];

  const HARD_REMOVE_SELECTOR = [
    "script", "style", "noscript", "template", "nav",
    "iframe", "form", "button", "svg", "canvas", "dialog",
    "[role='navigation']", "[role='banner']", "[role='dialog']",
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

  function isElementHidden(element, view) {
    if (!element || element.nodeType !== 1) return false;
    if (element.hidden || element.hasAttribute("hidden") || element.hasAttribute("inert")) return true;
    if (String(element.getAttribute("aria-hidden") || "").toLowerCase() === "true") return true;
    const inlineStyle = element.style || {};
    if (inlineStyle.display === "none" || inlineStyle.visibility === "hidden" || inlineStyle.visibility === "collapse") {
      return true;
    }
    if (!view || typeof view.getComputedStyle !== "function") return false;
    try {
      const style = view.getComputedStyle(element);
      return style.display === "none" ||
        style.visibility === "hidden" ||
        style.visibility === "collapse" ||
        style.contentVisibility === "hidden";
    } catch (_error) {
      return false;
    }
  }

  function isEffectivelyHidden(element, view) {
    let current = element;
    while (current && current.nodeType === 1) {
      if (isElementHidden(current, view)) return true;
      current = current.parentElement;
    }
    return false;
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

  function collectMetrics(element, semanticElement) {
    const textLength = (element.textContent || "").trim().length;
    let linkTextLength = 0;
    element.querySelectorAll("a").forEach(function (link) {
      linkTextLength += (link.textContent || "").trim().length;
    });
    return {
      textLength,
      linkTextLength,
      linkCount: element.querySelectorAll("a[href]").length,
      semanticBonus: semanticBonus(semanticElement || element),
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

  function shouldRemoveSemanticContainer(element, rootElement) {
    const tag = element.tagName;
    const role = String(element.getAttribute("role") || "").toLowerCase();
    if (tag !== "ASIDE" && tag !== "FOOTER" && role !== "complementary" && role !== "contentinfo") {
      return false;
    }
    const article = element.closest && element.closest("article");
    const belongsToArticle = article && (article === rootElement || (rootElement.contains && rootElement.contains(article)));
    return !belongsToArticle || isNoiseElement(element);
  }

  function cleanClone(rootElement, doc) {
    const clone = rootElement.cloneNode(true);
    const sourceNodes = [rootElement].concat(Array.from(rootElement.querySelectorAll("*")));
    const cloneNodes = [clone].concat(Array.from(clone.querySelectorAll("*")));
    const marked = new Set();
    let hiddenRemovedNodes = 0;
    let noiseRemovedNodes = 0;

    sourceNodes.forEach(function (source, index) {
      if (index === 0) return;
      const target = cloneNodes[index];
      if (!target) return;
      let ancestor = target.parentElement;
      while (ancestor && !marked.has(ancestor)) ancestor = ancestor.parentElement;
      if (ancestor) return;

      const hidden = isElementHidden(source, doc && doc.defaultView);
      const hardNoise = source.matches && source.matches(HARD_REMOVE_SELECTOR);
      const namedNoise = isNoiseElement(source);
      const semanticNoise = shouldRemoveSemanticContainer(source, rootElement);
      if (!hidden && !hardNoise && !namedNoise && !semanticNoise) return;
      marked.add(target);
      if (hidden) hiddenRemovedNodes += 1;
      else noiseRemovedNodes += 1;
    });

    if ((rootElement.tagName || "").toUpperCase() === "BODY") {
      Array.from(clone.children || []).filter(function (node) {
        return node.tagName === "HEADER";
      }).forEach(function (node) {
        if (!marked.has(node)) {
          marked.add(node);
          noiseRemovedNodes += 1;
        }
      });
    }
    marked.forEach(function (node) { node.remove(); });
    return {
      clone,
      removedNodes: hiddenRemovedNodes + noiseRemovedNodes,
      hiddenRemovedNodes,
      noiseRemovedNodes,
    };
  }

  function prepareCandidate(element, doc) {
    const cleaned = cleanClone(element, doc);
    const metrics = collectMetrics(cleaned.clone, element);
    return {
      element,
      cleaned,
      metrics,
      sourceTextLength: (element.textContent || "").trim().length,
      score: scoreCandidateMetrics(metrics),
    };
  }

  function linkDensity(metrics) {
    return metrics.textLength > 0 ? (metrics.linkTextLength || 0) / metrics.textLength : 0;
  }

  function expandRoot(candidate, doc) {
    let current = candidate;
    const original = candidate.element;
    for (let depth = 0; depth < 3; depth += 1) {
      const parent = current.element && current.element.parentElement;
      if (!parent || parent === doc.body || !/^(ARTICLE|MAIN|SECTION|DIV)$/.test(parent.tagName || "")) break;
      if (isNoiseElement(parent) || isEffectivelyHidden(parent, doc.defaultView)) break;
      const expanded = prepareCandidate(parent, doc);
      const extraChars = expanded.metrics.textLength - current.metrics.textLength;
      const maximumExtra = Math.max(1800, current.metrics.textLength * 0.65);
      const addsStructure = expanded.metrics.headingCount > current.metrics.headingCount ||
        expanded.metrics.paragraphCount > current.metrics.paragraphCount ||
        expanded.metrics.listItemCount > current.metrics.listItemCount;
      if (extraChars < 30 || extraChars > maximumExtra || !addsStructure ||
          linkDensity(expanded.metrics) > 0.4 || expanded.score < current.score * 0.72) {
        break;
      }
      current = expanded;
    }
    current.expandedFrom = current.element === original ? null : describeElement(original);
    return current;
  }

  function selectRoot(doc) {
    const candidates = [];
    const seen = new Set();
    CANDIDATE_SELECTORS.forEach(function (selector) {
      doc.querySelectorAll(selector).forEach(function (element) {
        if (seen.has(element) || isNoiseElement(element) || isEffectivelyHidden(element, doc.defaultView)) return;
        seen.add(element);
        const candidate = prepareCandidate(element, doc);
        if (candidate.metrics.textLength === 0) return;
        candidates.push(candidate);
      });
    });
    candidates.sort(function (a, b) { return b.score - a.score; });
    const best = candidates[0];
    if (best && best.metrics.textLength >= MIN_MEANINGFUL_CHARS) {
      const expanded = expandRoot(best, doc);
      expanded.candidateCount = candidates.length;
      expanded.fallback = false;
      return expanded;
    }
    const body = doc.body || (doc.querySelector && doc.querySelector("body"));
    const fallback = body ? prepareCandidate(body, doc) : {
      element: null,
      cleaned: null,
      metrics: { textLength: 0, linkTextLength: 0 },
      sourceTextLength: 0,
    };
    fallback.candidateCount = candidates.length;
    fallback.fallback = true;
    fallback.expandedFrom = null;
    return fallback;
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
      let ancestor = node;
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
    if (tag === "A") {
      context.linkCount += 1;
      const label = String(node.textContent || "").replace(/\s+/g, " ").trim() ||
        String(node.querySelector("img[alt]") && node.querySelector("img[alt]").getAttribute("alt") || "").trim();
      const destination = normalizeLinkDestination(node);
      if (!label || !destination) return serializeChildren(node, context);
      context.preservedLinkCount += 1;
      return `[${escapeMarkdownLinkLabel(label)}](<${destination}>)`;
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

  function normalizeLinkDestination(anchor) {
    const rawHref = String(anchor.getAttribute("href") || "").trim();
    if (!rawHref) return "";
    try {
      const resolved = new URL(rawHref, anchor.ownerDocument && anchor.ownerDocument.baseURI || undefined);
      if (resolved.protocol !== "http:" && resolved.protocol !== "https:" && resolved.protocol !== "mailto:") {
        return "";
      }
      return resolved.href.replace(/</g, "%3C").replace(/>/g, "%3E");
    } catch (_error) {
      return "";
    }
  }

  function escapeMarkdownLinkLabel(value) {
    return String(value || "")
      .replace(/\\/g, "\\\\")
      .replace(/\[/g, "\\[")
      .replace(/\]/g, "\\]");
  }

  function buildQualityWarnings(details) {
    const warnings = [];
    if (details.fallback) warnings.push("fallback_root");
    if (details.extractedCharCount < MIN_MEANINGFUL_CHARS) warnings.push("short_content");
    if (details.extractedCharCount >= 1000 && details.headingCount === 0) warnings.push("missing_headings");
    if (details.truncated) warnings.push("truncated");
    return warnings;
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
    const cleaned = selected.cleaned;
    const serializationContext = { linkCount: 0, preservedLinkCount: 0 };
    let text = normalizeStructuredText(serializeChildren(cleaned.clone, serializationContext));
    let truncated = false;
    if (text.length > MAX_EXTRACTED_CHARS) {
      text = text.slice(0, MAX_EXTRACTED_CHARS).trimEnd();
      truncated = true;
    }
    const qualityWarnings = buildQualityWarnings({
      fallback: selected.fallback,
      extractedCharCount: text.length,
      headingCount: selected.metrics.headingCount || 0,
      truncated,
    });
    return {
      text,
      diagnostics: {
        extractor_version: VERSION,
        strategy: describeElement(selected.element),
        expanded_from: selected.expandedFrom,
        candidate_count: selected.candidateCount,
        source_char_count: selected.sourceTextLength || 0,
        cleaned_source_char_count: selected.metrics.textLength || 0,
        extracted_char_count: text.length,
        removed_node_count: cleaned.removedNodes,
        hidden_removed_node_count: cleaned.hiddenRemovedNodes,
        noise_removed_node_count: cleaned.noiseRemovedNodes,
        heading_count: selected.metrics.headingCount || 0,
        paragraph_count: selected.metrics.paragraphCount || 0,
        list_item_count: selected.metrics.listItemCount || 0,
        code_block_count: selected.metrics.codeBlockCount || 0,
        table_count: selected.metrics.tableCount || 0,
        link_count: serializationContext.linkCount,
        preserved_link_count: serializationContext.preservedLinkCount,
        link_density: Number(linkDensity(selected.metrics).toFixed(3)),
        fallback: selected.fallback,
        truncated,
        quality_warnings: qualityWarnings,
      },
    };
  }

  return {
    VERSION,
    MAX_EXTRACTED_CHARS,
    extract,
    buildQualityWarnings,
    escapeMarkdownLinkLabel,
    isElementHidden,
    isNoiseName,
    normalizeStructuredText,
    normalizeLinkDestination,
    scoreCandidateMetrics,
  };
});

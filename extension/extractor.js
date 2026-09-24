// extractor.js —— 可测试的网页正文提取引擎。
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPageExtractor = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const VERSION = "1.4.0";
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
    "interactive-example", "mdn-live-sample-result",
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
        line = line.replace(/^\s*\*\s+/, "- ");
        const nestedList = line.match(/^(\s+)((?:[-+*]|\d+\.)\s+.*)$/);
        if (nestedList) {
          line = nestedList[1].replace(/\t/g, "  ") + nestedList[2].replace(/[ \t\f\v]+/g, " ").trimEnd();
        } else {
          line = line.replace(/[ \t\f\v]+/g, " ").trim();
        }
      }
      const blank = line.length === 0;
      if (!inCodeFence && line.trim() === "*") continue;
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
    const inlineCodeCount = Array.from(element.querySelectorAll("code")).filter(function (node) {
      return !node.closest("pre");
    }).length;
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
      inlineCodeCount,
      emphasisCount: element.querySelectorAll("strong,b,em,i,del,s").length,
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

  function isShadowCodeExample(element) {
    return element && element.tagName === "MDN-CODE-EXAMPLE" &&
      element.shadowRoot && element.shadowRoot.querySelector("pre");
  }

  function cloneForExtraction(rootElement) {
    const pairs = [];
    let shadowRootCount = 0;

    function cloneNode(source) {
      if (source.nodeType !== 1) return source.cloneNode(true);
      const target = source.cloneNode(false);
      pairs.push({ source, target });

      if (source.shadowRoot) {
        shadowRootCount += 1;
        const container = source.ownerDocument.createElement("div");
        container.setAttribute("data-web-rag-shadow-root", "");
        target.appendChild(container);
        const shadowChildren = isShadowCodeExample(source)
          ? [source.shadowRoot.querySelector("pre")]
          : Array.from(source.shadowRoot.childNodes || []);
        shadowChildren.filter(Boolean).forEach(function (child) {
          container.appendChild(cloneNode(child));
        });
      }

      Array.from(source.childNodes || []).forEach(function (child) {
        target.appendChild(cloneNode(child));
      });
      return target;
    }

    return { clone: cloneNode(rootElement), pairs, shadowRootCount };
  }

  function cleanClone(rootElement, doc) {
    const materialized = cloneForExtraction(rootElement);
    const clone = materialized.clone;
    const marked = new Set();
    let hiddenRemovedNodes = 0;
    let noiseRemovedNodes = 0;

    materialized.pairs.forEach(function (pair, index) {
      if (index === 0) return;
      const source = pair.source;
      const target = pair.target;
      let ancestor = target.parentElement;
      while (ancestor && !marked.has(ancestor)) ancestor = ancestor.parentElement;
      if (ancestor) return;

      const hidden = !isShadowCodeExample(source) && isElementHidden(source, doc && doc.defaultView);
      const meaningfulTableButton = source.tagName === "BUTTON" && source.closest && source.closest("td,th");
      const hardNoise = source.matches && source.matches(HARD_REMOVE_SELECTOR) && !meaningfulTableButton;
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
      shadowRootCount: materialized.shadowRootCount,
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
        const classes = String(ancestor.className || "");
        const match = classes.match(/(?:^|\s)language-([a-z0-9_+-]+)/i) ||
          classes.match(/(?:^|\s)brush:\s*([a-z0-9_+-]+)/i);
        language = match ? normalizeCodeLanguage(match[1]) : "";
        ancestor = ancestor.parentElement;
      }
      return code ? `\n\n\`\`\`${language}\n${code}\n\`\`\`\n\n` : "";
    }
    if (/^H[1-6]$/.test(tag)) {
      const level = Number(tag.slice(1));
      const previousSuppressLinks = context.suppressLinks;
      context.suppressLinks = true;
      const heading = formatHeadingContent(serializeChildren(node, context).trim());
      context.suppressLinks = previousSuppressLinks;
      return `\n\n${"#".repeat(level)} ${heading}\n\n`;
    }
    if (tag === "BLOCKQUOTE") {
      const quote = serializeChildren(node, context).trim().split("\n").map(function (line) {
        return line ? `> ${line}` : ">";
      }).join("\n");
      return `\n\n${quote}\n\n`;
    }
    if (tag === "UL" || tag === "OL") return serializeList(node, context, 0);
    if (tag === "LI") return serializeListItem(node, context, 0, 1, false);
    if (tag === "TABLE") {
      return serializeTable(node, context);
    }
    if (tag === "IMG") {
      const alt = String(node.getAttribute("alt") || "").trim();
      return alt ? ` [图片：${alt}] ` : "";
    }
    if (tag === "CODE") {
      context.inlineCodeCount += 1;
      return formatInlineCode(node.textContent || "");
    }
    if (tag === "A") {
      if (context.suppressLinks) return serializeChildren(node, context);
      context.linkCount += 1;
      const label = String(node.textContent || "").replace(/\s+/g, " ").trim() ||
        String(node.querySelector("img[alt]") && node.querySelector("img[alt]").getAttribute("alt") || "").trim();
      const destination = normalizeLinkDestination(node);
      if (!label || !destination) return serializeChildren(node, context);
      context.preservedLinkCount += 1;
      return `[${escapeMarkdownLinkLabel(label)}](<${destination}>)`;
    }
    if (tag === "STRONG" || tag === "B") {
      context.emphasisCount += 1;
      return wrapInlineMarkdown(serializeChildren(node, context), "**");
    }
    if (tag === "EM" || tag === "I") {
      context.emphasisCount += 1;
      return wrapInlineMarkdown(serializeChildren(node, context), "*");
    }
    if (tag === "DEL" || tag === "S") {
      context.emphasisCount += 1;
      return wrapInlineMarkdown(serializeChildren(node, context), "~~");
    }
    if (tag === "SPAN" && node.classList && node.classList.contains("lang") && node.parentElement && node.parentElement.querySelector("pre")) {
      return "";
    }

    const content = serializeChildren(node, context);
    if (!content.trim()) {
      const accessibleLabel = String(node.getAttribute("aria-label") || node.getAttribute("title") || "").trim();
      if (accessibleLabel) return accessibleLabel;
    }
    if (tag === "P" || BLOCK_TAGS.has(tag)) return `\n\n${content}\n\n`;
    return content;
  }

  function serializeChildren(element, context) {
    return Array.from(element.childNodes || []).map(function (child) {
      return serializeNode(child, context);
    }).join("");
  }

  function normalizeCodeLanguage(language) {
    return String(language || "").toLowerCase();
  }

  function formatHeadingContent(value) {
    return String(value || "").replace(/(^|[^`])(<\/?[a-z][^>\n]*>)(?!`)/gi, "$1`$2`");
  }

  function serializeList(list, context, depth) {
    const ordered = list.tagName === "OL";
    return Array.from(list.children || []).filter(function (child) {
      return child.tagName === "LI";
    }).map(function (item, index) {
      return serializeListItem(item, context, depth, index + 1, ordered);
    }).join("");
  }

  function serializeListItem(item, context, depth, index, ordered) {
    const nestedLists = [];
    const content = Array.from(item.childNodes || []).map(function (child) {
      if (child.nodeType === 1 && (child.tagName === "UL" || child.tagName === "OL")) {
        nestedLists.push(child);
        return "";
      }
      return serializeNode(child, context);
    }).join("").trim();
    const indent = "  ".repeat(depth);
    const prefix = ordered ? `${index}. ` : "- ";
    const continuationIndent = "  ".repeat(depth + 1);
    const line = content.replace(/\n+/g, "\n" + continuationIndent);
    return `\n${indent}${prefix}${line}` + nestedLists.map(function (nested) {
      return serializeList(nested, context, depth + 1);
    }).join("");
  }

  function serializeTable(table, context) {
    const rowElements = Array.from(table.querySelectorAll("tr")).filter(function (row) {
      return row.closest("table") === table;
    });
    const rows = rowElements.map(function (row) {
      return Array.from(row.children || [])
        .filter(function (cell) { return cell.tagName === "TH" || cell.tagName === "TD"; })
        .map(function (cell) {
          return normalizeStructuredText(serializeChildren(cell, context))
            .replace(/\n+/g, "<br>")
            .replace(/\|/g, "\\|")
            .trim();
        });
    }).filter(function (cells) { return cells.length > 0; });
    if (rows.length === 0) return "";

    const columnCount = rows.reduce(function (maximum, row) {
      return Math.max(maximum, row.length);
    }, 0);
    rows.forEach(function (row) {
      while (row.length < columnCount) row.push("");
    });
    const firstRowIsHeader = Array.from(rowElements[0].children || []).some(function (cell) {
      return cell.tagName === "TH";
    });
    if (!firstRowIsHeader) rows.unshift(Array(columnCount).fill(""));
    const separator = Array(columnCount).fill("---");
    rows.splice(1, 0, separator);
    const caption = Array.from(table.children || []).find(function (child) {
      return child.tagName === "CAPTION";
    });
    const captionText = caption ? normalizeStructuredText(serializeChildren(caption, context)) : "";
    const markdownRows = rows.map(function (row) { return `| ${row.join(" | ")} |`; }).join("\n");
    return `\n\n${captionText ? `*${captionText}*\n\n` : ""}${markdownRows}\n\n`;
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

  function formatInlineCode(value) {
    const code = String(value || "").replace(/\s+/g, " ");
    if (!code.trim()) return code;
    const runs = code.match(/`+/g) || [];
    const longestRun = runs.reduce(function (maximum, run) {
      return Math.max(maximum, run.length);
    }, 0);
    const delimiter = "`".repeat(longestRun + 1);
    const needsPadding = /^\s|\s$|^`|`$/.test(code);
    const padding = needsPadding ? " " : "";
    return delimiter + padding + code + padding + delimiter;
  }

  function wrapInlineMarkdown(value, marker) {
    const content = String(value || "");
    if (!content.trim()) return content;
    const leading = (content.match(/^\s*/) || [""])[0];
    const trailing = (content.match(/\s*$/) || [""])[0];
    const core = content.slice(leading.length, content.length - trailing.length);
    return leading + marker + core + marker + trailing;
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
    const serializationContext = {
      linkCount: 0,
      preservedLinkCount: 0,
      inlineCodeCount: 0,
      emphasisCount: 0,
    };
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
        shadow_root_count: cleaned.shadowRootCount,
        heading_count: selected.metrics.headingCount || 0,
        paragraph_count: selected.metrics.paragraphCount || 0,
        list_item_count: selected.metrics.listItemCount || 0,
        code_block_count: selected.metrics.codeBlockCount || 0,
        inline_code_count: serializationContext.inlineCodeCount,
        emphasis_count: serializationContext.emphasisCount,
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
    formatInlineCode,
    isElementHidden,
    isNoiseName,
    normalizeStructuredText,
    normalizeLinkDestination,
    scoreCandidateMetrics,
  };
});

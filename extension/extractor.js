// extractor.js —— 可测试的网页正文提取引擎。
"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPageExtractor = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const VERSION = "1.7.0";
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
    ".vt-doc",
    ".s-prose",
    ".js-post-body",
    ".story-body",
    ".article__body",
    ".notion-page-content",
    "#js_content",
    ".RichText.ztext",
    "#article_content",
    "#cnblogs_post_body",
    ".markdown-section",
    "[itemprop='articleBody']",
    "#mw-content-text",
    "#content",
    ".content",
  ];

  const HARD_REMOVE_SELECTOR = [
    "script", "style", "noscript", "template", "nav",
    "iframe", "button", "svg", "canvas", "dialog", "clipboard-copy",
    ".zeroclipboard-container", ".code-block-extension-header",
    "interactive-example", "mdn-live-sample-result",
    "[role='navigation']", "[role='banner']", "[role='dialog']",
  ].join(", ");

  const NOISE_PATTERN = /(^|[\s_-])(ad|ads|advert|advertisement|banner|promo|sponsor|sidebar|related|recommend(?:ed|ation)?|cookie|consent|modal|popup|newsletter|subscribe|social|share|breadcrumb|pagination|pager|comments?|toolbar|menu|nav|navigation|footer|outline|skip|edit)([\s_-]|$)/i;

  const BLOCK_TAGS = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DIV", "DL", "FIELDSET",
    "FIGCAPTION", "FIGURE", "FOOTER", "HEADER", "HR", "MAIN", "P",
    "SECTION", "DETAILS", "SUMMARY",
  ]);

  // Narrow, DOM-checked profiles avoid selecting large application shells over
  // known article bodies. Missing/hidden roots always fall back to heuristics.
  const SITE_PROFILES = [
    { id: "github-readme", hosts: ["github.com"], root: "article.markdown-body", title: "article.markdown-body h1" },
    { id: "wechat", hosts: ["mp.weixin.qq.com"], root: "#js_content", title: "#activity-name" },
    { id: "cnblogs", hosts: ["www.cnblogs.com", "cnblogs.com"], root: "#cnblogs_post_body", title: "#cb_post_title_url" },
    { id: "juejin", hosts: ["juejin.cn"], root: "#article-root .markdown-body, #article-root", title: "h1.article-title" },
    { id: "csdn", hosts: ["blog.csdn.net"], root: "#article_content", title: "#articleContentId" },
    { id: "zhihu-article", hosts: ["zhuanlan.zhihu.com"], root: ".Post-RichTextContainer, .Post-RichText", title: ".Post-Title" },
    { id: "xinhua", hosts: ["www.news.cn", "news.cn"], root: "#detail", title: "h1" },
    { id: "ndrc", hosts: ["www.ndrc.gov.cn", "ndrc.gov.cn"], root: ".TRS_Editor, .TRS_UEDITOR", title: ".article_title" },
    { id: "people-photo", hosts: ["pic.people.com.cn", "lianghui.people.com.cn"], root: ".content.clear", title: "h1", require: "#picG" },
  ];

  function findSiteRoot(doc) {
    let host;
    try { host = new URL(doc.URL).hostname; } catch (_error) { return null; }
    const profile = SITE_PROFILES.find(function (item) { return item.hosts.includes(host); });
    if (!profile || profile.require && !doc.querySelector(profile.require)) return null;
    const roots = Array.from(doc.querySelectorAll(profile.root)).filter(function (element) {
      return !isEffectivelyHidden(element, doc.defaultView) &&
        ((element.textContent || "").trim() || element.querySelector("img"));
    });
    if (!roots.length) return null;
    roots.sort(function (a, b) { return (b.textContent || "").length - (a.textContent || "").length; });
    const title = Array.from(doc.querySelectorAll(profile.title)).find(function (element) {
      return !isEffectivelyHidden(element, doc.defaultView);
    });
    return { element: roots[0], profile: profile.id, title: title && (title.textContent || "").replace(/\s+/g, " ").trim() };
  }

  function hasAccessBarrier(doc) {
    const text = String(doc.body && doc.body.innerText || doc.body && doc.body.textContent || "").trim();
    // Treat only a small, unmistakable error response as a barrier. An article
    // discussing HTTP errors or quoting these strings is legitimate content.
    if (!text || text.length > 1600) return false;
    if (!doc.querySelector("article") && text.length <= 400 && /^(?:403\s+Forbidden|Access\s+Denied|Forbidden)(?:\s|$)/i.test(text)) return true;
    try {
      const payload = JSON.parse(text);
      if (payload && payload.error && Number(payload.error.code) === 40362 &&
          /请求存在异常|限制本次访问/.test(payload.error.message || "")) return true;
    } catch (_error) {}
    return false;
  }

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
    let fenceLength = 0;
    let fenceIndent = "";
    let previousBlank = true;
    for (const originalLine of lines) {
      let line = originalLine;
      const fence = line.trim().match(/^(`{3,})(.*)$/);
      if (!inCodeFence && fence) {
        fenceIndent = (line.match(/^\s*/) || [""])[0];
        line = line.trimEnd();
        inCodeFence = true;
        fenceLength = fence[1].length;
      } else if (inCodeFence && fence && fence[1].length >= fenceLength && !fence[2].trim()) {
        line = fenceIndent + "`".repeat(fenceLength);
        inCodeFence = false;
        fenceLength = 0;
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
      tableCount: Array.from(element.querySelectorAll("table")).filter(function (table) { return !table.closest("pre"); }).length,
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

  function cloneForExtraction(rootElement, range) {
    const pairs = [];
    let shadowRootCount = 0;

    function cloneNode(source) {
      if (range && !range.intersectsNode(source)) return null;
      if (source.nodeType !== 1) {
        const cloned = source.cloneNode(true);
        if (range && source.nodeType === 3) {
          const start = source === range.startContainer ? range.startOffset : 0;
          const end = source === range.endContainer ? range.endOffset : source.length;
          cloned.nodeValue = source.nodeValue.slice(start, end);
        }
        return cloned;
      }
      const target = source.cloneNode(false);
      if (source.tagName === "IMG") {
        // Freeze the original responsive/lazy source before detaching. Never
        // let a page-supplied data-web-rag attribute override this value.
        const lazy = source.getAttribute("data-src") || source.getAttribute("data-original") || source.getAttribute("data-lazy-src");
        const raw = source.naturalWidth > 32 && source.currentSrc ? source.currentSrc : lazy || source.currentSrc || source.getAttribute("src");
        target.removeAttribute("data-web-rag-image");
        target.removeAttribute("data-web-rag-small-image");
        const knownNatural = source.currentSrc && raw === source.currentSrc && source.naturalWidth > 0 && source.naturalHeight > 0;
        const explicitSize = !lazy && source.hasAttribute("width") && source.hasAttribute("height");
        const width = knownNatural ? source.naturalWidth : explicitSize ? Number(source.getAttribute("width")) : 0;
        const height = knownNatural ? source.naturalHeight : explicitSize ? Number(source.getAttribute("height")) : 0;
        if (width > 0 && height > 0 && width <= 48 && height <= 48) target.setAttribute("data-web-rag-small-image", "true");
        try {
          const url = new URL(raw || "", source.baseURI);
          if (raw && /^https?:$/.test(url.protocol) && url.href.length <= 8192) target.setAttribute("data-web-rag-image", url.href);
        } catch (_error) {}
      }
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
          const cloned = cloneNode(child);
          if (cloned) container.appendChild(cloned);
        });
      }

      // Follow the rendered slot tree once; copying both light and shadow DOM
      // duplicates slotted paragraphs and can include unrendered fallback text.
      const children = source.tagName === "SLOT" && source.assignedNodes
        ? source.assignedNodes({ flatten: true }) : source.childNodes;
      Array.from(source.shadowRoot ? [] : children || []).forEach(function (child) {
        const cloned = cloneNode(child);
        if (cloned) target.appendChild(cloned);
      });
      return target;
    }

    return { clone: cloneNode(rootElement), pairs, shadowRootCount };
  }

  function cleanClone(rootElement, doc, options) {
    const explicit = options && options.mode !== "article";
    const materialized = cloneForExtraction(rootElement, options && options.range);
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
      const hardSelector = explicit
        ? "script,style,noscript,template,iframe,svg,canvas,dialog,input,textarea,select,clipboard-copy,.zeroclipboard-container,.code-block-extension-header,[role='dialog'],[data-web-rag-picker]"
        : HARD_REMOVE_SELECTOR + ",input,textarea,select,[data-web-rag-picker]";
      const hardNoise = source.matches && source.matches(hardSelector) && !meaningfulTableButton;
      // Syntax highlighters use classes such as hljs-comment/comment for actual
      // source code. Applying page-comment cleanup here silently deletes code.
      const inCode = source.closest && source.closest("pre,code");
      const namedNoise = !explicit && !inCode && isNoiseElement(source);
      const semanticNoise = !explicit && shouldRemoveSemanticContainer(source, rootElement);
      if (!hidden && !hardNoise && !namedNoise && !semanticNoise) return;
      marked.add(target);
      if (hidden) hiddenRemovedNodes += 1;
      else noiseRemovedNodes += 1;
    });

    if (!explicit && (rootElement.tagName || "").toUpperCase() === "BODY") {
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
    const site = findSiteRoot(doc);
    if (site) {
      const candidate = prepareCandidate(site.element, doc);
      candidate.candidateCount = 1;
      candidate.fallback = false;
      candidate.expandedFrom = null;
      candidate.siteProfile = site.profile;
      candidate.articleTitle = site.title;
      return candidate;
    }
    const candidates = [];
    const seen = new Set();
    CANDIDATE_SELECTORS.forEach(function (selector) {
      doc.querySelectorAll(selector).forEach(function (element) {
        if (seen.size >= 48 || seen.has(element) || isNoiseElement(element) || isEffectivelyHidden(element, doc.defaultView)) return;
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
      const codeNode = Array.from(node.children || []).find(function (child) { return child.tagName === "CODE"; }) || node;
      const code = serializeCodeText(codeNode).replace(/^\n+|\n+$/g, "");
      let language = "";
      let ancestor = codeNode;
      for (let depth = 0; ancestor && depth < 4 && !language; depth += 1) {
        const classes = String(ancestor.className || "");
        const match = classes.match(/(?:^|\s)language-([a-z0-9_+-]+)/i) ||
          classes.match(/(?:^|\s)brush:\s*([a-z0-9_+-]+)/i);
        language = match ? normalizeCodeLanguage(match[1]) : "";
        ancestor = ancestor.parentElement;
      }
      const longestRun = (code.match(/`+/g) || []).reduce(function (longest, run) { return Math.max(longest, run.length); }, 0);
      const fence = "`".repeat(Math.max(3, longestRun + 1));
      return code ? `\n\n${fence}${language}\n${code}\n${fence}\n\n` : "";
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

  function serializeCodeText(node) {
    const numberedLines = node.querySelectorAll && node.querySelectorAll("td.hljs-ln-code");
    if (numberedLines && numberedLines.length) {
      return Array.from(numberedLines).map(function (line) { return line.textContent || ""; }).join("\n");
    }
    function walk(current) {
      if (current.nodeType === 3) return current.nodeValue || "";
      if (current.nodeType !== 1) return "";
      if (current.tagName === "BR") return "\n";
      if (current.matches(".hljs-ln-numbers,.code-block-extension-header,button,clipboard-copy,.zeroclipboard-container")) return "";
      const text = Array.from(current.childNodes || []).map(walk).join("");
      return /^(DIV|P|TR)$/.test(current.tagName) && text && !text.endsWith("\n") ? text + "\n" : text;
    }
    return walk(node);
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
    const continuationIndent = " ".repeat(indent.length + prefix.length);
    // Every newline matters inside a fenced example. Collapsing newline runs
    // here removed source-code blank lines in Vue's numbered list examples.
    const line = content.replace(/\n/g, "\n" + continuationIndent);
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
    if (details.imageCount >= 3 && details.sourceTextLength < 500) warnings.push("image_heavy");
    return warnings;
  }

  function pendingCollapsedCount(root, doc) {
    if (!root || !root.querySelectorAll) return 0;
    const selector = "button[aria-expanded='false'][aria-controls],[role='button'][aria-expanded='false'][aria-controls],a[aria-expanded='false'][aria-controls]";
    const controls = (root.matches && root.matches(selector) ? [root] : [])
      .concat(Array.from(root.querySelectorAll(selector))).slice(0, 160);
    const targets = new Set();
    for (const control of controls) {
      if (isEffectivelyHidden(control, doc.defaultView) || control.closest("form,aside,nav,[disabled],[aria-disabled='true'],[role='navigation'],[role='complementary']")) continue;
      let noise = false, ancestor = control;
      while (ancestor && ancestor !== root.parentElement) {
        if (isNoiseElement(ancestor)) noise = true;
        if (ancestor.tagName === "DETAILS" && !ancestor.open) {
          const summary = Array.from(ancestor.children).find(function (node) { return node.tagName === "SUMMARY"; });
          if (!summary || !summary.contains(control)) noise = true;
        }
        ancestor = ancestor.parentElement;
      }
      if (noise) continue;
      const label = String(control.getAttribute("aria-label") || control.textContent || "").trim().replace(/\s+/g, " ");
      if (!/^(?:展开(?:全文|更多|正文)|显示更多|阅读全文|继续阅读|read more|show more|expand(?: all| content)?)\s*[»›>↓]{0,2}$/i.test(label)) continue;
      const scope = control.getRootNode();
      for (const id of String(control.getAttribute("aria-controls") || "").trim().split(/\s+/).slice(0, 8)) {
        const target = scope.getElementById && scope.getElementById(id);
        if (!target || !root.contains(target) || target.contains(control)) continue;
        if (isEffectivelyHidden(target, doc.defaultView)) targets.add(target);
      }
    }
    return targets.size;
  }

  function readabilityCandidate(doc, fallback) {
    const Reader = typeof globalThis !== "undefined" && globalThis.Readability;
    if (typeof Reader !== "function" || !fallback.element) return null;
    try {
      // Readability mutates its input. Work only on an inert, already cleaned
      // copy and serialize its returned DOM directly, never render its HTML.
      const copy = doc.implementation.createHTMLDocument(doc.title || "");
      const base = copy.createElement("base");
      base.href = doc.baseURI;
      copy.head.appendChild(base);
      copy.body.replaceWith(copy.importNode(fallback.cleaned.clone, true));
      const article = new Reader(copy, {
        maxElemsToParse: 30000, charThreshold: MIN_MEANINGFUL_CHARS,
        serializer: function (node) { return node; },
      }).parse();
      if (!article || !article.content) return null;
      const metrics = collectMetrics(article.content);
      if (metrics.textLength < MIN_MEANINGFUL_CHARS || linkDensity(metrics) > 0.45) return null;
      return Object.assign({}, fallback, {
        cleaned: Object.assign({}, fallback.cleaned, { clone: article.content }),
        metrics, fallback: false, strategy: "readability", expandedFrom: null,
      });
    } catch (_error) {
      return null;
    }
  }

  function extract(doc, options) {
    if (hasAccessBarrier(doc)) throw new Error("当前页面显示访问限制，请在网页中完成登录或验证、确认正文可见后再提取");
    options = options || {};
    const mode = options.mode || "article";
    if (!["article", "page", "selection", "region"].includes(mode)) throw new Error("不支持的剪藏方式");
    let selected;
    if (mode === "selection") {
      const selection = doc.defaultView && doc.defaultView.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount) throw new Error("请先在网页中选中文字，再提取");
      const container = doc.createElement("div");
      const totals = { clone: container, removedNodes: 0, hiddenRemovedNodes: 0, noiseRemovedNodes: 0, shadowRootCount: 0 };
      for (let index = 0; index < selection.rangeCount; index += 1) {
        const range = selection.getRangeAt(index);
        const ancestor = range.commonAncestorContainer.nodeType === 1
          ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
        if (ancestor && ancestor.closest("input,textarea,select")) {
          throw new Error("请选取网页正文，不能剪藏输入框内容");
        }
        if (!ancestor || isEffectivelyHidden(ancestor, doc.defaultView)) continue;
        // Clean the original selected subtree before detaching it, so CSS-only
        // hidden nodes keep their real visibility during select-all captures.
        const cleaned = cleanClone(ancestor, doc, { mode, range });
        container.appendChild(cleaned.clone);
        for (const key of ["removedNodes", "hiddenRemovedNodes", "noiseRemovedNodes", "shadowRootCount"]) totals[key] += cleaned[key];
      }
      selected = { element: container, cleaned: totals,
        candidateCount: 1, fallback: false, strategy: "selection" };
    } else if (mode === "page" || mode === "region") {
      const element = mode === "region" ? options.root : doc.body;
      if (!element || element.ownerDocument !== doc || !element.isConnected || isEffectivelyHidden(element, doc.defaultView)) {
        throw new Error("所选区域已失效，请重新选择");
      }
      selected = { element, cleaned: cleanClone(element, doc, { mode }),
        candidateCount: 1, fallback: false, strategy: mode };
    } else {
      selected = selectRoot(doc);
      if (selected.fallback) selected = readabilityCandidate(doc, selected) || selected;
      // Observed People.cn photo articles keep the visible H1 outside the
      // content container (and Readability can omit it). Keep that title while
      // retaining the generic body-selection/cleaning behavior.
      if (doc.querySelector("#picG")) {
        try {
          if (["bj.people.com.cn", "pic.people.com.cn", "lianghui.people.com.cn"].includes(new URL(doc.URL).hostname)) {
            const heading = Array.from(doc.querySelectorAll("h1")).find(function (node) {
              return !isEffectivelyHidden(node, doc.defaultView) && (node.textContent || "").trim();
            });
            if (heading && !selected.articleTitle) selected.articleTitle = heading.textContent.replace(/\s+/g, " ").trim().slice(0, 512);
            const photo = doc.querySelector("#picG");
            if (selected.element && !selected.element.contains(photo) && !isEffectivelyHidden(photo, doc.defaultView)) {
              const supplement = cleanClone(photo, doc);
              supplement.clone.querySelectorAll(".page_n,.about_pic").forEach(function (node) { node.remove(); });
              const combined = doc.createElement("div");
              combined.append(supplement.clone, selected.cleaned.clone);
              selected.cleaned.clone = combined;
              for (const field of ["removedNodes", "hiddenRemovedNodes", "noiseRemovedNodes", "shadowRootCount"]) selected.cleaned[field] += supplement[field];
              selected.sourceTextLength += (photo.textContent || "").length;
              selected.metrics = collectMetrics(combined);
            }
          }
        } catch (_error) {}
      }
    }
    if (selected.cleaned && !selected.metrics) {
      selected.metrics = collectMetrics(selected.cleaned.clone);
      selected.sourceTextLength = (selected.element.textContent || "").length;
    }
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
    const imageNodes = (cleaned.clone.matches && cleaned.clone.matches("img") ? [cleaned.clone] : [])
      .concat(Array.from(cleaned.clone.querySelectorAll("img")));
    const images = [];
    if (options.collectImages) {
      for (const image of imageNodes) {
        if (mode === "article" && image.getAttribute("data-web-rag-small-image") === "true") continue;
        const url = image.getAttribute("data-web-rag-image");
        if (url && !images.some(function (item) { return item.url === url; }) && images.length < 33) {
          images.push({ url, alt: String(image.getAttribute("alt") || "").slice(0, 160) });
        }
      }
    }
    const serializationContext = {
      linkCount: 0,
      preservedLinkCount: 0,
      inlineCodeCount: 0,
      emphasisCount: 0,
    };
    let text = normalizeStructuredText(mode === "region"
      ? serializeNode(cleaned.clone, serializationContext)
      : serializeChildren(cleaned.clone, serializationContext));
    if (selected.articleTitle) {
      const titleKey = selected.articleTitle.replace(/\s+/g, "");
      const hasTitle = text.split("\n").some(function (line) {
        return line.replace(/^#{1,6}\s+/, "").replace(/[\s*`]/g, "") === titleKey;
      });
      if (!hasTitle) text = "# " + selected.articleTitle + "\n\n" + text;
    }
    let truncated = false;
    if (text.length > MAX_EXTRACTED_CHARS) {
      text = text.slice(0, MAX_EXTRACTED_CHARS).trimEnd();
      truncated = true;
    }
    const qualityWarnings = buildQualityWarnings({
      fallback: selected.fallback,
      extractedCharCount: text.length,
      headingCount: (selected.metrics.headingCount || 0) + (selected.articleTitle && !cleaned.clone.querySelector("h1") ? 1 : 0),
      truncated,
      imageCount: cleaned.clone.querySelectorAll("img").length,
      sourceTextLength: selected.metrics.textLength || 0,
    });
    const closedDetails = (cleaned.clone.matches && cleaned.clone.matches("details:not([open])") ? [cleaned.clone] : [])
      .concat(Array.from(cleaned.clone.querySelectorAll("details:not([open])")));
    const includedCollapsedCount = closedDetails.filter(function (details) {
      const summary = Array.from(details.children).find(function (node) { return node.tagName === "SUMMARY"; });
      return Array.from(details.childNodes).some(function (node) {
        return node !== summary && ((node.textContent || "").trim() || (node.matches && node.matches("img")) || (node.querySelector && node.querySelector("img")));
      });
    }).length;
    const pendingCollapsed = mode === "selection" ? 0 : pendingCollapsedCount(selected.element, doc);
    if (includedCollapsedCount) qualityWarnings.push("collapsed_content_included");
    if (pendingCollapsed) qualityWarnings.push("collapsed_content_pending");
    if (options.collectImages && imageNodes.some(function (image) { return !image.getAttribute("data-web-rag-image"); })) qualityWarnings.push("ocr_unsupported_source");
    return {
      text,
      images,
      diagnostics: {
        extractor_version: VERSION,
        mode,
        site_profile: selected.siteProfile || null,
        strategy: selected.strategy || describeElement(selected.element),
        expanded_from: selected.expandedFrom,
        candidate_count: selected.candidateCount,
        source_char_count: selected.sourceTextLength || 0,
        cleaned_source_char_count: selected.metrics.textLength || 0,
        extracted_char_count: text.length,
        removed_node_count: cleaned.removedNodes,
        hidden_removed_node_count: cleaned.hiddenRemovedNodes,
        noise_removed_node_count: cleaned.noiseRemovedNodes,
        shadow_root_count: cleaned.shadowRootCount,
        heading_count: (selected.metrics.headingCount || 0) + (selected.articleTitle && !cleaned.clone.querySelector("h1") ? 1 : 0),
        paragraph_count: selected.metrics.paragraphCount || 0,
        list_item_count: selected.metrics.listItemCount || 0,
        code_block_count: selected.metrics.codeBlockCount || 0,
        inline_code_count: serializationContext.inlineCodeCount,
        emphasis_count: serializationContext.emphasisCount,
        table_count: selected.metrics.tableCount || 0,
        image_count: cleaned.clone.querySelectorAll("img").length,
        included_collapsed_count: includedCollapsedCount,
        pending_collapsed_count: pendingCollapsed,
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
    hasAccessBarrier,
    normalizeStructuredText,
    normalizeLinkDestination,
    scoreCandidateMetrics,
  };
});

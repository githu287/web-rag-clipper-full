// Conservative article-pagination metadata. No network or page mutations.
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagPagination = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const PAGE_KEYS = new Set(["page", "pageno", "page_no", "page_num", "pageindex", "pagenum", "pg"]);
  function key(value) {
    return globalThis.webRagUrlUtils ? webRagUrlUtils.normalizeWebUrl(value) : new URL(value).href;
  }
  function pathPage(path) {
    const match = path.match(/^(.*?)(?:\/page\/(\d+)\/?|[_-](\d+)(\.(?:html?|shtml|xhtml|php|aspx?))?)$/i);
    return match ? { family: match[1] + (match[4] || ""), index: Number(match[2] || match[3]) } : { family: path.replace(/\/$/, ""), index: 1 };
  }
  function nextURL(current, target) {
    try {
      const left = new URL(key(current));
      const right = new URL(key(new URL(target, current).href));
      if (!/^https?:$/.test(right.protocol) || left.origin !== right.origin || right.username || right.password ||
          right.href.length > 4096 || right.hash || left.hash || left.href === right.href) return null;
      // Keep every non-pagination query parameter, including document IDs.
      const query = function (url) {
        const pages = [], rest = [];
        for (const [name, value] of url.searchParams) {
          if (PAGE_KEYS.has(name.toLowerCase())) pages.push([name.toLowerCase(), value]);
          else rest.push([name, value]);
        }
        rest.sort(function (a, b) { return JSON.stringify(a).localeCompare(JSON.stringify(b)); });
        return { pages, rest: JSON.stringify(rest) };
      };
      const a = query(left), b = query(right);
      if (a.rest !== b.rest || a.pages.length > 1 || b.pages.length > 1) return null;
      if (left.pathname === right.pathname && b.pages.length === 1) {
        const before = a.pages[0] || [b.pages[0][0], "1"];
        const after = b.pages[0];
        if (before[0] === after[0] && /^\d{1,4}$/.test(before[1]) && /^\d{1,4}$/.test(after[1]) && Number(after[1]) === Number(before[1]) + 1) return right.href;
      }
      if (JSON.stringify(a.pages) !== JSON.stringify(b.pages)) return null;
      const p = pathPage(left.pathname), q = pathPage(right.pathname);
      // The first article filename may itself end in a numeric ID. Compare the
      // next page's base to the entire original path before interpreting IDs.
      if (q.index === 2 && q.family === left.pathname.replace(/\/$/, "")) return right.href;
      if (p.family === q.family && q.index === p.index + 1 && q.index <= 500) return right.href;
    } catch (_error) {}
    return null;
  }
  function titleKey(title) {
    return String(title || "").normalize("NFKC")
      .replace(/\(\s*\d+(?:\s*\/\s*\d+)?\s*\)|第\s*\d+\s*页/gi, "")
      .replace(/[\s\p{P}\p{S}]/gu, "").toLowerCase();
  }
  function sameArticle(first, next) {
    const a = first.pagination || {}, b = next.pagination || {};
    if (a.canonical && b.canonical && key(a.canonical) === key(b.canonical) &&
        (key(a.canonical) === key(first.url) || nextURL(a.canonical, first.url))) return true;
    const left = titleKey(a.heading || first.title), right = titleKey(b.heading || next.title);
    return left.length >= 4 && left === right;
  }
  function detect(doc) {
    const metadata = { next_url: null, status: "none", canonical: null, heading: "" };
    const visible = function (element) {
      if (element.tagName === "LINK") return true;
      let node = element;
      while (node && node.nodeType === 1) {
        if (globalThis.webRagPageExtractor && webRagPageExtractor.isElementHidden(node, doc.defaultView)) return false;
        node = node.parentElement;
      }
      const rect = element.getBoundingClientRect();
      return !!(rect.width && rect.height);
    };
    const canonical = doc.querySelector("link[rel~='canonical' i][href]");
    try {
      const url = new URL(canonical && canonical.getAttribute("href"), doc.baseURI);
      if (canonical && url.origin === new URL(doc.URL).origin) metadata.canonical = key(url.href);
    } catch (_error) {}
    const heading = Array.from(doc.querySelectorAll("article h1,main h1,h1")).find(visible);
    metadata.heading = heading ? heading.textContent.trim().slice(0, 512) : "";
    const strong = Array.from(doc.querySelectorAll("a[rel~='next' i],link[rel~='next' i],a[aria-label*='下一页'],a[aria-label*='next page' i]")).slice(0, 160);
    const links = Array.from(new Set(strong.concat(Array.from(doc.querySelectorAll("a[href]")).slice(0, 1000))));
    const targets = new Set();
    const excluded = new Set();
    let unsupported = false;
    for (const link of links.slice(0, 1160)) {
      if (!visible(link) || link.getAttribute("aria-disabled") === "true" || link.closest("[inert],.disabled,[aria-disabled='true']")) continue;
      if (link.closest("aside,[role='complementary'],[id*='comment' i],[class*='comment' i],.related,.recommend")) continue;
      const label = String(link.getAttribute("aria-label") || link.textContent || "").trim().replace(/\s+/g, " ");
      const rel = String(link.getAttribute("rel") || "").toLowerCase().split(/\s+/).includes("next");
      if (link.hasAttribute("download") || /下一篇|下一章|下一个文章|next\s+(?:post|article|chapter)/i.test(label)) {
        try { excluded.add(key(new URL(link.getAttribute("href"), doc.baseURI).href)); } catch (_error) {}
        continue;
      }
      if (!rel && !/^(?:下一页|下页|后页|next(?: page)?|[»›>→])\s*[»›>→]{0,3}$/i.test(label)) continue;
      if (!rel && /^(?:next|[»›>→])$/i.test(label) && !link.closest(".pagination,.pager,.pages,.page-links,.pagebar,#page,[aria-label*='分页'],[aria-label*='pagination' i]")) continue;
      const raw = link.getAttribute("href");
      if (!raw || /^\s*(?:#|javascript:)/i.test(raw)) { unsupported = true; continue; }
      let target;
      try { target = nextURL(doc.URL, new URL(raw, doc.baseURI).href); } catch (_error) {}
      if (target) targets.add(target); else unsupported = true;
    }
    for (const target of excluded) targets.delete(target);
    if (targets.size === 1 && !unsupported) { metadata.status = "ready"; metadata.next_url = Array.from(targets)[0]; }
    else if (targets.size > 1 || targets.size && unsupported) metadata.status = "ambiguous";
    else if (unsupported) metadata.status = "unsupported";
    return metadata;
  }
  return { key, nextURL, titleKey, sameArticle, detect };
});

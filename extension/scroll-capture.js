// Optional bounded scrolling. Scroll events can let the site load more content;
// the extension never activates links/buttons or performs its own fetches.
"use strict";
(function () {
  function samePage(url) { return webRagUrlUtils.normalizeWebUrl(document.URL) === webRagUrlUtils.normalizeWebUrl(url); }
  function check(signal, url) {
    if (signal.aborted) throw new Error("已取消提取");
    if (!samePage(url)) throw new Error("页面已变化，请重新提取");
  }

  function pause(ms, signal) {
    return new Promise(function (resolve, reject) {
      function abort() { clearTimeout(timer); signal.removeEventListener("abort", abort); reject(new Error("已取消提取")); }
      const timer = setTimeout(function () { signal.removeEventListener("abort", abort); resolve(); }, ms);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
    });
  }

  function selectScroller() {
    const page = document.scrollingElement || document.documentElement;
    const candidates = [];
    const nodes = document.querySelectorAll("main,[role='main'],article,[role='feed'],[role='list'],div,section");
    for (const element of Array.from(nodes).slice(0, 400)) {
      if (element === page || element.closest("nav,aside,form,[role='navigation']")) continue;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden" ||
          !/^(auto|scroll)$/.test(style.overflowY) || element.clientHeight < 150 ||
          rect.width < 200 || rect.bottom <= 0 || rect.top >= innerHeight ||
          element.scrollHeight - element.clientHeight <= 8 ||
          webRagPageExtractor.isNoiseName(`${element.id} ${element.className}`)) continue;
      candidates.push({ element, area: Math.min(rect.width, innerWidth) * Math.min(rect.height, innerHeight) });
    }
    candidates.sort(function (a, b) { return b.area - a.area; });
    const best = candidates[0];
    if (best && (page.scrollHeight - page.clientHeight < innerHeight || best.area >= innerWidth * innerHeight * 0.65)) {
      return { element: best.element, nested: true, multiple: candidates.some(function (candidate) {
        return candidate !== best && candidate.area >= best.area * 0.8 &&
          !best.element.contains(candidate.element) && !candidate.element.contains(best.element);
      }) };
    }
    return { element: page, nested: false, multiple: candidates.length > 0 };
  }

  async function run(options) {
    const signal = options.signal;
    const url = document.URL;
    const chosen = selectScroller();
    const scroller = chosen.element;
    const original = { left: scroller.scrollLeft, top: scroller.scrollTop };
    const hadInlineStyle = scroller.hasAttribute("style");
    const pageOriginal = { left: scrollX, top: scrollY };
    const styles = ["scroll-behavior", "scroll-snap-type"].map(function (name) {
      const saved = { name, value: scroller.style.getPropertyValue(name), priority: scroller.style.getPropertyPriority(name) };
      scroller.style.setProperty(name, name === "scroll-behavior" ? "auto" : "none", "important");
      return saved;
    });
    const requested = options.limits || {};
    const maxSteps = Math.min(20, Math.max(1, Number(requested.maxSteps) || 20));
    const maxMs = Math.min(25000, Math.max(500, Number(requested.maxMs) || 25000));
    const start = Date.now();
    const accumulator = webRagSnapshotMerge.create(webRagPageExtractor.MAX_EXTRACTED_CHARS);
    const warnings = new Set();
    if (chosen.multiple) warnings.add("scroll_other_containers");
    let latest;
    let merged;
    let reason = "step_limit";
    let steps = 0;
    let stableBottom = 0;
    let bottomSince = 0;
    let previousHeight = -1;
    let previousText = "";
    const framesBefore = Array.from(document.querySelectorAll("iframe,frame")).map(function (frame) { return frame.src; });
    function onKey(event) {
      if (event.key === "Escape") { event.preventDefault(); options.cancel(); }
    }
    document.addEventListener("keydown", onKey, true);
    function snapshot() {
      check(signal, url);
      latest = webRagPageExtractor.extract(document, { mode: options.mode });
      for (const warning of latest.diagnostics.quality_warnings || []) warnings.add(warning);
      merged = accumulator.add(latest.text);
      options.progress({ phase: "scroll", steps, max_steps: maxSteps, elapsed_ms: Date.now() - start,
        max_ms: maxMs, collected_chars: merged.text.length, target: chosen.nested ? "container" : "page" });
    }
    try {
      check(signal, url);
      scroller.scrollTo({ left: original.left, top: 0, behavior: "instant" });
      await pause(550, signal);
      snapshot();
      for (let turn = 1; turn <= maxSteps; turn += 1) {
        if (Date.now() - start >= maxMs) { reason = "time_limit"; break; }
        steps = turn;
        check(signal, url);
        const viewport = chosen.nested ? scroller.clientHeight : innerHeight;
        const beforeTop = scroller.scrollTop;
        const nextTop = Math.min(scroller.scrollHeight - scroller.clientHeight, beforeTop + Math.max(100, viewport * 0.7));
        scroller.scrollTo({ left: original.left, top: Math.max(0, nextTop), behavior: "instant" });
        await pause(Math.min(650, Math.max(0, maxMs - (Date.now() - start))), signal);
        snapshot();
        if (merged.truncated || latest.diagnostics.truncated) { reason = "size_limit"; break; }
        const height = scroller.scrollHeight;
        const atBottom = height - scroller.clientHeight - scroller.scrollTop <= 3;
        stableBottom = atBottom && height === previousHeight && latest.text === previousText ? stableBottom + 1 : 0;
        if (!atBottom || !stableBottom) bottomSince = Date.now();
        previousHeight = height;
        previousText = latest.text;
        if (stableBottom >= 2 && Date.now() - bottomSince >= 1800) { reason = "bottom_stable"; break; }
        if (nextTop > beforeTop + 5 && scroller.scrollTop <= beforeTop + 1 && !atBottom) { reason = "scroll_blocked"; break; }
      }
      if (reason !== "bottom_stable") warnings.add("scroll_incomplete");
      if (merged.uncertain_overlaps) warnings.add("scroll_overlap_uncertain");
      const framesAfter = Array.from(document.querySelectorAll("iframe,frame"));
      if (framesAfter.length > framesBefore.length || framesAfter.some(function (frame) { return !framesBefore.includes(frame.src); })) warnings.add("scroll_new_frames");
      if (merged.truncated || reason === "size_limit") warnings.add("truncated");
      return { text: merged.text, diagnostics: Object.assign({}, latest.diagnostics, {
        extracted_char_count: merged.text.length, quality_warnings: Array.from(warnings), truncated: merged.truncated || latest.diagnostics.truncated,
        scroll_enabled: true, scroll_steps: Math.min(steps, maxSteps), scroll_stop_reason: reason,
        scroll_snapshots: merged.snapshots, scroll_elapsed_ms: Date.now() - start,
        scroll_target: chosen.nested ? "container" : "page", scroll_uncertain_overlaps: merged.uncertain_overlaps,
      }) };
    } finally {
      document.removeEventListener("keydown", onKey, true);
      // Do not scroll a different SPA route to the old page's reading position.
      if (samePage(url) && scroller.isConnected) {
        scroller.scrollTo({ left: original.left, top: original.top, behavior: "instant" });
        if (chosen.nested) window.scrollTo({ left: pageOriginal.left, top: pageOriginal.top, behavior: "instant" });
      }
      for (const saved of styles) {
        if (saved.value) scroller.style.setProperty(saved.name, saved.value, saved.priority);
        else scroller.style.removeProperty(saved.name);
      }
      if (!hadInlineStyle && scroller.style.length === 0) scroller.removeAttribute("style");
    }
  }
  globalThis.webRagScrollCapture = { run };
})();

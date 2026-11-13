"use strict";

// Read the player's clock; never seek, play, select excerpts or reveal filtered rows.
const webRagVideoTranscriptPlayback = (() => {
  function create(player, list, status, locate, isCurrent) {
    let rows = [], marked = [];
    function message(text) { if (status.textContent !== text) status.textContent = text; }
    function clear() {
      for (const row of marked) row.element.classList.remove("is-current");
      marked = []; locate.disabled = true;
      message("选择同一原视频后，按播放位置高亮原文；定位按钮仅滚动原文列表。");
    }
    function update() {
      if (!isCurrent() || !Number.isFinite(player.currentTime)) { clear(); return; }
      const next = rows.filter(row => row.start <= player.currentTime && player.currentTime < row.end);
      const nextSet = new Set(next), previousSet = new Set(marked);
      for (const row of marked) if (!nextSet.has(row)) row.element.classList.remove("is-current");
      for (const row of next) if (!previousSet.has(row)) row.element.classList.add("is-current");
      marked = next;
      const visible = marked.filter(row => !row.element.hidden);
      locate.disabled = !visible.length;
      message(!marked.length ? "当前时间没有对应的转写原文。" :
        `当前：原文 ${marked.slice(0, 6).map(row => row.id).join("、")}${marked.length > 6 ? `等 ${marked.length} 条` : ""}。` +
        (visible.length < marked.length ? "部分或全部当前原文被搜索隐藏，可清除搜索查看。" : "可点击定位按钮查看，ASR时间可能有误差。"));
    }
    function setRows(items) {
      clear();
      rows = items.filter(row => Number.isFinite(row.start) && Number.isFinite(row.end) && row.start >= 0 && row.end > row.start);
      update();
    }
    for (const name of ["timeupdate", "seeking", "seeked", "ended", "loadedmetadata", "emptied", "error"]) player.addEventListener(name, update);
    locate.addEventListener("click", () => {
      update();
      const target = marked.find(row => !row.element.hidden);
      if (!target || locate.disabled) return;
      list.scrollTop += target.element.getBoundingClientRect().top - list.getBoundingClientRect().top;
      target.element.querySelector(".video-time-link").focus({ preventScroll: true });
    });
    clear();
    return { setRows, update, clear };
  }
  return { create };
})();

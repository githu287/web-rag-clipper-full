"use strict";

// One local, disposable subtitle track. No media upload or persistent storage.
const webRagVideoCaptions = (() => {
  function create(player, onState) {
    let active = null;
    function clear() {
      const old = active; active = null;
      if (old) {
        clearTimeout(old.timer);
        old.element.remove();
        URL.revokeObjectURL(old.url);
      }
      onState("off", 0);
    }
    function show(job, isCurrent) {
      clear();
      if (!isCurrent()) return;
      const result = webRagVideoSubtitles.render(job, { format: "vtt" });
      const element = document.createElement("track");
      element.kind = "subtitles"; element.label = "云端ASR转写"; element.srclang = "und";
      element.dataset.asr = "true";
      const state = { element, url: URL.createObjectURL(new Blob([result.content], { type: result.mime })), loaded: false };
      active = state;
      function fail() {
        if (active !== state) return;
        const visible = isCurrent(); clear();
        if (visible) onState("failed", 0);
      }
      element.onload = () => {
        if (active !== state) return;
        if (!isCurrent()) { clear(); return; }
        clearTimeout(state.timer);
        if (!element.track.cues || element.track.cues.length !== result.count) { fail(); return; }
        if (element.track.mode !== "showing") { clear(); return; }
        state.loaded = true;
        onState("shown", result.count);
      };
      element.onerror = fail;
      state.timer = setTimeout(fail, 5000);
      onState("loading", result.count);
      try {
        element.src = state.url;
        player.append(element);
        element.track.mode = "showing";
      } catch (_error) { fail(); }
    }
    player.textTracks.addEventListener("change", () => {
      // Keep the checkbox in sync if captions are switched off in native controls.
      if (active && active.loaded && active.element.track.mode !== "showing") clear();
    });
    return { show, clear };
  }
  return { create };
})();

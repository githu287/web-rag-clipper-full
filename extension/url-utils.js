// url-utils.js —— 与后端 normalize_web_url 对齐的查重辅助。
"use strict";

(() => {
  const TRACKING_QUERY_KEYS = new Set([
    "dclid",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "mkt_tok",
    "oly_anon_id",
    "oly_enc_id",
    "scm",
    "spm",
    "twclid",
    "vero_conv",
    "vero_id",
    "yclid",
    "_hsenc",
    "_hsmi",
  ]);

  // Python urllib.parse.urlencode / quote_plus 的编码规则。
  function quotePlus(value) {
    return encodeURIComponent(value)
      .replace(/[!'()*]/g, function (character) {
        return "%" + character.charCodeAt(0).toString(16).toUpperCase();
      })
      .replace(/%20/g, "+");
  }

  function normalizeWebUrl(value) {
    const url = new URL(String(value || "").trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error("web URL scheme must be http or https");
    }
    if (url.username || url.password) {
      throw new Error("web URL must not contain credentials");
    }
    url.hostname = url.hostname.replace(/\.$/, "").toLowerCase();

    const kept = [];
    for (const [key, itemValue] of url.searchParams.entries()) {
      const normalizedKey = key.toLowerCase();
      if (normalizedKey.startsWith("utm_") || TRACKING_QUERY_KEYS.has(normalizedKey)) {
        continue;
      }
      kept.push([key, itemValue]);
    }
    url.search = kept.length
      ? "?" + kept.map(function ([key, itemValue]) {
          return quotePlus(key) + "=" + quotePlus(itemValue);
        }).join("&")
      : "";
    url.hash = "";
    return url.toString();
  }

  globalThis.webRagUrlUtils = Object.freeze({ normalizeWebUrl });
})();

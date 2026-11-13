"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.webRagClipCompleteness = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const MAX = 500000;
  const stops = {
    bottom_stable: "到达当前底部并暂时稳定，仍可能有更慢的加载内容", step_limit: "达到本轮20次滚动上限", time_limit: "达到本轮读取时间上限",
    scroll_blocked: "网页未响应滚动，请检查弹层或其他滚动区域", size_limit: "达到50万字符上限", page_limit: "达到本轮分页上限，可继续读取后续页",
    total_page_limit: "达到单次预览30页总上限，可分区域另行剪藏", complete: "未再发现支持的下一页，不能据此证明全文完整",
    page_failed: "后续页读取失败或访问受限，可检查网页后重试", interrupted: "临时标签页被接管或连接中断，可手动重试",
    ambiguous: "检测到多个下一页，未自动选择", unsupported: "下一页不符合支持规则，请手动打开后剪藏",
    different_article: "后续页文章身份不一致，未合并", repeated_content: "后续页与已采集内容重复，已停止", loop: "检测到分页链接循环，已停止",
  };
  function actions(source, diagnostics) {
    const d = diagnostics || {};
    if (!source || d.truncated || String(source.raw_text || "").length >= MAX) return { pagination: false, scroll: false, ocr: false };
    return { pagination: !!source.pagination_resume,
      scroll: !!source.scroll_resume && !(d.pagination_page_count > 1),
      ocr: !!d.ocr_enabled && (d.ocr_images || []).some(image => ["failed", "pending"].includes(image.status)) };
  }
  function mergeDiagnostics(old, next) {
    const d = Object.assign({}, old, next);
    d.included_frame_count = Math.max(old.included_frame_count || 0, next.included_frame_count || 0);
    const images = new Map((old.ocr_images || []).map(image => [image.url, image]));
    for (const image of next.ocr_images || []) images.set(image.url, image);
    const warnings = new Set([...(old.quality_warnings || []), ...(next.quality_warnings || [])]);
    if (next.pagination_stop_reason === "complete") { warnings.delete("pagination_incomplete"); warnings.delete("pagination_none"); }
    if (next.scroll_stop_reason === "bottom_stable") warnings.delete("scroll_incomplete");
    if (images.size) {
      d.ocr_images = [...images.values()]; d.ocr_candidates = images.size;
      for (const [field, status] of [["ocr_recognized", "recognized"], ["ocr_empty", "empty"], ["ocr_failed", "failed"], ["ocr_pending", "pending"]]) d[field] = d.ocr_images.filter(image => image.status === status).length;
      if (!d.ocr_failed) warnings.delete("ocr_failed");
      if (!d.ocr_pending && !d.ocr_failed) for (const flag of ["ocr_image_limit", "ocr_byte_limit", "ocr_time_limit", "ocr_unavailable"]) warnings.delete(flag);
      if (d.ocr_recognized) warnings.delete("image_heavy");
      d.ocr_notices = [...new Set([...(old.ocr_notices || []), ...(next.ocr_notices || [])])];
    }
    d.quality_warnings = [...warnings];
    return d;
  }
  function describe(d) {
    const lines = [];
    lines.push(d.pagination_enabled ? `分页：已读取 ${d.pagination_page_count || 1} 页；${stops[d.pagination_stop_reason] || "请核对分页范围"}。` : "分页：本次未合并分页，仅提取当前页面所选范围。");
    lines.push(d.scroll_enabled ? `自动加载：累计 ${d.scroll_total_steps ?? d.scroll_steps ?? 0} 次滚动；${stops[d.scroll_stop_reason] || "请核对停止原因"}。` : "自动加载：本次未滚动加载，尚未出现的内容未采集。");
    if (d.scroll_enabled && d.pagination_page_count > 1) lines.push("自动加载仅作用于起始页。后续分页未自动滚动；起始页需要继续加载时，请关闭分页并重新提取。");
    if (d.ocr_enabled) lines.push(`图片识别：${d.ocr_recognized || 0} 张有文字、${d.ocr_empty || 0} 张无文字、${d.ocr_failed || 0} 张失败、${(d.ocr_images || []).filter(image => image.status === "pending").length} 张未处理；仅针对本次发现的图片候选。`);
    else lines.push("图片识别：本次未执行，图片中的文字可能未包含。");
    if ((d.quality_warnings || []).includes("collapsed_content_pending")) lines.push("仍有未展开的正文：在原网页手动展开后重新提取，重新提取会替换预览，请先保留编辑。");
    if ((d.quality_warnings || []).includes("frames_incomplete")) lines.push("部分内嵌页面未读取：可单独打开相应页面再剪藏。");
    if ((d.quality_warnings || []).includes("scroll_overlap_uncertain")) lines.push("部分滚动窗口无法可靠对齐，请重点核对重复或遗漏。");
    if (d.truncated) lines.push("正文已截断，请分区域另行剪藏；此预览不再自动续采。");
    if ((d.quality_warnings || []).includes("capture_image_limit")) lines.push("累计图片候选达到33张保存上限，后续图片未继续识别，可分区域另行剪藏。");
    return lines;
  }
  function append(original, addition) {
    const text = String(original || ""), extra = String(addition || "");
    const result = extra.trim() ? text + (text && !extra.startsWith("\n") ? "\n\n" : "") + extra : text;
    if (result.length > MAX) throw new Error("追加后超过50万字符，请缩短当前预览或新增内容后再确认；原预览未修改");
    return result;
  }
  return { actions, describe, mergeDiagnostics, append };
});

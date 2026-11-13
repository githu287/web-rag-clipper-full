"""Real extension continuation/review against local pages; OCR boundary is controlled, no user data or model calls."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/capture-continuation-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, errors, requests, ocr_calls = [], [], [], []
    passed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            parsed = urlsplit(self.path); query = parse_qs(parsed.query)
            requests.append(self.path)
            status = 200
            if parsed.path == "/image.png":
                content = (ROOT / "extension/tests/fixtures/ocr/bilingual.png").read_bytes(); mime = "image/png"
            else:
                page = int(query.get("page", ["1"])[0])
                if query.get("slow") and page > 5: time.sleep(2)
                if parsed.path == "/story":
                    slow = "&slow=1" if query.get("slow") else ""
                    link = f'<nav class="pagination"><a rel="next" href="/story?page={page+1}{slow}">下一页</a></nav>' if page < 12 else ""
                    body = f'<article><h1>续采测试文章</h1><p>PAGE_MARKER_{page:02d}。' + "这是允许连续读取的同一篇文章。数字80%与中文正文必须保持原样。" * 10 + "</p></article>" + link
                elif parsed.path == "/virtual":
                    body = '''<article><h1>虚拟长文章</h1><div id="rows" style="height:15000px;position:relative"></div></article>
<script>function render(){const start=Math.min(53,Math.floor(scrollY/200));rows.innerHTML=Array.from({length:7},(_,j)=>{const i=start+j;return '<p style="position:absolute;top:'+(i*200)+'px">VIRTUAL_ROW_'+String(i).padStart(2,'0')+'。'+('这是虚拟窗口正文，正常重复和否定词不能丢失。'.repeat(8))+'</p>'}).join('')}addEventListener('scroll',render);render()</script>'''
                    if query.get("mixed"):
                        if page == 1:
                            body += '<nav class="pagination"><a rel="next" href="/virtual?page=2&amp;mixed=1">下一页</a></nav>'
                        elif requests.count(self.path) == 1:
                            status = 403
                            body = '<h1>403 Forbidden</h1><p>Access denied</p>'
                        else:
                            body = '<article><h1>虚拟长文章</h1><p>MIXED_PAGE_02。' + '这是同一文章的第二页内容。' * 30 + '</p></article>'
                elif parsed.path == "/images":
                    body = '<article><h1>图文测试</h1><p>' + "正常正文，不将图片失败当作全文采集完成。" * 15 + '</p>' + ''.join(f'<img width="600" height="240" alt="示例图片{i}" src="/image.png?n={i}">' for i in range(1, 10)) + '</article>'
                elif parsed.path == "/static-long":
                    body = '<article style="min-height:16000px"><h1>已加载的长网页</h1><p>STATIC_ONCE。' + '全部正文已经在DOM中，滚动可能没有新增文字。' * 20 + '</p></article>'
                else:
                    body = '<article><h1>其他网页</h1><p>这是一个不同来源的页面。</p></article>'
                content = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>续采测试文章</title><style>body{margin:20px}article{max-width:650px}p{line-height:1.8}img{display:block}</style></head><body>' + body + '</body></html>').encode()
                mime = "text/html; charset=utf-8"
            self.send_response(status); self.send_header("Content-Type", mime); self.end_headers()
            try: self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_port)
    def done(text): checks.append(text); print(text, flush=True)

    try:
        with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True, viewport={"width": 820, "height": 900},
                ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"])
            def api(route):
                if urlsplit(route.request.url).path == "/ocr/image":
                    ocr_calls.append(len(ocr_calls) + 1)
                    if len(ocr_calls) == 1: route.fulfill(status=500, json={"detail": "fixture image failure"})
                    else: route.fulfill(json={"text": "OCR_RESULT_" + str(len(ocr_calls)), "warnings": []})
                else: route.abort()
            context.route("http://localhost:18001/**", api)
            try:
                eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                source = context.new_page(); panel = context.new_page()
                panel.set_viewport_size({"width": 420, "height": 1000})
                panel.on("pageerror", lambda error: errors.append(str(error)))
                panel.goto(f"chrome-extension://{eid}/sidepanel.html"); panel.wait_for_load_state("networkidle")
                panel.evaluate("""() => {
                  webRagApiClient.setPluginDetails({pluginId:'capture-fixture',pluginSecret:'capture-secret',pluginName:'剪藏测试'});
                  globalThis.originalCapture=webRagPageCapture.capture;
                  webRagPageCapture.capture=(tab,options)=>originalCapture(tab,{...options,scrollLimits:{maxSteps:4}});
                }""")
                def navigate(path):
                    source.goto(base + path); source.wait_for_load_state("networkidle")
                    tab = panel.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                    panel.evaluate("""data=>{clearClipDraft();currentTabId=data.tab;binding={pluginId:'capture-fixture',pageUrl:data.url,pageTitle:'原网页标题',mode:'current',stale:false};renderAppView();switchView('clip')}""", {"tab": tab, "url": source.url})
                    panel.locator("#clip-mode").select_option("article")
                    for selector in ("#clip-pagination", "#clip-auto-scroll", "#clip-image-ocr"): panel.locator(selector).uncheck()
                    return tab
                def settled(): panel.wait_for_function("()=>!clipBusy", timeout=45000)
                def extract(): panel.locator("#clip-btn").click(); panel.wait_for_function("()=>!clipBusy && clipDraft!==null", timeout=45000)
                def continuation(selector): panel.locator(selector).click(); settled(); expect(panel.locator("#clip-continuation-pending")).to_be_visible()

                tab = navigate("/story?page=1")
                panel.locator("#clip-pagination").check(); extract()
                expect(panel.locator("#clip-completeness-list")).to_contain_text("已读取 5 页")
                expect(panel.locator("#clip-continue-pages")).to_be_visible()
                original = panel.locator("#clip-preview-text").input_value() + "\n\nKEEP_EDITED_80%"
                panel.locator("#clip-preview-title").fill("用户编辑的标题")
                panel.locator("#clip-preview-text").fill(original)
                assert panel.evaluate("!clipCaptureSession.source.raw_text.includes('KEEP_EDITED_80%') && clipCaptureSession.source.title!=='用户编辑的标题'")
                start = len(requests)
                continuation("#clip-continue-pages")
                expect(panel.locator("#clip-preview-text")).to_have_value(original)
                expect(panel.locator("#clip-preview-save-btn")).to_be_disabled()
                delta = panel.locator("#clip-continuation-text").input_value()
                assert "PAGE_MARKER_06" in delta and "PAGE_MARKER_10" in delta and "PAGE_MARKER_05" not in delta
                expect(panel.locator("#clip-continuation-summary")).to_contain_text("确认后分页：已读取 10 页")
                assert not any(int(parse_qs(urlsplit(path).query).get("page", ["0"])[0]) in range(1, 6) for path in requests[start:])
                original += "\nKEEP_EDIT_AFTER_FETCH"
                panel.locator("#clip-preview-text").fill(original)
                panel.locator("#clip-continuation-text").fill(delta + "\nREVIEWED_NEW_BATCH")
                panel.screenshot(path=str(args.artifacts / "pending-review.png"), full_page=True)
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-continuation-pending")).to_be_hidden()
                text = panel.locator("#clip-preview-text").input_value()
                assert text.startswith(original) and all(text.count(f"PAGE_MARKER_{i:02d}") == 1 for i in range(1, 11))
                assert "REVIEWED_NEW_BATCH" in text
                expect(panel.locator("#clip-preview-title")).to_have_value("用户编辑的标题")
                expect(panel.locator("#clip-source-pages li")).to_have_count(10)
                done("Pagination resumes at page6, stages page6-10, preserves edited title/body and only appends on explicit confirmation with no repeated page markers")

                continuation("#clip-continue-pages")
                tail = panel.locator("#clip-continuation-text").input_value()
                panel.locator("#clip-continuation-discard").click()
                expect(panel.locator("#clip-preview-text")).to_have_value(text)
                expect(panel.locator("#clip-source-pages li")).to_have_count(10)
                continuation("#clip-continue-pages")
                assert panel.locator("#clip-continuation-text").input_value() == tail
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-source-pages li")).to_have_count(12)
                expect(panel.locator("#clip-continue-pages")).to_be_hidden()
                expect(panel.locator("#clip-completeness-list")).to_contain_text("不能据此证明全文完整")
                done("Discard retains accepted page10 cursor; retry stages the same page11-12, then final confirmation clears continuation without claiming full coverage")

                navigate("/story?page=1&slow=1"); panel.locator("#clip-pagination").check(); extract()
                old = panel.locator("#clip-preview-text").input_value()
                panel.locator("#clip-continue-pages").click()
                panel.wait_for_function("()=>clipScrollProgress && clipScrollProgress.phase==='pagination'")
                panel.locator("#clip-capture-cancel").click(); settled()
                expect(panel.locator("#clip-preview-text")).to_have_value(old)
                expect(panel.locator("#clip-continuation-pending")).to_be_hidden()
                panel.wait_for_function("()=>chrome.storage.session.get('webRagPaginationTabs').then(x=>!(x.webRagPaginationTabs||[]).length)")
                done("Canceling a pending slow page leaves the edited preview and accepted cursor intact and releases owned background tabs")

                navigate("/virtual"); source.evaluate("scrollTo(0,100)"); source.wait_for_timeout(100)
                panel.locator("#clip-auto-scroll").check(); extract()
                old = panel.locator("#clip-preview-text").input_value()
                assert "VIRTUAL_ROW_00" in old and "VIRTUAL_ROW_59" not in old
                expect(panel.locator("#clip-continue-scroll")).to_be_visible()
                assert abs(source.evaluate("scrollY") - 100) < 3
                continuation("#clip-continue-scroll")
                delta = panel.locator("#clip-continuation-text").input_value()
                assert delta.strip() and "VIRTUAL_ROW_00" not in delta
                expect(panel.locator("#clip-preview-text")).to_have_value(old)
                panel.locator("#clip-continuation-apply").click()
                combined = panel.locator("#clip-preview-text").input_value()
                import re
                markers = re.findall(r"VIRTUAL_ROW_(\d+)", combined)
                assert markers == sorted(set(markers)) and len(markers) > len(re.findall(r"VIRTUAL_ROW_(\d+)", old)), markers
                assert abs(source.evaluate("scrollY") - 100) < 3
                done("Serialized virtual-window checkpoint resumes beyond the old position, preserves overlap order without repeated markers and restores the user's reading position")

                continuation("#clip-continue-scroll")
                panel.locator("#clip-continuation-discard").click()
                expect(panel.locator("#clip-preview-text")).to_have_value(combined)
                panel.locator("#clip-continue-scroll").click()
                panel.wait_for_function("()=>clipScrollProgress && clipScrollProgress.steps>=1")
                panel.locator("#clip-capture-cancel").click(); settled()
                expect(panel.locator("#clip-preview-text")).to_have_value(combined)
                assert abs(source.evaluate("scrollY") - 100) < 3
                done("Discarded or canceled rolling continuations cannot advance the accepted checkpoint or overwrite the preview")
                source.reload(); source.wait_for_load_state("networkidle")
                panel.locator("#clip-continue-scroll").click(); settled()
                expect(panel.locator("#clip-preview-text")).to_have_value(combined)
                expect(panel.locator("#clip-continuation-pending")).to_be_hidden()
                expect(panel.locator("#clip-status")).to_contain_text("滚动区域或续采位置已变化")
                done("Reloading the source invalidates its DOM-bound scroll cursor and preserves the original preview instead of guessing a resume position")

                navigate("/static-long"); panel.locator("#clip-auto-scroll").check(); extract()
                old = panel.locator("#clip-preview-text").input_value() + "\nKEEP_STATIC_EDIT"
                panel.locator("#clip-preview-text").fill(old)
                continuation("#clip-continue-scroll")
                expect(panel.locator("#clip-continuation-text")).to_have_value("")
                expect(panel.locator("#clip-continuation-summary")).to_contain_text("本轮没有新增文字")
                expect(panel.locator("#clip-continuation-summary")).to_contain_text("累计 8 次滚动")
                expect(panel.locator("#clip-continuation-apply")).to_have_text("确认更新采集进度")
                panel.locator("#clip-continuation-text").fill("用户补充的内容")
                expect(panel.locator("#clip-continuation-summary")).to_contain_text("待追加")
                expect(panel.locator("#clip-continuation-apply")).to_have_text("确认追加到当前预览")
                panel.locator("#clip-continuation-text").fill("")
                expect(panel.locator("#clip-continuation-apply")).to_have_text("确认更新采集进度")
                panel.screenshot(path=str(args.artifacts / "empty-continuation-review.png"), full_page=True)
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-preview-text")).to_have_value(old)
                assert panel.evaluate("clipCaptureSession.report.scroll_total_steps") == 8
                expect(panel.locator("#clip-continuation-summary")).to_be_empty()
                done("Empty continuation clearly previews pending progress; edited additions update the action, and accepting empty text advances only the checkpoint without changing the edited body")

                navigate("/virtual?page=1&mixed=1")
                panel.locator("#clip-auto-scroll").check(); panel.locator("#clip-pagination").check(); extract()
                expect(panel.locator("#clip-continue-scroll")).to_be_visible()
                expect(panel.locator("#clip-continue-pages")).to_be_visible()
                assert panel.evaluate("clipDraft.diagnostics.pagination_stop_reason") == "page_failed"
                old = panel.locator("#clip-preview-text").input_value()
                continuation("#clip-continue-scroll")
                delta = panel.locator("#clip-continuation-text").input_value()
                assert "VIRTUAL_ROW_" in delta and "MIXED_PAGE_02" in delta
                expect(panel.locator("#clip-preview-text")).to_have_value(old)
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-completeness-list")).to_contain_text("已读取 2 页")
                expect(panel.locator("#clip-continue-scroll")).to_be_hidden()
                expect(panel.locator("#clip-completeness-list")).to_contain_text("起始页需要继续加载")
                assert panel.locator("#clip-preview-text").input_value().count("MIXED_PAGE_02") == 1
                done("Combined settings retain next-page handling after rolling continuation; a previously failed page is retried and appended after the new scroll text with a clear multi-page rolling limitation")

                navigate("/images"); panel.locator("#clip-image-ocr").check(); extract()
                expect(panel.locator("#clip-source-images li")).to_have_count(9)
                assert len(ocr_calls) == 8
                expect(panel.locator("#clip-completeness-list")).to_contain_text("1 张失败、1 张未处理")
                old = panel.locator("#clip-preview-text").input_value()
                continuation("#clip-retry-images")
                assert len(ocr_calls) == 10
                expect(panel.locator("#clip-preview-text")).to_have_value(old)
                delta = panel.locator("#clip-continuation-text").input_value()
                assert "OCR_RESULT_9" in delta and "OCR_RESULT_10" in delta and "OCR_RESULT_2" not in delta
                expect(panel.locator("#clip-continuation-summary")).to_contain_text("9 张有文字")
                assert "图片文字识别 9" in delta and "图片文字识别 1" in delta
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-retry-images")).to_be_hidden()
                expect(panel.locator("#clip-completeness-list")).to_contain_text("9 张有文字")
                assert panel.locator("#clip-preview-text").input_value().startswith(old)
                done("OCR report distinguishes success/failure/unprocessed images; retry only handles the missing two, keeps global image numbering and appends text after review (controlled OCR boundary)")

                panel.screenshot(path=str(args.artifacts / "completeness.png"), full_page=True)
                navigate("/story?page=1"); panel.locator("#clip-pagination").check(); extract()
                continuation("#clip-continue-pages")
                old = panel.locator("#clip-preview-text").input_value()
                source.evaluate("history.pushState({},'', '/different-route')")
                panel.locator("#clip-continuation-apply").click()
                panel.wait_for_timeout(200)
                assert panel.evaluate("clipDraft===null || !clipDraft.raw_text.includes('PAGE_MARKER_06')")
                done("A route change before confirmation rejects the pending batch instead of attaching it to another page")
                navigate("/story?page=1"); panel.locator("#clip-pagination").check(); extract()
                continuation("#clip-continue-pages")
                panel.evaluate("webRagApiClient.setPluginDetails({pluginId:'other-capture',pluginSecret:'other-secret'})")
                panel.locator("#clip-continuation-apply").click()
                expect(panel.locator("#clip-preview")).to_be_hidden()
                assert panel.evaluate("clipDraft===null && clipCaptureSession===null && clipContinuationPending===null")
                expect(panel.locator("#clip-source-pages")).to_be_empty()
                expect(panel.locator("#clip-continuation-text")).to_have_value("")
                done("Workspace changes reject pending merge and clear the original preview, private cursors, source links and new text")
                assert errors == [], errors
                passed = True
            finally: context.close()
    finally:
        server.shutdown(); server.server_close()
        (args.artifacts / "report.json").write_text(json.dumps({"passed": passed, "checks": checks, "errors": errors, "ocr_boundary": "fixture", "backend": "not used", "cloud_calls": 0}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__": main()

"""Exercise the real unpacked extension in headless Chrome, without a backend.

Requires Python Playwright and an installed Chrome. All profiles, screenshots
and reports are written under .venv/runtime/capture-browser by default.
Run: .venv/Scripts/python.exe scripts/check_capture_browser.py
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PARAGRAPH = "这是一段用于浏览器剪藏验证的正文，包含足够多的文字和明确的标点。" * 12


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/capture-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report: dict = {"checks": [], "backend_required": False, "native_sidepanel_container": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assets = {
                "/extension/tests/extractor.browser.test.html": "text/html; charset=utf-8",
                "/extension/extractor.js": "application/javascript",
                "/extension/vendor/readability/Readability.js": "application/javascript",
            }
            if self.path in assets:
                data = (ROOT / self.path.lstrip("/")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", assets[self.path])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/frames":
                body = f'''<h1>主页面文章</h1><article><p>{PARAGRAPH}</p>
                  <iframe title="跨域内嵌正文" src="http://localhost:{secondary.server_port}/child" style="width:600px;height:300px"></iframe>
                  <iframe src="/hidden" style="display:none"></iframe></article>'''
            elif self.path == "/child":
                body = f'<article><h1>跨域内嵌正文</h1><p id="child-text">内嵌唯一标记FRAME527。{PARAGRAPH}</p></article>'
            elif self.path == "/hidden":
                body = f'<article><p>隐藏页面禁止混入HIDDEN915。{PARAGRAPH}</p></article>'
            elif self.path == "/dynamic":
                body = f'''<main>正在加载</main><script>setTimeout(() => {{document.querySelector('main').innerHTML = '<article><h1>延迟正文</h1><p>动态唯一标记DYNAMIC742。{PARAGRAPH}</p></article>';}}, 1600);</script>'''
            else:
                body = f'''<nav>整页导航唯一标记NAV123</nav><article><h1>测试文章</h1>
                <p id="first">正文唯一标记BODY318。{PARAGRAPH}</p>
                <p id="pick"><a id="pick-link" href="/should-not-navigate">区域唯一标记REGION819</a></p>
                <p id="outside">区域外标记OUTSIDE。</p></article>
                <aside class="related-posts">整页推荐标记RELATED456</aside>
                <textarea>输入框私密标记PRIVATE999</textarea><p style="display:none">隐藏唯一标记HIDDEN123</p>'''
            data = ('<!doctype html><meta charset="utf-8"><title>剪藏测试</title>' + body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    secondary = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    primary = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    for server in (primary, secondary):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{primary.server_port}"

    def done(text):
        report["checks"].append(text)
        print(text, flush=True)

    def wait_js(page, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.evaluate(expression):
                return
            time.sleep(0.1)
        raise TimeoutError("Capture did not settle: " + expression)

    try:
        with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                viewport={"width": 760, "height": 900}, ignore_default_args=["--disable-extensions"],
                args=["--enable-unsafe-extension-debugging"])
            try:
                cdp = context.browser.new_browser_cdp_session()
                extension_id = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                driver = context.new_page()
                driver.goto(f"chrome-extension://{extension_id}/tests/capture-driver.html")
                driver.wait_for_load_state("networkidle")
                source = context.new_page()

                def navigate(path):
                    source.goto(base + path)
                    source.wait_for_load_state("networkidle")
                    return driver.evaluate("""async url => (await chrome.tabs.query({})).find(t => t.url === url).id""", source.url)

                def capture(tab_id, mode):
                    return driver.evaluate("""async data => webRagPageCapture.capture(data.id, {mode: data.mode})""", {"id": tab_id, "mode": mode})

                def begin(tab_id, mode):
                    driver.evaluate("""data => {
                      globalThis.captureResult = null; globalThis.captureError = null;
                      globalThis.captureAbort = new AbortController();
                      webRagPageCapture.capture(data.id, {mode:data.mode, signal:captureAbort.signal})
                        .then(r => {globalThis.captureResult=r;}, e => {globalThis.captureError=e.message;});
                    }""", {"id": tab_id, "mode": mode})

                regression = context.new_page()
                # The fixture contains an inline runner; serve it as an ordinary
                # page so the extension's strict MV3 CSP stays unchanged.
                regression.goto(base + "/extension/tests/extractor.browser.test.html")
                assert regression.locator("#test-result").inner_text() == "EXTRACTOR_BROWSER_TESTS_PASSED", regression.locator("#test-result").inner_text()
                regression.close()
                done("DOM regressions: hidden nodes, formatting, Shadow DOM slots, Readability, selection and region")

                tab = navigate("/article")
                # Inspect the rendered fixture before interacting with it.
                assert source.locator("#pick-link").inner_text() == "区域唯一标记REGION819"
                article = capture(tab, "article")
                assert "BODY318" in article["raw_text"] and "NAV123" not in article["raw_text"]
                whole = capture(tab, "page")
                assert "NAV123" in whole["raw_text"] and "RELATED456" in whole["raw_text"]
                assert "PRIVATE999" not in whole["raw_text"] and "HIDDEN123" not in whole["raw_text"]
                done("Article/page modes differ correctly; hidden nodes and form values excluded")

                source.evaluate("""() => {const range=document.createRange(); range.selectNodeContents(document.querySelector('#pick')); const s=getSelection();s.removeAllRanges();s.addRange(range);}""")
                selected = capture(tab, "selection")
                assert "REGION819" in selected["raw_text"] and "BODY318" not in selected["raw_text"]
                source.evaluate("getSelection().removeAllRanges()")
                begin(tab, "selection")
                wait_js(driver, "captureError !== null")
                assert "选取" in driver.evaluate("captureError")
                done("Selection captures exactly selected text; empty selection reports a clear error")

                begin(tab, "region")
                source.locator("[data-web-rag-picker]").wait_for(state="attached")
                source.locator("#pick-link").hover()
                source.screenshot(path=str(args.artifacts / "region-picker.png"))
                source.locator("#pick-link").click()
                wait_js(driver, "captureResult !== null")
                region = driver.evaluate("captureResult")
                assert "REGION819" in region["raw_text"] and "BODY318" not in region["raw_text"]
                assert source.url == base + "/article", "picking a link must not navigate"
                assert source.locator("[data-web-rag-picker]").count() == 0
                done("Region picker highlights, captures one region, removes itself and prevents link activation")

                tab = navigate("/frames")
                framed = capture(tab, "article")
                assert "FRAME527" in framed["raw_text"] and "HIDDEN915" not in framed["raw_text"]
                assert framed["diagnostics"]["included_frame_count"] == 1
                child = source.frame(url=f"http://localhost:{secondary.server_port}/child")
                child.evaluate("""() => {const r=document.createRange();r.selectNodeContents(document.querySelector('#child-text'));getSelection().removeAllRanges();getSelection().addRange(r);}""")
                selected_frame = capture(tab, "selection")
                assert "FRAME527" in selected_frame["raw_text"] and selected_frame["url"] == base + "/frames"
                done("Cross-origin iframe text and selection captured with the top-level page URL; hidden iframe excluded")

                begin(tab, "region")
                source.locator("[data-web-rag-picker]").wait_for(state="attached")
                source.locator("h1").click(position={"x": 2, "y": 2}, trial=True)
                source.keyboard.press("Escape")
                wait_js(driver, "captureError !== null", timeout=5)
                assert "取消" in driver.evaluate("captureError")
                assert source.locator("[data-web-rag-picker]").count() == 0
                assert child.locator("[data-web-rag-picker]").count() == 0
                done("Escape cancels all region pickers across frames promptly")

                begin(tab, "region")
                source.locator("[data-web-rag-picker]").wait_for(state="attached")
                driver.evaluate("captureAbort.abort()")
                wait_js(driver, "captureError !== null", timeout=5)
                assert source.locator("[data-web-rag-picker]").count() == 0
                done("Extension cancel signal clears page overlays and pending requests")

                tab = navigate("/dynamic")
                dynamic = capture(tab, "article")
                assert "DYNAMIC742" in dynamic["raw_text"]
                done("Delayed SPA content is awaited and captured")

                tab = navigate("/article")
                begin(tab, "article")
                source.evaluate("history.pushState({}, '', '/changed')")
                wait_js(driver, "captureError !== null")
                assert "页面已变化" in driver.evaluate("captureError")
                done("Navigation during extraction rejects stale content")

                # Original fixtures preserve observed site DOM patterns. Route
                # only these exact synthetic URLs; no live site traffic in CI.
                fixtures = json.loads((ROOT / "extension/tests/site-fixtures.json").read_text(encoding="utf-8"))["cases"]
                # Deny every other request during synthetic-site checks,
                # including unexpected assets or redirects to a live origin.
                source.route("**/*", lambda route: route.abort())
                for case in fixtures:
                    fixture_html = "<!doctype html><meta charset='utf-8'><title>Local site-pattern fixture</title>" + case["html"].replace("{{padding}}", "<p>" + PARAGRAPH + "</p>")
                    def fixture_route(route, _request, data=fixture_html):
                        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=data)
                    source.route(case["url"], fixture_route)
                    source.goto(case["url"])
                    source.wait_for_load_state("networkidle")
                    tab = driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                    begin(tab, "article")
                    wait_js(driver, "captureResult !== null || captureError !== null")
                    if case.get("error"):
                        assert case["error"] in (driver.evaluate("captureError") or ""), case["id"]
                    else:
                        result = driver.evaluate("captureResult")
                        assert result, (case["id"], driver.evaluate("captureError"))
                        for expected in case.get("contains", []):
                            assert expected in result["raw_text"], (case["id"], "missing", expected)
                        for excluded in case.get("excludes", []):
                            assert excluded not in result["raw_text"], (case["id"], "noise", excluded)
                        if case.get("profile"):
                            assert result["diagnostics"]["site_profile"] == case["profile"], case["id"]
                        if case.get("warning"):
                            assert case["warning"] in result["diagnostics"]["quality_warnings"], case["id"]
                    source.unroute(case["url"], fixture_route)
                done(f"Site-pattern regressions: {len(fixtures)} original fixtures passed without live site requests")
                report["passed"] = True
            finally:
                context.close()
    finally:
        for server in (primary, secondary):
            server.shutdown()
            server.server_close()
        (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

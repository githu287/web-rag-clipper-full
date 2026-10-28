"""Real extension regression for opt-in bounded scrolling; local fixtures only.

Requires installed Chrome and Python Playwright. No backend, workspace or model
credentials. Reports default to ignored .venv/runtime/scroll-browser/.
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

LAZY = """<style>p {height:160px;margin:0 0 10px}</style>
<article><h1>延迟加载测试</h1><div id="rows"></div></article>
<script>
let count=0,pending=false; const total=location.pathname==='/infinite'?1000:18;
function append(n){for(let j=0;j<n&&count<total;j++,count++){
 const p=document.createElement('p');p.textContent='LAZY_'+String(count).padStart(3,'0')+'。这是页面滚动后出现的独立正文，用于验证追加加载、重复消除以及顺序保持。'.repeat(3);rows.append(p);
}}
append(8);addEventListener('scroll',()=>{
 if(!pending&&count<total&&innerHeight+scrollY>=document.documentElement.scrollHeight-80){pending=true;setTimeout(()=>{append(4);pending=false;},350);}
});</script>"""

VIRTUAL = """<style>html,body{margin:0}#viewport{height:480px;overflow-y:auto;scroll-behavior:smooth!important;scroll-snap-type:y mandatory}
#spacer{position:relative;height:1920px}#rows{position:absolute;margin:0;padding:0;list-style:none}li{height:120px;margin:0}</style>
<article><h1>虚拟列表测试</h1><div id="viewport" role="main"><div id="spacer"><ul id="rows"></ul></div></div></article>
<script>
const viewport=document.getElementById('viewport'),rows=document.getElementById('rows');
function render(){const first=Math.floor(viewport.scrollTop/120);rows.style.top=(first*120)+'px';rows.replaceChildren();
for(let i=first;i<Math.min(16,first+6);i++){const li=document.createElement('li');li.textContent='VIRT_'+String(i).padStart(3,'0')+'。虚拟列表只保留当前窗口，旧节点会被回收，提取时必须保留已经读取的内容。';rows.append(li);}}
viewport.addEventListener('scroll',render);render();</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/scroll-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"checks": [], "backend_required": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = VIRTUAL if self.path == "/virtual" else LAZY
            if self.path == "/frame":
                body = '<article><h1>框架页面</h1><p>可见框架内容。</p></article>'
            if self.path == "/with-frame":
                body += '<iframe src="/frame" style="width:500px;height:200px"></iframe>'
            data = ('<!doctype html><meta charset="utf-8"><title>滚动采集测试</title>' + body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def done(text):
        report["checks"].append(text)
        print(text, flush=True)

    def wait_js(page, expression, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.evaluate(expression):
                return
            time.sleep(0.1)
        raise TimeoutError(expression)

    try:
        with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                viewport={"width": 760, "height": 900}, ignore_default_args=["--disable-extensions"],
                args=["--enable-unsafe-extension-debugging"])
            try:
                cdp = context.browser.new_browser_cdp_session()
                extension = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                driver = context.new_page()
                driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
                driver.wait_for_load_state("networkidle")
                source = context.new_page()

                def navigate(path):
                    source.goto(base + path)
                    source.wait_for_load_state("networkidle")
                    return driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)

                def begin(tab, **options):
                    driver.evaluate("""data=>{
                      globalThis.captureResult=null;globalThis.captureError=null;globalThis.captureProgress=[];
                      globalThis.captureAbort=new AbortController();
                      webRagPageCapture.capture(data.tab,{...data.options,signal:captureAbort.signal,onProgress:p=>captureProgress.push(p)})
                        .then(r=>{captureResult=r;},e=>{captureError=e.message;});
                    }""", {"tab": tab, "options": {"mode": "article", **options}})

                def result():
                    wait_js(driver, "captureResult !== null || captureError !== null")
                    value = driver.evaluate("captureResult")
                    assert value, driver.evaluate("captureError")
                    return value

                tab = navigate("/lazy")
                assert source.locator("#rows p").count() == 8
                begin(tab)
                normal = result()
                assert "LAZY_000" in normal["raw_text"] and "LAZY_017" not in normal["raw_text"]
                assert source.evaluate("scrollY") == 0
                done("Default capture does not scroll or trigger remaining lazy batches")

                source.evaluate("scrollTo({top:150,behavior:'instant'})")
                begin(tab, autoScroll=True)
                loaded = result()
                for i in range(18):
                    marker = f"LAZY_{i:03}"
                    assert loaded["raw_text"].count(marker) == 1, marker
                assert abs(source.evaluate("scrollY") - 150) <= 3
                assert loaded["diagnostics"]["scroll_stop_reason"] == "bottom_stable"
                assert driver.evaluate("captureProgress.length") > 2
                assert loaded["raw_text"].index("LAZY_000") < loaded["raw_text"].index("LAZY_017")
                done("Opt-in scrolling loads all finite batches in order, deduplicates and restores page position")

                tab = navigate("/virtual")
                source.locator("#viewport").evaluate("el=>el.scrollTo({top:240,behavior:'instant'})")
                source.wait_for_timeout(100)
                assert source.locator("#rows li").count() == 6
                before_style = source.locator("#viewport").get_attribute("style")
                begin(tab, autoScroll=True)
                virtual = result()
                for i in range(16):
                    marker = f"VIRT_{i:03}"
                    assert virtual["raw_text"].count(marker) == 1, (marker, virtual["raw_text"])
                assert virtual["diagnostics"]["scroll_target"] == "container"
                assert abs(source.locator("#viewport").evaluate("el=>el.scrollTop") - 240) <= 3
                assert source.locator("#viewport").get_attribute("style") == before_style
                source.screenshot(path=str(args.artifacts / "virtual-restored.png"))
                done("Nested virtual list retains recycled rows once, restores its scroll position and styles")

                source.locator("#viewport").evaluate("""el=>{el.style.setProperty('scroll-behavior','smooth','important');el.style.setProperty('scroll-snap-type','y mandatory','important');}""")
                styled_before = source.locator("#viewport").get_attribute("style")
                begin(tab, autoScroll=True, scrollLimits={"maxSteps": 1})
                result()
                assert source.locator("#viewport").get_attribute("style") == styled_before
                assert abs(source.locator("#viewport").evaluate("el=>el.scrollTop") - 240) <= 3
                done("Existing inline scroll/snap values and important priorities are restored")

                tab = navigate("/infinite")
                begin(tab, autoScroll=True, scrollLimits={"maxSteps": 2})
                limited = result()
                assert limited["diagnostics"]["scroll_steps"] <= 2
                assert limited["diagnostics"]["scroll_stop_reason"] == "step_limit"
                assert "scroll_incomplete" in limited["diagnostics"]["quality_warnings"]
                assert source.evaluate("scrollY") == 0
                done("Step limit returns a partial preview with an explicit incomplete warning")

                begin(tab, autoScroll=True, scrollLimits={"maxSteps": 999, "maxMs": 999999})
                ceiling = result()
                assert ceiling["diagnostics"]["scroll_steps"] == 20
                assert ceiling["diagnostics"]["scroll_stop_reason"] == "step_limit"
                assert "scroll_incomplete" in ceiling["diagnostics"]["quality_warnings"]
                assert source.evaluate("scrollY") == 0
                done("Oversized caller limits are clamped to the production ceiling of 20 scroll steps")

                begin(tab, autoScroll=True, scrollLimits={"maxMs": 1000})
                timed = result()
                assert timed["diagnostics"]["scroll_stop_reason"] == "time_limit"
                assert "scroll_incomplete" in timed["diagnostics"]["quality_warnings"]
                assert source.evaluate("scrollY") == 0
                done("Time limit returns captured content and restores page position")

                source.evaluate("scrollTo({top:100,behavior:'instant'})")
                begin(tab, autoScroll=True)
                wait_js(driver, "captureProgress.some(p=>p.steps>=1)")
                driver.evaluate("captureAbort.abort()")
                wait_js(driver, "captureError !== null")
                assert "取消" in driver.evaluate("captureError")
                assert abs(source.evaluate("scrollY") - 100) <= 3
                progress_count = driver.evaluate("captureProgress.length")
                source.wait_for_timeout(800)
                assert abs(source.evaluate("scrollY") - 100) <= 3
                assert driver.evaluate("captureProgress.length") == progress_count
                done("Cancellation settles pending capture, stops progress and restores before returning")

                begin(tab, autoScroll=True)
                wait_js(driver, "captureProgress.some(p=>p.steps>=1)")
                source.keyboard.press("Escape")
                wait_js(driver, "captureError !== null")
                assert "取消" in driver.evaluate("captureError")
                assert abs(source.evaluate("scrollY") - 100) <= 3
                done("Escape cancels automatic loading and restores the reading position")

                tab = navigate("/lazy")
                begin(tab, autoScroll=True)
                wait_js(driver, "captureProgress.length > 0")
                source.evaluate("history.replaceState({},'', '#section-two')")
                anchored = result()
                assert "LAZY_017" in anchored["raw_text"]
                assert source.evaluate("scrollY") == 0
                done("Ordinary section-anchor updates during scrolling remain on the same document")

                tab = navigate("/lazy")
                begin(tab, autoScroll=True)
                wait_js(driver, "captureProgress.length > 0")
                source.evaluate("history.pushState({},'', '/different-route')")
                wait_js(driver, "captureError !== null")
                assert "页面已变化" in driver.evaluate("captureError")
                done("SPA navigation rejects stale snapshots without restoring the previous route's position")

                tab = navigate("/lazy")
                begin(tab, autoScroll=True)
                wait_js(driver, "captureProgress.length > 0")
                source.evaluate("history.pushState({},'', '#/different-route')")
                wait_js(driver, "captureError !== null")
                assert "页面已变化" in driver.evaluate("captureError")
                done("Hash-router navigation is still rejected as a different document")

                # UI component check: use a synthetic local identity/binding,
                # while all extraction and chrome.* calls remain real. Never
                # register a backend workspace or provide a model credential.
                tab = navigate("/lazy")
                panel = context.new_page()
                panel.set_viewport_size({"width": 420, "height": 1000})
                panel.goto(f"chrome-extension://{extension}/sidepanel.html")
                panel.wait_for_load_state("networkidle")
                panel.locator("#view-welcome").wait_for(state="visible")
                panel.evaluate("""async data=>{
                  webRagApiClient.getPlugin=()=>({pluginId:'scroll-ui-fixture',pluginName:'本地滚动测试',apiKeyConfigured:false});
                  currentTabId=data.tab;
                  binding={pluginId:'scroll-ui-fixture',pageUrl:data.url,pageTitle:'滚动采集测试',mode:'current',stale:false};
                  await sessionStore.setTabBinding(data.tab,binding);
                  renderAppView();switchView('clip');
                }""", {"tab": tab, "url": source.url})
                assert not panel.locator("#clip-auto-scroll").is_checked()
                panel.locator("#clip-mode").select_option("selection")
                assert panel.locator("#clip-auto-scroll").is_disabled()
                panel.locator("#clip-mode").select_option("article")
                panel.locator("#clip-auto-scroll").check()
                panel.locator("#clip-btn").click()
                wait_js(panel, "clipScrollProgress && clipScrollProgress.steps >= 1")
                assert "正在加载并采集" in panel.locator("#clip-status").inner_text()
                panel.screenshot(path=str(args.artifacts / "sidepanel-progress.png"))
                wait_js(panel, "!clipBusy && clipDraft !== null")
                assert "LAZY_017" in panel.locator("#clip-preview-text").input_value()
                assert "自动加载" in panel.locator("#clip-preview-diagnostics").inner_text()
                panel.locator("#clip-preview-text").fill("KEEP_EDITED_DRAFT527")
                panel.locator("#clip-btn").click()
                wait_js(panel, "clipScrollProgress && clipScrollProgress.steps >= 1")
                panel.locator("#clip-capture-cancel").click()
                wait_js(panel, "!clipBusy")
                assert panel.locator("#clip-preview-text").input_value() == "KEEP_EDITED_DRAFT527"
                assert "原有预览已保留" in panel.locator("#clip-status").inner_text()
                assert source.evaluate("scrollY") == 0
                panel.evaluate("chrome.storage.local.clear()")
                panel.close()
                done("Sidepanel checkbox, mode gating, live progress, lazy preview and cancel preserving edits passed (synthetic identity)")
                report["passed"] = True
            finally:
                context.close()
    finally:
        server.shutdown()
        server.server_close()
        (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

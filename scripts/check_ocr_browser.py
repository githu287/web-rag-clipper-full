"""Real extension -> raw-image API -> local CPU model, ephemeral test identity.

Uses dynamic fixture servers and dependency overrides, so owns its server setup.
No .env, databases, model keys, remote pages or document writes.
"""
from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
import uvicorn

from backend.core.config import Settings
Settings.model_config["env_file"] = None
from backend.api.deps import get_current_plugin
from backend.api.routers.ocr import router
from backend.core.di import get_plugin_service
from backend.core.exceptions import PluginCredentialsMissingError, PluginSecretMismatchError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/ocr-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "extension/tests/fixtures/ocr"
    checks = []
    image_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith(".png"):
                image_requests.append({"path": self.path, "workspace_header": bool(self.headers.get("X-Plugin-ID") or self.headers.get("X-Plugin-Secret"))})
                data = (fixture / ("long.png" if self.path == "/long.png" else "bilingual.png")).read_bytes()
                if self.path == "/tiny.png":
                    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZVNoAAAAASUVORK5CYII=")
                content_type = "image/png"
            else:
                body = '''<nav><img src="/nav.png" alt="导航图片"></nav>
                <article id="article"><h1>图片剪藏测试</h1><p>可编辑的网页正文。</p>
                <img src="/tiny.png" alt="小图标" width="16" height="16">
                <img src="/bilingual.png" alt="中英文正文" width="600">
                <img src="/long.png" alt="长图正文" width="600">
                <img src="/hidden.png" hidden alt="隐藏图片"></article>
                <aside><img src="/aside.png" alt="推荐图片"></aside>'''
                if self.path == "/only":
                    body = '<article><img src="/bilingual.png" width="600"></article>'
                if self.path == "/lazy":
                    body = '<article><h1>延迟图片</h1><img data-src="/bilingual.png" style="width:600px;height:200px"></article>'
                if self.path == "/unsupported":
                    body = '<article><p>普通正文</p><img src="data:image/png;base64,invalid" alt="内嵌图" width="600" height="100"></article>'
                if self.path == "/virtual-images":
                    body = '''<style>html,body{margin:0}#viewport{height:480px;overflow:auto}#spacer{height:2400px;position:relative}#rows{position:absolute}</style>
                    <article><h1>虚拟图片列表</h1><div id="viewport" role="main"><div id="spacer"><div id="rows"></div></div></div></article>
                    <script>const scroller=document.getElementById('viewport'), rows=document.getElementById('rows');function render(){let n=Math.floor(scroller.scrollTop/480);rows.style.top=(n*480)+'px';rows.innerHTML='<p>图片序号 '+n+'</p><img width="600" height="300" src="/virtual-'+n+'.png">'}scroller.onscroll=render;render();</script>'''
                data = ('<!doctype html><meta charset="utf-8"><title>本地 OCR 测试</title>' + body).encode()
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args): pass

    # Production router and authentication dependency; only the repository-facing
    # service uses an ephemeral fixed identity. Real OCR child/model is unchanged.
    api = FastAPI()
    api.include_router(router)
    class Identity:
        def authenticate(self, plugin_id, secret):
            if plugin_id != "ocr-browser-fixture" or secret != "ephemeral-ocr-fixture":
                raise PluginSecretMismatchError("invalid fixture identity")
            return type("Plugin", (), {"plugin_id": plugin_id})()
    api.dependency_overrides[get_plugin_service] = Identity
    async def unauthorized(_request, _error):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    api.add_exception_handler(PluginCredentialsMissingError, unauthorized)
    api.add_exception_handler(PluginSecretMismatchError, unauthorized)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    api_server = uvicorn.Server(uvicorn.Config(api, log_level="error"))
    api_thread = threading.Thread(target=lambda: api_server.run(sockets=[sock]), daemon=True)
    api_thread.start()
    source_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=source_server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{source_server.server_port}"

    def wait_js(page, expression, timeout=55):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.evaluate(expression): return
            time.sleep(.1)
        raise TimeoutError(expression)

    def done(text):
        checks.append(text)
        print(text, flush=True)

    try:
        with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":760,"height":900})
            try:
                cdp = context.browser.new_browser_cdp_session()
                extension = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                driver = context.new_page()
                driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
                source = context.new_page()
                source.goto(base)
                source.wait_for_load_state("networkidle")
                source.locator("article img").first.wait_for(state="visible")
                tab = driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                capture = driver.evaluate("async id=>webRagPageCapture.capture(id,{collectImages:true})", tab)
                assert [image["alt"] for image in capture["images"]] == ["中英文正文", "长图正文"], capture
                ordinary = driver.evaluate("async id=>webRagPageCapture.capture(id,{})", tab)
                assert not ordinary["images"]
                done("Default-off and article image scope exclude navigation, sidebar and hidden images")

                source.evaluate("""()=>{const range=document.createRange();range.selectNode(document.querySelector('article img[alt="中英文正文"]'));const selection=getSelection();selection.removeAllRanges();selection.addRange(range)}""")
                selected = driver.evaluate("async id=>webRagPageCapture.capture(id,{mode:'selection',collectImages:true})", tab)
                assert len(selected["images"]) == 1 and selected["images"][0]["alt"] == "中英文正文", selected
                done("Image-only DOM selection stays inside the selected range")

                panel = context.new_page()
                panel.set_viewport_size({"width":420,"height":1100})
                panel.goto(f"chrome-extension://{extension}/sidepanel.html")
                panel.wait_for_load_state("networkidle")
                panel.locator("#view-welcome").wait_for(state="visible")
                panel.evaluate("""async data=>{
                  WEB_RAG_CLIPPER_CONFIG.API_BASE_URL=data.api;
                  // api-client snapshots its base constant; route only this backend
                  // request to the dynamic test server without editing user config.
                  const original=fetch;
                  globalThis.fetch=(url,options)=>original(String(url).startsWith(API_BASE_URL+'/ocr/')?data.api+String(url).slice(API_BASE_URL.length):url,options);
                  webRagApiClient.setPluginDetails({pluginId:'ocr-browser-fixture',pluginSecret:'ephemeral-ocr-fixture',pluginName:'本地OCR测试',apiKeyConfigured:false});
                  currentTabId=data.tab;
                  binding={pluginId:'ocr-browser-fixture',pageUrl:data.url,pageTitle:'图片剪藏测试',mode:'current',stale:false};
                  await sessionStore.setTabBinding(data.tab,binding);
                  renderAppView();switchView('clip');
                }""", {"tab":tab,"url":source.url,"api":f"http://127.0.0.1:{port}"})
                assert not panel.locator("#clip-image-ocr").is_checked()
                panel.locator("#clip-image-ocr").check()
                panel.locator("#clip-btn").click()
                wait_js(panel, "clipScrollProgress && clipScrollProgress.phase === 'ocr'")
                assert "正在识别图片" in panel.locator("#clip-status").inner_text()
                wait_js(panel, "!clipBusy")
                text = panel.locator("#clip-preview-text").input_value().replace(" ", "")
                assert "中文与English2026" in text and "长图结束2026" in text, text
                assert "2 张有文字" in panel.locator("#clip-preview-diagnostics").inner_text()
                assert not panel.locator("#clip-preview-save-btn").is_disabled()
                panel.screenshot(path=str(args.artifacts / "preview.png"))
                assert not any(item["workspace_header"] for item in image_requests)
                done("Real sidepanel -> authenticated API -> local model produces editable bilingual and long-image preview without model key or database writes")

                panel.locator("#clip-preview-text").fill("KEEP_OCR_EDITED_DRAFT")
                panel.locator("#clip-btn").click()
                wait_js(panel, "clipScrollProgress && clipScrollProgress.phase === 'ocr'")
                panel.locator("#clip-capture-cancel").click()
                wait_js(panel, "!clipBusy")
                assert panel.locator("#clip-preview-text").input_value() == "KEEP_OCR_EDITED_DRAFT"
                done("Cancelling OCR preserves the previously edited preview")

                source.goto(base + "/only")
                pure = driver.evaluate("async id=>webRagPageCapture.capture(id,{collectImages:true})", tab)
                assert pure["images"] and pure["raw_text"] == "", pure
                source.goto(base + "/lazy")
                lazy = driver.evaluate("async id=>webRagPageCapture.capture(id,{collectImages:true})", tab)
                assert len(lazy["images"]) == 1 and lazy["images"][0]["url"].endswith("/bilingual.png"), lazy
                source.goto(base + "/unsupported")
                unsupported = driver.evaluate("async id=>webRagPageCapture.capture(id,{collectImages:true})", tab)
                assert "ocr_unsupported_source" in unsupported["diagnostics"]["quality_warnings"], unsupported
                done("Pure image articles, lazy data-src and unsupported source warning are handled explicitly")

                source.goto(base + "/virtual-images")
                virtual = driver.evaluate("async id=>webRagPageCapture.capture(id,{collectImages:true,autoScroll:true})", tab)
                assert len(virtual["images"]) == 5, virtual
                assert source.locator("#viewport").evaluate("el=>el.scrollTop") == 0
                done("Scrolling accumulates images before virtual DOM nodes are recycled and restores position")
                panel.evaluate("chrome.storage.local.clear()")
            finally:
                context.close()
    finally:
        api_server.should_exit = True
        api_thread.join(timeout=8)
        sock.close()
        source_server.shutdown()
        source_server.server_close()
        (args.artifacts / "report.json").write_text(json.dumps({"checks":checks,"passed":len(checks)==6,"real_model":True,"database_writes":False},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()

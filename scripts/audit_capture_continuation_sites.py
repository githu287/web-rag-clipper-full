"""Opt-in live-site sidepanel continuation audit; no user identity, database or cloud models.

Uses production scroll limits. --ocr starts an isolated production OCR route with
an ephemeral authentication service and the real local CPU model. Optional
--fail-first-ocr injects one 503 before the model to exercise manual recovery;
the report records this explicitly. Live access failures are observations, not CI.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright

from audit_capture_sites import INSPECT, fenced_code_blocks, normalize_code, normalized

ROOT = Path(__file__).resolve().parent.parent


def start_ocr(inject_failure):
    # Never load the user's .env or instantiate a database service.
    sys.path.insert(0, str(ROOT))
    from backend.core.config import Settings
    Settings.model_config["env_file"] = None
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    import uvicorn
    from backend.api.routers.ocr import router
    from backend.core.di import get_plugin_service, get_ocr_service
    from backend.core.exceptions import PluginCredentialsMissingError, PluginSecretMismatchError
    from backend.services.ocr import OcrError, OcrService

    class Identity:
        def authenticate(self, plugin_id, secret):
            if plugin_id != "live-capture-audit" or secret != "ephemeral-live-audit":
                raise PluginSecretMismatchError("invalid audit identity")
            return type("Plugin", (), {"plugin_id": plugin_id})()

    class Recognition:
        def __init__(self):
            self.model = OcrService()
            self.calls = self.injected = 0

        async def recognize(self, content):
            self.calls += 1
            if inject_failure and not self.injected:
                self.injected += 1
                raise OcrError(503, "OCR_UNAVAILABLE", "Audit-only single injected outage")
            return await self.model.recognize(content)

    recognition = Recognition()
    api = FastAPI()
    api.include_router(router)
    api.dependency_overrides[get_plugin_service] = Identity
    api.dependency_overrides[get_ocr_service] = lambda: recognition

    async def unauthorized(_request, _error):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    api.add_exception_handler(PluginCredentialsMissingError, unauthorized)
    api.add_exception_handler(PluginSecretMismatchError, unauthorized)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(api, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        raise RuntimeError("Isolated OCR API failed to start")
    return server, thread, sock, address, recognition


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--only", nargs="+", default=["github-readme", "github-yt-dlp", "vue-guide", "wechat-science"])
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--ocr", action="store_true", help="Use real local OCR on the live WeChat image sample")
    parser.add_argument("--fail-first-ocr", action="store_true", help="Inject one disclosed 503; all subsequent recognition is real")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/live-capture-continuation")
    args = parser.parse_args()
    if not 1 <= args.max_rounds <= 5:
        parser.error("--max-rounds must be between 1 and 5")
    if args.fail_first_ocr and not args.ocr:
        parser.error("--fail-first-ocr requires --ocr")
    samples = json.loads((ROOT / "extension/tests/site-samples.json").read_text(encoding="utf-8"))["samples"]
    supported = {sample["id"]: sample for sample in samples if sample["id"] in
                 {"github-readme", "github-yt-dlp", "vue-guide", "wechat-science", "wechat-text", "cnblogs-decorator"}}
    if set(args.only) - supported.keys():
        parser.error("Unknown continuation audit sample")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"date": datetime.now(timezone.utc).isoformat(), "extension_version": json.loads((ROOT / "extension/manifest.json").read_text())["version"],
              "production_limits": True, "database_writes": False, "cloud_calls": 0,
              "ocr_real_model": args.ocr, "ocr_injected_outage_requested": args.fail_first_ocr, "results": []}
    api = start_ocr(args.fail_first_ocr) if args.ocr else None

    def save():
        if api:
            report["ocr_api_calls"] = api[4].calls
            report["ocr_injected_outages"] = api[4].injected
        (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":1280,"height":1000})
            try:
                extension = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
                panel = context.new_page()
                panel.set_viewport_size({"width":420,"height":1100})
                panel.goto(f"chrome-extension://{extension}/sidepanel.html")
                panel.wait_for_load_state("networkidle")
                panel.evaluate("""data => {
                  const original = fetch;
                  globalThis.fetch = (url, options) => {
                    const target = new URL(String(url));
                    if (target.origin === new URL(API_BASE_URL).origin) {
                      if (!data.api || target.pathname !== '/ocr/image') return Promise.reject(new Error('Audit forbids other backend calls'));
                      return original(data.api + target.pathname, options);
                    }
                    return original(url, options);
                  };
                  webRagApiClient.setPluginDetails({pluginId:'live-capture-audit',pluginSecret:'ephemeral-live-audit',pluginName:'真实剪藏验收'});
                }""", {"api": api[3] if api else None})
                for sample_id in args.only:
                    sample = supported[sample_id]
                    is_ocr = sample_id == "wechat-science"
                    record = {"id":sample_id,"url":sample["url"],"rounds":[]}
                    report["results"].append(record)
                    if is_ocr and not args.ocr:
                        record["status"] = "skipped_ocr_not_enabled"
                        save()
                        continue
                    source = context.new_page()
                    started = time.monotonic()
                    try:
                        response = source.goto(sample["url"], wait_until="domcontentloaded", timeout=25000)
                        record["http_status"] = response.status if response else None
                        try: source.wait_for_load_state("networkidle", timeout=3500)
                        except Exception: pass
                        source.evaluate("scrollTo(0,150)")
                        source.wait_for_timeout(150)
                        original_url, original_top = source.url, source.evaluate("scrollY")
                        tab = panel.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                        panel.evaluate("""data => {
                          clearClipDraft();currentTabId=data.tab;
                          binding={pluginId:'live-capture-audit',pageUrl:data.url,pageTitle:'在线样本',mode:'current',stale:false};
                          renderAppView();switchView('clip');
                        }""", {"tab":tab,"url":source.url})
                        panel.locator("#clip-mode").select_option("article")
                        panel.locator("#clip-auto-scroll").set_checked(not is_ocr)
                        panel.locator("#clip-pagination").uncheck()
                        panel.locator("#clip-image-ocr").set_checked(is_ocr)
                        panel.locator("#clip-btn").click()
                        panel.wait_for_function("()=>!clipBusy", timeout=160000)
                        initial = panel.evaluate("clipDraft && {text:clipDraft.raw_text,diagnostics:clipDraft.diagnostics}")
                        if not initial:
                            raise RuntimeError(panel.locator("#clip-status").inner_text())
                        record["initial_chars"] = len(initial["text"])
                        record["initial_diagnostics"] = initial["diagnostics"]
                        original = panel.locator("#clip-preview-text").input_value() + "\n\nLIVE_AUDIT_USER_EDIT_80%"
                        panel.locator("#clip-preview-title").fill("在线验收保留的编辑标题")
                        panel.locator("#clip-preview-text").fill(original)
                        button = panel.locator("#clip-retry-images" if is_ocr else "#clip-continue-scroll")
                        for round_number in range(1, args.max_rounds + 1):
                            if not button.is_visible(): break
                            old = panel.locator("#clip-preview-text").input_value()
                            before_calls = api[4].calls if api else 0
                            button.click()
                            panel.wait_for_function("()=>!clipBusy", timeout=160000)
                            if not panel.locator("#clip-continuation-pending").is_visible():
                                raise RuntimeError(panel.locator("#clip-status").inner_text())
                            assert panel.locator("#clip-preview-text").input_value() == old
                            delta = panel.locator("#clip-continuation-text").input_value()
                            proposed = panel.evaluate("clipContinuationPending.session.report")
                            review = panel.locator("#clip-continuation-summary").inner_text()
                            if not delta.strip():
                                assert "没有新增文字" in review
                                assert panel.locator("#clip-continuation-apply").inner_text() == "确认更新采集进度"
                            panel.screenshot(path=str(args.artifacts / f"{sample_id}-review-{round_number}.png"), full_page=True)
                            panel.locator("#clip-continuation-apply").click()
                            panel.wait_for_function("()=>clipContinuationPending===null")
                            combined = panel.locator("#clip-preview-text").input_value()
                            assert combined.startswith(old) and combined.count("LIVE_AUDIT_USER_EDIT_80%") == 1
                            assert panel.locator("#clip-preview-title").input_value() == "在线验收保留的编辑标题"
                            if not delta.strip(): assert combined == old
                            record["rounds"].append({"round":round_number,"added_chars":len(delta),"ocr_calls":(api[4].calls if api else 0)-before_calls,
                                                     "preview_edits_preserved":True,"review":review,"diagnostics":proposed})
                            print(json.dumps({"id":sample_id,"round":round_number,"added_chars":len(delta)}, ensure_ascii=False), flush=True)
                        final = panel.locator("#clip-preview-text").input_value()
                        inspected = source.evaluate(INSPECT, sample)
                        readable = re.sub(r'\[([^\]]*)\]\(<[^>]*>\)', r'\1', final)
                        readable = re.sub(r'(?m)(^|<br>)\s*(?:[-*+] |\d+\. )', r'\1', readable).replace('<br>', '')
                        actual = normalized(readable)
                        blocks = inspected["blocks"]
                        record["reference_blocks"] = len(blocks)
                        record["matched_blocks"] = sum(normalized(block["text"]) in actual for block in blocks)
                        codes = inspected["code_samples"]
                        actual_codes = fenced_code_blocks(final)
                        record["reference_code_blocks"] = len(codes)
                        record["exact_code_blocks"] = sum(normalize_code(code) in actual_codes for code in codes)
                        record["source_unchanged"] = source.url == original_url and abs(source.evaluate("scrollY")-original_top) < 3
                        record["final_diagnostics"] = panel.evaluate("clipDraft.diagnostics")
                        record["continuation_still_available"] = button.is_visible()
                        (args.artifacts / f"{sample_id}.txt").write_text(final, encoding="utf-8")
                        panel.screenshot(path=str(args.artifacts / f"{sample_id}-final.png"), full_page=True)
                        if is_ocr:
                            d = record["final_diagnostics"]
                            okay = d["ocr_recognized"] > 0 and not d["ocr_failed"] and not any(image["status"] == "pending" for image in d["ocr_images"])
                        else:
                            okay = inspected["root_found"] and len(blocks)>0 and record["matched_blocks"]==len(blocks) and record["exact_code_blocks"]==len(codes)
                        record["status"] = "checks_passed" if okay and record["source_unchanged"] else "needs_review"
                    except Exception as error:
                        record["status"] = "access_or_transport_error" if "net::ERR_" in str(error) else "audit_failed"
                        record["error"] = str(error)[:700]
                    finally:
                        record["seconds"] = round(time.monotonic()-started, 3)
                        panel.evaluate("clearClipDraft()")
                        source.close()
                        save()
                        print(json.dumps({key:record.get(key) for key in ("id","status","initial_chars","reference_blocks","matched_blocks","exact_code_blocks","seconds")}, ensure_ascii=False), flush=True)
            finally: context.close()
    finally:
        if api:
            api[0].should_exit = True
            api[1].join(timeout=8)
            api[2].close()
        save()


if __name__ == "__main__": main()

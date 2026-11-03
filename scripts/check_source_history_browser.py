"""Real extension history/source UI with controlled HTTP fixtures, no user data or paid APIs."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/source-history-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, requests, waiting = [], [], []
    behavior = {"mode": "normal"}
    documents = {1: {"revision": 2}, 2: {"revision": 7}, 3: {"revision": 2}, 4: {"revision": 2}}

    def result(refs):
        rows = []
        for ref in refs:
            doc = documents.get(ref["document_id"])
            state = "missing" if doc is None else "unavailable" if doc.get("unavailable") else (
                "unversioned" if ref["content_revision"] is None else
                "current" if ref["content_revision"] == doc["revision"] else "changed")
            rows.append({**ref, "state": state})
        return {"sources": rows}

    def route_api(route):
        path = route.request.url.split(":18001", 1)[1].split("?", 1)[0]
        if path == "/plugins/me":
            owner = route.request.headers.get("x-plugin-id", "fixture-a")
            route.fulfill(json={"plugin_id": owner, "plugin_name": owner, "api_key_configured": False})
        elif path == "/rag/sources/check":
            refs = route.request.post_data_json["sources"]
            requests.append({"owner": route.request.headers.get("x-plugin-id"), "sources": refs})
            if behavior["mode"] == "delay":
                waiting.append((route, refs))
            elif behavior["mode"] == "error":
                route.fulfill(status=503, json={"detail": "fixture unavailable"})
            elif behavior["mode"] == "unsupported":
                route.fulfill(status=404, json={"detail": "old backend has no check endpoint"})
            elif behavior["mode"] == "malformed":
                route.fulfill(json={"sources": [{"document_id": 999, "content_revision": 2, "state": "current"}] * len(refs)})
            else:
                route.fulfill(json=result(refs))
        elif path == "/documents":
            route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 10})
        else:
            route.fulfill(status=404, json={"detail": "fixture route unavailable"})

    with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            viewport={"width": 420, "height": 900}, ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging"])
        try:
            extension_id = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            page = context.new_page()
            page.route("http://localhost:18001/**", route_api)
            page.goto("chrome-extension://" + extension_id + "/sidepanel.html")
            page.wait_for_load_state("networkidle")
            expect(page.locator("#plugin-register-btn")).to_be_visible()
            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-a',pluginSecret:'fixture-a-secret',pluginName:'fixture-a'});
                await webRagApiClient.persistPlugin(); await validatePlugin();
                session = await sessionStore.createSession('fixture-a', {title:'历史来源验收'});
                session.messages = [{id:'history-a',role:'assistant',mode:'all',content:'历史答案：编号806。',createdAt:1,
                  sources:[{document_id:1,content_revision:2,chunk_id:'1_0',title:'<img src=x onerror=alert(1)>',score:.9},
                           {document_id:2,chunk_id:'2_0',title:'旧版引用',score:.8},
                           {document_id:3,content_revision:2,chunk_id:'3_0',title:'第三份资料',score:.7}]}];
                await sessionStore.setCurrentSessionId('fixture-a',session.sessionId);
                await saveCurrentSession(); renderChat();
            }""")
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(1)
            expect(page.locator(".msg-bubble")).to_contain_text("编号806")
            expect(page.locator(".sources-head")).to_contain_text("未核对")
            assert not requests
            checks.append("Stored history restores source revisions and stays explicitly unchecked without background/model requests")

            page.locator(".sources-head").click()
            statuses = page.locator(".source-status")
            expect(statuses.nth(0)).to_contain_text("与回答时版本一致")
            expect(statuses.nth(1)).to_contain_text("旧回答未记录版本")
            expect(page.locator(".source-title").first).to_contain_text("<img")
            assert page.locator(".source-card img").count() == 0 and len(requests) == 1
            checks.append("Expanding sources issues one bounded authenticated check; old versions remain unknown and source markup stays text")

            documents[1]["revision"] = 4
            page.locator(".sources-recheck").click()
            expect(statuses.nth(0)).to_contain_text("资料已更新")
            expect(page.locator(".sources-head")).to_contain_text("已更新")
            expect(page.locator(".msg-bubble")).to_contain_text("编号806")
            page.locator(".sources-head").click()
            expect(page.locator(".sources-head")).to_contain_text("已更新")
            page.locator(".sources-head").click()
            expect(statuses.nth(0)).to_contain_text("资料已更新")
            checks.append("Reclip marks the historical answer as outdated, including the collapsed badge, while retaining its original text")

            documents.pop(3)
            documents[1]["unavailable"] = True
            page.locator(".sources-recheck").click()
            expect(statuses.nth(0)).to_contain_text("暂不可用")
            expect(statuses.nth(2)).to_contain_text("已删除或不可访问")
            checks.append("Unavailable and deleted/inaccessible sources have distinct labels")

            for mode in ("error", "malformed", "unsupported"):
                behavior["mode"] = mode
                page.locator(".sources-recheck").click()
                expect(page.locator(".sources-summary")).to_contain_text("暂时无法核对")
                expect(page.locator(".sources-head")).to_contain_text("未完成核对")
                expect(page.locator(".sources-recheck")).to_be_enabled()
            behavior["mode"] = "normal"
            page.locator(".sources-recheck").click()
            expect(statuses.nth(2)).to_contain_text("已删除或不可访问")
            checks.append("HTTP failures and malformed replies never mark evidence current; manual retry restores status")
            page.set_viewport_size({"width": 360, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            page.screenshot(path=str(args.artifacts / "source-status.png"), full_page=True)

            def wait_for_route():
                deadline = time.monotonic() + 5
                while not waiting and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                assert waiting, "Controlled pending source check did not start"

            behavior["mode"] = "delay"
            page.locator(".sources-recheck").click()
            wait_for_route()
            page.evaluate("renderChat()")
            old_route, refs = waiting.pop()
            old_route.fulfill(json=result(refs))
            expect(page.locator(".sources-head")).to_contain_text("未核对")
            checks.append("A response for a detached conversation view cannot mark its replacement checked")

            page.locator(".sources-head").click()
            wait_for_route()
            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-b',pluginSecret:'fixture-b-secret',pluginName:'fixture-b'});
                await webRagApiClient.persistPlugin(); await validatePlugin(); renderChat();
            }""")
            old_route, refs = waiting.pop()
            old_route.fulfill(json=result(refs))
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(0)
            assert page.locator(".source-status").count() == 0
            assert page.evaluate("webRagApiClient.getPlugin().pluginId") == "fixture-b"
            checks.append("A late source response cannot populate another Workspace")
            (args.artifacts / "report.json").write_text(json.dumps({"passed": True, "scope": __doc__, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            for check in checks:
                print(check, flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()

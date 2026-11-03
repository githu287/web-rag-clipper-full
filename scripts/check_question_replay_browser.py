"""Real extension replay/compose UI with controlled HTTP replies; no paid calls or user database."""
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
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/question-replay-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, calls, waiting = [], [], []
    behavior = {"mode": "normal"}

    def answer(payload):
        doc = payload.get("document_id") or 300
        return {"answer": "新回答：" + str(doc), "sources": [{"document_id": doc, "content_revision": 4,
            "title": "测试资料", "url": "https://example.com/source", "chunk_id": f"{doc}_0", "score": .9}]}

    def route_api(route):
        path = route.request.url.split(":18001", 1)[1].split("?", 1)[0]
        if path == "/plugins/me":
            owner = route.request.headers.get("x-plugin-id", "fixture-a")
            route.fulfill(json={"plugin_id": owner, "plugin_name": owner, "api_key_configured": True})
        elif path == "/model-providers":
            route.fulfill(json={"request_timeouts_ms": {"ask": 10000}})
        elif path == "/rag/ask":
            payload = route.request.post_data_json
            calls.append({"owner": route.request.headers.get("x-plugin-id"), **payload})
            if behavior["mode"] == "delay":
                waiting.append((route, payload))
            elif behavior["mode"] == "deleted" and payload.get("document_id") == 77:
                route.fulfill(status=404, json={"type": "DocumentNotFoundError", "message": "原文档不存在"})
            else:
                route.fulfill(json=answer(payload))
        elif path == "/rag/sources/check":
            route.fulfill(json={"sources": [{**ref, "state": "changed"} for ref in route.request.post_data_json["sources"]]})
        elif path == "/documents":
            route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 10})
        else:
            route.fulfill(status=404, json={"detail": "fixture endpoint unavailable"})

    with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            viewport={"width": 420, "height": 1000}, ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging"])
        try:
            eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            page = context.new_page()
            page.route("http://localhost:18001/**", route_api)
            page.goto("chrome-extension://" + eid + "/sidepanel.html")
            page.wait_for_load_state("networkidle")
            expect(page.locator("#plugin-register-btn")).to_be_visible()
            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-a',pluginSecret:'secret-a',pluginName:'fixture-a'});
                await webRagApiClient.persistPlugin(); await validatePlugin();
                session = await sessionStore.createSession('fixture-a', {title:'原问题回填验收'});
                const scope = {mode:'current',documentId:77,pageTitle:'原文档 A',pageUrl:'https://example.com/a'};
                session.messages = [
                  {id:'u1',role:'user',content:'旧问题：编号是多少？',createdAt:1,...scope},
                  {id:'a1',role:'assistant',content:'旧回答806',question:'旧问题：编号是多少？',replyTo:'u1',createdAt:2,...scope,
                    sources:[{document_id:77,content_revision:2,chunk_id:'77_0',score:.9}]},
                  {id:'orphan',role:'assistant',content:'缺少原问题的旧记录',createdAt:3,...scope}];
                await sessionStore.setCurrentSessionId('fixture-a',session.sessionId);
                await saveCurrentSession();
                binding = {mode:'all',documentId:202,pageTitle:'新页面 B',pageUrl:'https://example.com/b',stale:false};
                renderChat(); updateSendState();
            }""")
            original_session = page.evaluate("session.sessionId")
            page.locator("#nav-chat").click()
            replays = page.locator(".msg-replay")
            expect(replays.nth(0)).to_be_enabled()
            expect(replays.nth(1)).to_be_disabled()
            page.locator(".sources-head").click()
            expect(page.locator(".source-status")).to_contain_text("资料已更新")
            page.locator("#chat-textarea").fill("我正在编辑的草稿")
            replays.nth(0).click()
            expect(page.locator("#chat-textarea")).to_have_value("我正在编辑的草稿")
            expect(page.locator("#chat-replay-notice")).to_be_visible()
            expect(page.locator("#chat-replay-context")).to_be_hidden()
            page.locator("#chat-textarea").fill("")
            replays.nth(0).click()
            expect(page.locator("#chat-textarea")).to_have_value("旧问题：编号是多少？")
            expect(page.locator("#chat-replay-notice")).to_be_hidden()
            expect(page.locator("#chat-replay-context")).to_contain_text("原文档 A")
            assert page.locator(".scope-btn.active").count() == 0
            assert not calls
            checks.append("Updated history fills the recorded question and original document without sending; unrecoverable history and existing drafts are preserved")

            page.locator("#chat-textarea").fill("改问：当前编号是多少？")
            page.evaluate("binding={mode:'current',documentId:202,pageTitle:'新页面 B',stale:false};updateSendState()")
            expect(page.locator("#scope-desc")).to_contain_text("原文档 A")
            page.set_viewport_size({"width": 360, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth<=document.documentElement.clientWidth+1")
            page.screenshot(path=str(args.artifacts / "replay-draft.png"), full_page=True)
            behavior["mode"] = "delay"
            page.locator("#chat-send").evaluate("button=>{button.click();button.click()}")
            deadline = time.monotonic() + 5
            while not waiting and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert waiting and len(calls) == 1
            assert calls[-1]["document_id"] == 77 and calls[-1]["query"] == "改问：当前编号是多少？"
            page.locator("#new-session-btn").click()
            route, payload = waiting.pop()
            route.fulfill(json=answer(payload))
            page.wait_for_function("() => !isSending")
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(0)
            saved = page.evaluate("id=>sessionStore.getSession(id)", original_session)
            assert saved["messages"][-1]["content"] == "新回答：77"
            assert saved["messages"][-1]["question"] == "改问：当前编号是多少？"
            assert saved["messages"][-1]["replyTo"] == saved["messages"][-2]["id"]
            assert saved["messages"][-1]["documentId"] == 77
            checks.append("Edited replay keeps its original document through binding changes; double-click sends once and a late answer stays in its original session")

            page.evaluate("""async id=>{session=await sessionStore.getSession(id);await sessionStore.setCurrentSessionId('fixture-a',id);renderChat();updateSendState();}""", original_session)
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator(".msg-replay").last.click()
            expect(page.locator("#chat-textarea")).to_have_value("改问：当前编号是多少？")
            expect(page.locator("#chat-replay-context")).to_contain_text("原文档 A")
            page.locator("#scope-all").click()
            expect(page.locator("#chat-replay-context")).to_be_hidden()
            behavior["mode"] = "normal"
            page.locator("#chat-send").click()
            page.wait_for_function("() => !isSending")
            assert len(calls) == 2 and "document_id" not in calls[-1]
            checks.append("Reload preserves replay linkage; explicitly selecting all-library releases the original document scope")

            page.locator(".msg-replay").first.click()
            behavior["mode"] = "deleted"
            page.locator("#chat-send").click()
            page.wait_for_function("() => !isSending")
            assert len(calls) == 3 and calls[-1]["document_id"] == 77
            assert page.evaluate("session.messages.at(-1).documentId") == 77
            assert page.evaluate("session.messages.at(-1).question") == "旧问题：编号是多少？"
            checks.append("A deleted original document fails within its original scope without fallback to a different document or whole library")

            page.locator(".msg-replay").last.click()
            page.locator("#chat-replay-cancel").click()
            expect(page.locator("#chat-textarea")).to_have_value("旧问题：编号是多少？")
            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-b',pluginSecret:'secret-b',pluginName:'fixture-b'});
                await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
            }""")
            expect(page.locator("#chat-textarea")).to_have_value("")
            expect(page.locator("#chat-replay-context")).to_be_hidden()
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(0)
            assert len(calls) == 3
            checks.append("Workspace changes clear the private replay draft and cannot silently send it in the new workspace")

            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-a',pluginSecret:'secret-a',pluginName:'fixture-a'});
                await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
            }""")
            page.locator(".msg-replay").last.click()
            behavior["mode"] = "delay"
            page.locator("#chat-send").click()
            deadline = time.monotonic() + 5
            while not waiting and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert waiting
            page.evaluate("""async () => {
                webRagApiClient.setPluginDetails({pluginId:'fixture-b',pluginSecret:'secret-b',pluginName:'fixture-b'});
                await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
            }""")
            route, payload = waiting.pop()
            route.fulfill(json=answer(payload))
            page.wait_for_function("() => !isSending")
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(0)
            previous = page.evaluate("id=>sessionStore.getSession(id)", original_session)
            assert previous["messages"][-1]["role"] == "user"
            assert len(calls) == 4
            checks.append("A replay response from a previous Workspace is discarded without writing an assistant message")
            (args.artifacts / "report.json").write_text(json.dumps({"passed": True, "scope": __doc__, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            for check in checks:
                print(check, flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()

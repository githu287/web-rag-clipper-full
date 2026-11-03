"""Real unpacked Chrome extension draft persistence; controlled API replies, no paid calls or user database."""
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
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/question-drafts-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, calls, waiting, errors = [], [], [], []
    behavior = {"delay": False}

    def answer(payload):
        return {"answer": "受控回答", "sources": []}

    def route_api(route):
        endpoint = route.request.url.split(":18001", 1)[1].split("?", 1)[0]
        if endpoint == "/plugins/me":
            owner = route.request.headers.get("x-plugin-id", "draft-a")
            route.fulfill(json={"plugin_id": owner, "plugin_name": owner, "api_key_configured": True})
        elif endpoint == "/model-providers":
            route.fulfill(json={"request_timeouts_ms": {"ask": 10000}})
        elif endpoint == "/documents":
            route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 10})
        elif endpoint == "/rag/ask":
            payload = route.request.post_data_json
            calls.append({"owner": route.request.headers.get("x-plugin-id"), **payload})
            if behavior["delay"]:
                waiting.append((route, payload))
            else:
                route.fulfill(json=answer(payload))
        else:
            route.fulfill(status=404, json={"detail": "fixture endpoint unavailable"})

    with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            viewport={"width": 360, "height": 1000}, ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging"])
        context.route("http://localhost:18001/**", route_api)
        try:
            eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            url = "chrome-extension://" + eid + "/sidepanel.html"

            def open_panel():
                panel = context.new_page()
                panel.on("pageerror", lambda error: errors.append(str(error)))
                panel.goto(url)
                panel.wait_for_load_state("networkidle")
                return panel

            def select_owner(panel, name="a", secret=None):
                panel.evaluate("""async ({name,secret}) => {
                    webRagApiClient.setPluginDetails({pluginId:'draft-'+name,pluginSecret:secret||('secret-'+name),pluginName:'draft-'+name});
                    await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
                }""", {"name": name, "secret": secret})
                panel.locator("#nav-chat").click()

            def saved(panel):
                expect(panel.locator("#chat-draft-status")).to_contain_text("保存在本机")
                panel.evaluate("() => questionDraftController().flush()")

            page = open_panel()
            select_owner(page)
            page.evaluate("""async () => {
                binding={mode:'current',documentId:77,pageTitle:'中文资料 A',pageUrl:'https://example.com/a',stale:false};
                session.messages=[{id:'old-answer',role:'assistant',content:'历史答案',question:'原问题？',mode:'current',
                  documentId:77,pageTitle:'中文资料 A',pageUrl:'https://example.com/a',createdAt:1}];
                await saveCurrentSession();updateSendState();
            }""")
            original = page.evaluate("session.sessionId")
            draft_text = "请按三点总结资料 A。\nAlso preserve English terms."
            page.locator("#chat-textarea").fill(draft_text)
            saved(page)
            page.close()
            page = open_panel()
            page.locator("#nav-chat").click()
            expect(page.locator("#chat-textarea")).to_have_value(draft_text)
            expect(page.locator("#chat-replay-context")).to_contain_text("中文资料 A")
            assert not calls
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
            page.screenshot(path=str(args.artifacts / "restored-draft.png"), full_page=True)
            checks.append("Closing and reopening a real extension page restores multiline Chinese/English text and its original document without model calls")

            behavior["delay"] = True
            page.locator("#chat-send").click()
            deadline = time.monotonic() + 5
            while not waiting and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert waiting and calls[-1]["document_id"] == 77
            page.locator("#chat-textarea").fill("回答生成期间编辑的下一条")
            saved(page)
            route, payload = waiting.pop()
            route.fulfill(json=answer(payload))
            page.wait_for_function("() => !isSending")
            expect(page.locator("#chat-textarea")).to_have_value("回答生成期间编辑的下一条")
            assert page.evaluate("() => questionDraftController().list().then(items => items[0].query)") == "回答生成期间编辑的下一条"
            behavior["delay"] = False
            checks.append("Send retains the restored document; late answers cannot clear or overwrite a newly typed next draft")

            page.locator("#chat-textarea").fill("")
            page.locator(".msg-replay").first.click()
            page.locator("#chat-textarea").fill("原问题编辑后暂不发送")
            saved(page)
            page.locator("#new-session-btn").click()
            expect(page.locator("#chat-textarea")).to_have_value("")
            page.locator("#chat-draft-find").click()
            page.locator(".draft-restore-btn", has_text="原问题编辑后暂不发送").click()
            expect(page.locator("#chat-textarea")).to_have_value("原问题编辑后暂不发送")
            assert page.evaluate("session.sessionId") == original
            expect(page.locator("#chat-replay-context")).to_contain_text("中文资料 A")
            assert len(calls) == 1
            checks.append("New sessions leave prior drafts recoverable; selecting a draft returns to its own conversation and frozen replay scope")

            select_owner(page, "b")
            expect(page.locator("#chat-textarea")).to_have_value("")
            page.locator("#chat-draft-find").click()
            expect(page.locator("#chat-draft-list")).to_have_text("当前空间暂无未发送草稿")
            page.locator("#scope-all").click()
            page.locator("#chat-textarea").fill("B 的私有草稿")
            saved(page)
            select_owner(page)
            expect(page.locator("#chat-textarea")).to_have_value("原问题编辑后暂不发送")
            select_owner(page, secret="rotated-secret-a")
            expect(page.locator("#chat-textarea")).to_have_value("")
            page.locator("#chat-draft-find").click()
            expect(page.locator("#chat-draft-list")).to_have_text("当前空间暂无未发送草稿")
            raw = page.evaluate("""async () => {
                const values=await chrome.storage.local.get(null);
                return JSON.stringify(Object.fromEntries(Object.entries(values).filter(([key])=>key.startsWith(STORAGE_KEYS.DRAFT_PREFIX))));
            }""")
            assert all(secret not in raw for secret in ["secret-a", "secret-b", "rotated-secret-a"])
            checks.append("Workspace and credential changes isolate draft restoration and listings; persisted draft records contain no plaintext credentials")

            page.evaluate("binding={mode:'current',documentId:null,stale:true};updateSendState()")
            page.locator("#chat-textarea").fill("尚未剪藏页面上的草稿")
            saved(page)
            page.close()
            page = open_panel()
            page.locator("#nav-chat").click()
            expect(page.locator("#chat-textarea")).to_have_value("尚未剪藏页面上的草稿")
            expect(page.locator("#chat-replay-context")).to_contain_text("原范围不可用")
            expect(page.locator("#chat-send")).to_be_disabled()
            page.locator("#chat-textarea").press("Enter")
            assert len(calls) == 1
            page.locator("#scope-all").click()
            expect(page.locator("#chat-textarea")).to_have_value("尚未剪藏页面上的草稿")
            page.locator("#chat-send").click()
            page.wait_for_function("() => !isSending")
            assert len(calls) == 2 and "document_id" not in calls[-1]
            page.evaluate("() => questionDraftController().flush()")
            page.reload()
            page.wait_for_load_state("networkidle")
            page.locator("#nav-chat").click()
            expect(page.locator("#chat-textarea")).to_have_value("")
            checks.append("Unknown original scope blocks both button and Enter until an explicit scope selection; submitted drafts do not reappear on reload")

            page.evaluate("""() => {
                globalThis.draftOriginalSet=chrome.storage.local.set.bind(chrome.storage.local);
                chrome.storage.local.set=items=>Object.keys(items).some(key=>key.startsWith(STORAGE_KEYS.DRAFT_PREFIX))
                  ? Promise.reject(new Error('fixture quota')) : draftOriginalSet(items);
            }""")
            page.locator("#chat-textarea").fill("存储失败时不要丢掉我的输入")
            expect(page.locator("#chat-draft-status")).to_contain_text("未保存")
            expect(page.locator("#chat-textarea")).to_have_value("存储失败时不要丢掉我的输入")
            previous_session = page.evaluate("session.sessionId")
            page.locator("#new-session-btn").click()
            expect(page.locator("#chat-draft-status")).to_contain_text("未保存")
            assert page.evaluate("session.sessionId") == previous_session
            expect(page.locator("#chat-textarea")).to_have_value("存储失败时不要丢掉我的输入")
            page.wait_for_function("() => !els.chatTextarea.readOnly")
            page.evaluate("() => {chrome.storage.local.set=draftOriginalSet}")
            page.locator("#chat-textarea").fill("存储恢复后再次编辑")
            saved(page)
            checks.append("Storage failures keep the current input and prevent leaving it for a new session; a later edit retries successfully")

            page.evaluate("""() => {
                globalThis.originalGetDraft=sessionStore.getQuestionDraft;
                globalThis.draftReadGate=new Promise(resolve=>{globalThis.releaseDraftRead=resolve});
                sessionStore.getQuestionDraft=async (...args)=>{await draftReadGate;return originalGetDraft(...args)};
                els.chatTextarea.value='';questionReplayDraft=null;
                questionDraftController().activate(null);syncQuestionComposerOwner();
            }""")
            page.locator("#chat-textarea").fill("读取尚未结束时的新编辑")
            saved(page)
            page.evaluate("releaseDraftRead();sessionStore.getQuestionDraft=originalGetDraft")
            expect(page.locator("#chat-textarea")).to_have_value("读取尚未结束时的新编辑")
            assert len(calls) == 2 and not errors, errors
            checks.append("Delayed draft reads cannot overwrite new typing; 360px layout has no horizontal overflow and no browser JavaScript errors")
            (args.artifacts / "report.json").write_text(json.dumps({"passed": True, "scope": __doc__, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            for check in checks:
                print(check, flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()

"""Real Chrome session history, draft-safe navigation and cross-panel deletion; controlled HTTP, no user database or paid models."""
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
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/chat-sessions-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, calls, waiting, errors = [], [], [], []

    def route_api(route):
        endpoint = route.request.url.split(":18001", 1)[1].split("?", 1)[0]
        if endpoint == "/plugins/me":
            owner = route.request.headers.get("x-plugin-id", "history-a")
            route.fulfill(json={"plugin_id": owner, "plugin_name": owner, "api_key_configured": True})
        elif endpoint == "/model-providers":
            route.fulfill(json={"request_timeouts_ms": {"ask": 30000}})
        elif endpoint == "/documents":
            route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 10})
        elif endpoint == "/rag/ask":
            payload = route.request.post_data_json
            calls.append(payload)
            waiting.append(route)
        else:
            route.fulfill(status=404, json={"detail": "fixture endpoint unavailable"})

    def passed(value):
        checks.append(value)
        print(value, flush=True)

    with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            viewport={"width": 320, "height": 760}, ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging"])
        context.route("http://localhost:18001/**", route_api)
        page = None
        try:
            eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            url = "chrome-extension://" + eid + "/sidepanel.html"

            def panel():
                result = context.new_page()
                result.on("pageerror", lambda error: errors.append(str(error)))
                result.goto(url)
                result.wait_for_load_state("networkidle")
                return result

            def select_owner(target, name="a"):
                target.evaluate("""async name => {
                    webRagApiClient.setPluginDetails({pluginId:'history-'+name,pluginSecret:'secret-'+name,pluginName:'history-'+name});
                    await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
                }""", name)
                target.locator("#nav-chat").click()

            def history(target):
                if target.locator("#chat-sessions-toggle").get_attribute("aria-expanded") != "true":
                    target.locator("#chat-sessions-toggle").click()
                expect(target.locator("#chat-sessions")).to_be_visible()
                expect(target.locator("#chat-sessions-status")).not_to_contain_text("正在读取")

            def row(target, sid):
                return target.locator(f'.chat-session-row[data-session-id="{sid}"]')

            def choose(target, sid):
                history(target)
                row(target, sid).locator(".chat-session-open").click()
                target.wait_for_function("id => session && session.sessionId === id", arg=sid)
                target.wait_for_function("() => !els.chatTextarea.readOnly")

            def draft_saved(target):
                expect(target.locator("#chat-draft-status")).to_contain_text("保存在本机")
                target.evaluate("() => questionDraftController().flush()")

            def request_wait(target):
                deadline = time.monotonic() + 5
                while not waiting and time.monotonic() < deadline:
                    target.wait_for_timeout(20)
                assert waiting

            def delete(target, sid):
                history(target)
                row(target, sid).locator(".chat-session-delete").click()
                row(target, sid).locator(".session-delete-confirm").click()

            page = panel()
            select_owner(page)
            ids = page.evaluate("""async () => {
                binding={mode:'current',documentId:77,pageTitle:'原文档 A',pageUrl:'https://example.com/a',stale:false};
                const scope={mode:'current',documentId:77,pageTitle:'原文档 A'};
                session.title='中文访谈 · 第一节';
                session.messages=[{id:'u',role:'user',content:'原问题',createdAt:1,...scope},
                  {id:'a',role:'assistant',content:'已完成的历史答案',question:'原问题',createdAt:2,...scope}];
                await saveCurrentSession();
                const old=await sessionStore.createSession('history-a',{title:'English Notes · 第二节 <img src=x onerror=alert(1)> 长标题'.repeat(3)});
                old.messages=[{id:'old',role:'assistant',content:'无草稿的旧对话',question:'旧问题',mode:'all',documentId:null}];
                await sessionStore.saveSession(old);
                const foreign=await sessionStore.createSession('history-b',{title:'其他空间私有对话'});
                const index=(await chrome.storage.local.get(STORAGE_KEYS.SESSIONS))[STORAGE_KEYS.SESSIONS];
                index[foreign.sessionId].pluginId='history-a';
                index.legacy={pluginId:'history-a',title:'旧版无归属数据'};
                await chrome.storage.local.set({[STORAGE_KEYS.SESSIONS]:index,[STORAGE_KEYS.SESSION_PREFIX+'legacy']:{sessionId:'legacy',messages:[]}});
                renderChat();updateSendState();
                return {first:session.sessionId,old:old.sessionId,foreign:foreign.sessionId};
            }""")
            history(page)
            expect(page.locator(".chat-session-row")).to_have_count(2)
            expect(row(page, ids["first"])).to_have_attribute("data-current", "true")
            assert page.locator("#chat-session-list img").count() == 0
            expect(page.locator("#chat-session-list")).not_to_contain_text("其他空间私有对话")
            expect(page.locator("#chat-session-list")).not_to_contain_text("旧版无归属数据")
            assert page.evaluate("document.documentElement.scrollWidth<=document.documentElement.clientWidth+1")
            page.screenshot(path=str(args.artifacts / "session-list.png"), full_page=True)
            assert not calls
            passed("History lists completed conversations without drafts, marks the current one, renders titles as text and excludes foreign/legacy records at 320px")

            page.locator("#chat-textarea").fill("这条草稿先保留\nKeep English terms")
            draft_saved(page)
            choose(page, ids["old"])
            expect(page.locator("#chat-area")).to_contain_text("无草稿的旧对话")
            expect(page.locator("#chat-textarea")).to_have_value("")
            choose(page, ids["first"])
            expect(page.locator("#chat-textarea")).to_have_value("这条草稿先保留\nKeep English terms")
            expect(page.locator("#chat-replay-context")).to_contain_text("原文档 A")
            page.reload(); page.wait_for_load_state("networkidle"); page.locator("#nav-chat").click()
            assert page.evaluate("session.sessionId") == ids["first"]
            expect(page.locator("#chat-textarea")).to_have_value("这条草稿先保留\nKeep English terms")
            assert not calls
            passed("Switching saves drafts, restores their original document on return, and preserves the selected session after reload without model calls")

            page.evaluate("""() => {
                globalThis.originalSet=chrome.storage.local.set.bind(chrome.storage.local);
                chrome.storage.local.set=items=>Object.keys(items).some(key=>key.startsWith(STORAGE_KEYS.DRAFT_PREFIX))
                  ? Promise.reject(new Error('fixture draft quota')):originalSet(items);
            }""")
            page.locator("#chat-textarea").fill("保存失败不能带走我的输入")
            expect(page.locator("#chat-draft-status")).to_contain_text("未保存")
            history(page)
            row(page, ids["old"]).locator(".chat-session-open").click()
            page.wait_for_function("() => !els.chatTextarea.readOnly")
            assert page.evaluate("session.sessionId") == ids["first"]
            expect(page.locator("#chat-textarea")).to_have_value("保存失败不能带走我的输入")
            page.evaluate("() => {chrome.storage.local.set=originalSet}")
            choose(page, ids["old"])
            page.locator("#scope-all").click()
            page.locator("#chat-textarea").fill("准备删除的旧会话草稿")
            draft_saved(page)
            choose(page, ids["first"])
            passed("A failed draft write blocks conversation switching and retains input; retrying after storage recovery succeeds")

            history(page)
            row(page, ids["old"]).locator(".chat-session-delete").click()
            row(page, ids["old"]).locator(".session-delete-cancel").click()
            assert page.evaluate("id=>sessionStore.getSession(id)", ids["old"])
            page.evaluate("""() => {
                chrome.storage.local.set=items=>Object.entries(items).some(([key,value])=>key.startsWith(STORAGE_KEYS.SESSION_PREFIX)&&value===null)
                  ? Promise.reject(new Error('fixture delete failure')):originalSet(items);
            }""")
            delete(page, ids["old"])
            expect(page.locator("#chat-sessions-status")).to_contain_text("删除失败")
            assert page.evaluate("id=>sessionStore.getSession(id)", ids["old"])
            assert page.evaluate("() => questionDraftController().list().then(items=>items.length)") == 2
            page.evaluate("() => {chrome.storage.local.set=originalSet}")
            row(page, ids["old"]).locator(".session-delete-confirm").click()
            expect(row(page, ids["old"])).to_have_count(0)
            assert page.evaluate("session.sessionId") == ids["first"]
            assert not page.evaluate("id=>sessionStore.getSession(id)", ids["old"])
            assert not page.evaluate("""async id=>(await chrome.storage.local.get(STORAGE_KEYS.DRAFT_PREFIX+id))[STORAGE_KEYS.DRAFT_PREFIX+id]""", ids["old"])
            assert not calls
            passed("Delete cancellation is harmless; failed deletion keeps messages/draft/pointer; successful deletion removes only that conversation and draft")

            page.locator("#chat-sessions-toggle").click()
            page.locator("#scope-all").click()
            page.locator("#chat-textarea").fill("保存成功后才允许发送的问题")
            draft_saved(page)
            original_count = page.evaluate("session.messages.length")
            page.evaluate("""() => {
                chrome.storage.local.set=items=>Object.entries(items).some(([key,value])=>key.startsWith(STORAGE_KEYS.SESSION_PREFIX)&&value!==null)
                  ? Promise.reject(new Error('fixture message quota')):originalSet(items);
            }""")
            page.locator("#chat-send").click()
            page.wait_for_function("() => !isSending")
            expect(page.locator("#chat-textarea")).to_have_value("保存成功后才允许发送的问题")
            expect(page.locator("#chat-replay-notice")).to_contain_text("尚未发送")
            assert page.evaluate("session.messages.length") == original_count
            assert not calls
            page.evaluate("() => {chrome.storage.local.set=originalSet}")
            passed("A failed initial message save restores the original question and scope and stops before any model request")

            page.locator("#chat-send").click(); request_wait(page)
            delete(page, ids["first"])
            page.wait_for_function("id=>session && session.sessionId!==id && !els.chatTextarea.readOnly", arg=ids["first"])
            fresh = page.evaluate("session.sessionId")
            expect(page.locator("#chat-empty")).to_be_visible()
            waiting.pop().fulfill(json={"answer": "应丢弃的迟到回答", "sources": []})
            page.wait_for_function("() => !isSending")
            expect(page.locator("#chat-area .msg.assistant")).to_have_count(0)
            assert not page.evaluate("id=>sessionStore.getSession(id)", ids["first"])
            assert page.evaluate("() => sessionStore.getSessionsByPlugin('history-a').then(items=>items.length)") == 1
            assert len(calls) == 1
            passed("Deleting the only current conversation creates an empty replacement; a delayed answer cannot resurrect the deleted conversation")

            other = panel(); other.locator("#nav-chat").click()
            assert other.evaluate("session.sessionId") == fresh
            page.evaluate("""async () => {
                const base=await sessionStore.getSession(session.sessionId);
                globalThis.releaseWrite=null;
                globalThis.writeGate=new Promise(resolve=>{globalThis.releaseWrite=resolve});
                globalThis.gatedSet=chrome.storage.local.set.bind(chrome.storage.local);
                chrome.storage.local.set=async items=>{if(items[STORAGE_KEYS.SESSION_PREFIX+base.sessionId])await writeGate;return gatedSet(items)};
                globalThis.leftSave=sessionStore.saveSession({...base,messages:[{id:'left',role:'user',content:'面板一',createdAt:1,mode:'all'}]});
            }""")
            other.evaluate("""async () => {
                const base=await sessionStore.getSession(session.sessionId);
                globalThis.rightSave=sessionStore.saveSession({...base,messages:[{id:'right',role:'assistant',content:'面板二',createdAt:2,mode:'all'}]});
            }""")
            page.evaluate("() => {releaseWrite()}")
            assert page.evaluate("() => leftSave")
            assert other.evaluate("() => rightSave")
            page.evaluate("() => {chrome.storage.local.set=gatedSet}")
            assert page.evaluate("async () => (await sessionStore.getSession(session.sessionId)).messages.map(m=>m.id)") == ["left", "right"]
            passed("Two actual extension pages serialize stale concurrent saves through Web Locks and retain both message additions")

            fallback = page.evaluate("async () => (await sessionStore.createSession('history-a',{title:'删除后的保留会话'})).sessionId")
            page.locator("#scope-all").click()
            page.locator("#chat-textarea").fill("跨面板删除期间的请求")
            page.locator("#chat-send").click(); request_wait(page)
            delete(other, fresh)
            page.wait_for_function("id=>session && session.sessionId===id && !els.chatTextarea.readOnly", arg=fallback)
            other.wait_for_function("id=>session && session.sessionId===id && !els.chatTextarea.readOnly", arg=fallback)
            waiting.pop().fulfill(json={"answer": "其他面板删除后也不得回写", "sources": []})
            page.wait_for_function("() => !isSending")
            assert not page.evaluate("id=>sessionStore.getSession(id)", fresh)
            assert page.evaluate("session.messages.length") == 0
            assert len(calls) == 2
            other.close()
            passed("Deleting a conversation in another panel updates the waiting panel, switches both to a valid conversation and discards the late model reply")

            page.evaluate("""() => {
                globalThis.oldGetSessions=sessionStore.getSessionsByPlugin;
                globalThis.historyGate=new Promise(resolve=>{globalThis.releaseHistory=resolve});
                sessionStore.getSessionsByPlugin=async id=>{if(id==='history-a')await historyGate;return oldGetSessions(id)};
            }""")
            page.locator("#chat-sessions-toggle").click()
            select_owner(page, "b")
            page.evaluate("() => {releaseHistory();sessionStore.getSessionsByPlugin=oldGetSessions}")
            history(page)
            expect(page.locator("#chat-session-list")).not_to_contain_text("删除后的保留会话")
            assert page.evaluate("session.pluginId") == "history-b"
            passed("A delayed history read from the old Workspace cannot populate the new Workspace list or select its conversations")

            page.evaluate("() => {sessionStore.getSessionsByPlugin=async()=>{throw new Error('fixture read failure')}}")
            page.locator("#chat-sessions-refresh").click()
            expect(page.locator("#chat-sessions-status")).to_contain_text("读取失败")
            page.evaluate("() => {sessionStore.getSessionsByPlugin=oldGetSessions}")
            page.locator("#chat-sessions-refresh").click()
            expect(page.locator("#chat-sessions-status")).to_contain_text("共")
            page.locator("#chat-sessions-refresh").press("Escape")
            expect(page.locator("#chat-sessions")).to_be_hidden()
            expect(page.locator("#chat-sessions-toggle")).to_be_focused()
            assert not errors, errors
            assert len(calls) == 2
            passed("History read errors support refresh retry; Escape closes the list and restores focus; no JavaScript errors or automatic model calls")
            (args.artifacts / "report.json").write_text(json.dumps({"passed": True, "scope": __doc__, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            if page and not page.is_closed():
                page.screenshot(path=str(args.artifacts / "failure.png"), full_page=True)
            raise
        finally:
            context.close()


if __name__ == "__main__":
    main()

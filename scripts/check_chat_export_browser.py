"""Real Chrome Markdown downloads and export isolation; controlled HTTP, no user database or paid model calls."""
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
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/chat-export-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, requests, waiting, downloads, errors = [], [], [], [], []

    def route_api(route):
        endpoint = route.request.url.split(":18001", 1)[1].split("?", 1)[0]
        requests.append(endpoint)
        if endpoint == "/plugins/me":
            owner = route.request.headers.get("x-plugin-id", "export-a")
            route.fulfill(json={"plugin_id": owner, "plugin_name": owner, "api_key_configured": True})
        elif endpoint == "/model-providers":
            route.fulfill(json={"request_timeouts_ms": {"ask": 30000}})
        elif endpoint == "/documents":
            route.fulfill(json={"items": [], "total": 0, "page": 1, "page_size": 10})
        elif endpoint == "/rag/ask":
            waiting.append(route)
        else:
            route.fulfill(status=404, json={"detail": "fixture endpoint unavailable"})

    def passed(value):
        checks.append(value)
        print(value, flush=True)

    with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            accept_downloads=True, viewport={"width": 320, "height": 850},
            ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"])
        context.route("http://localhost:18001/**", route_api)
        page = None
        try:
            eid = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            page = context.new_page()
            page.on("download", lambda download: downloads.append(download.suggested_filename))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("chrome-extension://" + eid + "/sidepanel.html")
            page.wait_for_load_state("networkidle")

            def select_owner(name="a", secret=None):
                page.evaluate("""async ({name,secret}) => {
                    webRagApiClient.setPluginDetails({pluginId:'export-'+name,pluginSecret:secret||('secret-'+name),pluginName:'export-'+name});
                    await webRagApiClient.persistPlugin();await validatePlugin();renderChat();updateSendState();
                }""", {"name": name, "secret": secret})
                page.locator("#nav-chat").click()

            def download_file(name, trigger=None):
                with page.expect_download() as event:
                    trigger() if trigger else page.locator("#chat-export").click()
                download = event.value
                assert download.failure() is None
                assert download.suggested_filename.endswith(".md")
                assert not any(char in download.suggested_filename for char in '/\\<>:"|?*')
                path = args.artifacts / name
                download.save_as(path)
                return path.read_text(encoding="utf-8"), download.suggested_filename

            def gate_read():
                page.evaluate("""() => {
                    globalThis.exportRealGet=sessionStore.getSession;
                    globalThis.releaseExportRead=null;
                    globalThis.exportGate=new Promise(resolve=>{globalThis.releaseExportRead=resolve;setTimeout(resolve,10000)});
                    const id=session.sessionId;
                    sessionStore.getSession=async sid=>{const value=await exportRealGet(sid);if(sid===id)await exportGate;return value};
                }""")

            def release_read():
                page.evaluate("() => {releaseExportRead();sessionStore.getSession=exportRealGet}")

            def clear_draft():
                page.locator("#chat-textarea").fill("")
                page.evaluate("() => questionDraftController().flush()")

            select_owner()
            expect(page.locator("#chat-export")).to_be_disabled()
            first_id = page.evaluate(r"""async () => {
                session.title='../CON 中文 / English 📝 <script>test</script>';
                const scope={mode:'current',documentId:77,pageTitle:'原资料 A',pageUrl:'https://example.com/a?chapter=1'};
                session.messages=[{id:'u1',role:'user',content:'请解释这段代码。\nKeep English terms',createdAt:1,...scope},
                  {id:'a1',role:'assistant',content:'## 中文要点\n\n**English term**\n\n```python\nprint("你好")\n```',question:'请解释这段代码。\nKeep English terms',replyTo:'u1',createdAt:2,...scope,
                   sources:[{document_id:77,content_revision:2,title:'资料 [A]',url:'https://example.com/a?q=(test)',score:.9,api_key:'SECRET_FIELD_SENTINEL'},
                     {title:'不可导出链接',url:'javascript:alert(1)'}],plugin_secret:'AUTH_FIELD_SENTINEL'}];
                await saveCurrentSession();renderChat();updateSendState();return session.sessionId;
            }""")
            page.locator("#chat-textarea").fill("UNSENT_DRAFT_ONLY：这段不要导出")
            page.evaluate("() => questionDraftController().flush()")
            baseline = page.evaluate("async()=>JSON.stringify(await sessionStore.getSession(session.sessionId))")
            before = len(requests)
            content, filename = download_file("basic.md")
            assert "请解释这段代码" in content and "English term" in content
            assert '> ```python\n> print("你好")\n> ```' in content
            assert "单篇文档 · 原资料 A" in content and "记录版本 2" in content
            assert "https://example.com/a?q=(test)" in content and "未重新联网核对" in content
            assert all(value not in content for value in ["UNSENT_DRAFT_ONLY", "AUTH_FIELD_SENTINEL", "SECRET_FIELD_SENTINEL", "javascript:", "secret-a"])
            assert "中文" in filename and "📝" in filename
            assert page.evaluate("async()=>JSON.stringify(await sessionStore.getSession(session.sessionId))") == baseline
            expect(page.locator("#chat-textarea")).to_have_value("UNSENT_DRAFT_ONLY：这段不要导出")
            assert len(requests) == before
            assert page.evaluate("document.documentElement.scrollWidth<=document.documentElement.clientWidth+1")
            page.screenshot(path=str(args.artifacts / "export-toolbar.png"), full_page=True)
            passed("Actual UTF-8 Markdown download preserves Chinese/English, code, scope and historical sources; omits drafts/credential fields, leaves storage unchanged and adds no requests")

            clear_draft()
            gate_read()
            page.locator("#chat-export").click()
            expect(page.locator("#chat-export")).to_be_disabled()
            page.evaluate("""() => {
                session.messages[1].content='AFTER_CLICK_MUTATION';session.messages[1].sources[0].title='AFTER_CLICK_SOURCE';
                exportChatSession();exportChatSession();
            }""")
            count = len(downloads)
            frozen, _ = download_file("frozen.md", release_read)
            assert "中文要点" in frozen and "AFTER_CLICK" not in frozen
            assert len(downloads) == count + 1
            page.evaluate("async()=>{session=await sessionStore.getSession(session.sessionId);renderChat();updateSendState()}")
            passed("Text and source snapshots freeze at click time; edits during delayed storage reads do not alter the file and repeated clicks produce one download")

            page.evaluate("""() => {
                session.messages.push({id:'volatile',role:'assistant',content:'UNSAVED_ANSWER：请保留这份完整回答',mode:'all',question:'旧问题'});
                renderChat();updateSendState();
            }""")
            volatile, _ = download_file("unsaved.md")
            assert "UNSAVED_ANSWER" in volatile and "尚未保存" in volatile
            assert page.evaluate("async()=>(await sessionStore.getSession(session.sessionId)).messages.length") == 2
            passed("An answer kept only in panel memory can be exported without writing it to storage; the file explicitly marks unsaved messages")

            page.locator("#new-session-btn").click()
            page.wait_for_function("() => !els.chatTextarea.readOnly")
            page.locator("#scope-all").click()
            page.locator("#chat-textarea").fill("请求仍在生成时导出")
            page.locator("#chat-send").click()
            deadline = time.monotonic() + 5
            while not waiting and time.monotonic() < deadline:
                page.wait_for_timeout(20)
            assert waiting
            pending, _ = download_file("pending.md")
            assert "生成中" in pending and "请求仍在生成时导出" in pending
            waiting.pop().fulfill(json={"answer": "LATE_ANSWER：导出之后才返回的回答", "sources": []})
            page.wait_for_function("() => !isSending")
            assert "LATE_ANSWER" not in (args.artifacts / "pending.md").read_text(encoding="utf-8")
            completed, _ = download_file("completed.md")
            assert "LATE_ANSWER" in completed and "仍有回答生成中" not in completed
            passed("Exporting a pending question is enabled and marked incomplete; later answers do not rewrite an existing file and appear in a subsequent export")

            page.locator("#chat-textarea").fill("失败测试期间保留草稿")
            page.evaluate("() => questionDraftController().flush()")
            count = len(downloads)
            page.evaluate("() => {globalThis.getBeforeFailure=sessionStore.getSession;sessionStore.getSession=async()=>{throw new Error('fixture read failure')}}")
            page.locator("#chat-export").click()
            expect(page.locator("#chat-export-status")).to_contain_text("导出失败")
            expect(page.locator("#chat-export")).to_be_enabled()
            page.evaluate("() => {sessionStore.getSession=getBeforeFailure;globalThis.realCreateUrl=URL.createObjectURL;URL.createObjectURL=()=>{throw new Error('fixture Blob failure')}}")
            page.locator("#chat-export").click()
            expect(page.locator("#chat-export-status")).to_contain_text("导出失败")
            assert len(downloads) == count
            expect(page.locator("#chat-textarea")).to_have_value("失败测试期间保留草稿")
            page.evaluate("() => {URL.createObjectURL=realCreateUrl}")
            download_file("retried.md")
            clear_draft()
            passed("Storage and Blob URL failures preserve conversation/draft, release the busy state, show an error and allow a successful retry")

            page.evaluate("id=>openChatSession(id)", first_id)
            second_id = page.evaluate("""async()=>{
                const target=await sessionStore.createSession('export-a',{title:'另一段对话'});
                target.messages=[{id:'second-answer',role:'assistant',content:'SECOND_SESSION_ONLY',mode:'all'}];
                await sessionStore.saveSession(target);return target.sessionId;
            }""")
            gate_read(); count = len(downloads)
            page.locator("#chat-export").click()
            page.locator("#chat-sessions-toggle").click()
            page.locator(f'.chat-session-row[data-session-id="{second_id}"] .chat-session-open').click()
            page.wait_for_function("id=>session.sessionId===id", arg=second_id)
            expect(page.locator("#chat-export-status")).to_be_hidden()
            assert len(downloads) == count
            second, _ = download_file("second-session.md")
            assert "SECOND_SESSION_ONLY" in second and "中文要点" not in second
            second_status = page.locator("#chat-export-status").text_content()
            release_read()
            expect(page.locator("#chat-export-status")).to_have_text(second_status)
            assert len(downloads) == count + 1
            passed("Switching conversations cancels a delayed export; the next export contains only the newly selected conversation")

            gate_read(); count = len(downloads)
            page.locator("#chat-export").click()
            select_owner("b")
            release_read()
            expect(page.locator("#chat-export")).to_be_disabled()
            assert len(downloads) == count
            page.evaluate("""async()=>{
                session.messages=[{id:'workspace-b',role:'assistant',content:'WORKSPACE_B_ONLY',mode:'all'}];
                await saveCurrentSession();renderChat();updateSendState();
            }""")
            other, _ = download_file("workspace-b.md")
            assert "WORKSPACE_B_ONLY" in other and "SECOND_SESSION_ONLY" not in other
            gate_read(); count = len(downloads)
            page.locator("#chat-export").click()
            page.evaluate("""async()=>{
                webRagApiClient.setPluginDetails({pluginSecret:'rotated-secret-b'});
                await webRagApiClient.persistPlugin();updateSendState();
            }""")
            release_read()
            expect(page.locator("#chat-export-status")).to_be_hidden()
            assert len(downloads) == count
            passed("Workspace changes and credential rotation invalidate delayed exports, clear old status and prevent cross-identity downloads")

            page.evaluate("() => {globalThis.validGet=sessionStore.getSession;sessionStore.getSession=async id=>({...await validGet(id),pluginId:'foreign'})}")
            count = len(downloads)
            page.locator("#chat-export").click()
            expect(page.locator("#chat-export-status")).to_contain_text("无法导出")
            assert len(downloads) == count
            page.evaluate("() => {sessionStore.getSession=validGet}")
            gate_read(); count = len(downloads)
            page.locator("#chat-export").click()
            deleting = page.evaluate("session.sessionId")
            page.locator("#chat-sessions-toggle").click()
            row = page.locator(f'.chat-session-row[data-session-id="{deleting}"]')
            row.locator(".chat-session-delete").click(); row.locator(".session-delete-confirm").click()
            page.wait_for_function("id=>session&&session.sessionId!==id", arg=deleting)
            release_read()
            expect(page.locator("#chat-export")).to_be_disabled()
            assert len(downloads) == count
            assert requests.count("/rag/ask") == 1 and "/rag/sources/check" not in requests
            assert not errors, errors
            passed("Foreign ownership and real deletion reject export even when a delayed read returns an old copy; no JavaScript errors, extra model calls or source rechecks")

            (args.artifacts / "report.json").write_text(json.dumps({"passed": True, "scope": __doc__, "checks": checks, "download_count": len(downloads)}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            if page and not page.is_closed():
                page.screenshot(path=str(args.artifacts / "failure.png"), full_page=True)
            raise
        finally:
            context.close()


if __name__ == "__main__":
    main()

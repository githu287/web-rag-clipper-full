"""Real extension pagination against local fixtures; no backend/model/database.

Owns a dynamic fixture server to test redirects, slow responses and request logs.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/pagination-browser")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    checks, requests = [], []
    passed = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            case, number = query.get("case", ["normal"])[0], int(query.get("page", ["1"])[0])
            if parsed.path.startswith("/marked_"):
                number = int(parsed.path.split("_")[1].split(".")[0])
            requests.append({"case": case, "page": number, "path": parsed.path})
            if parsed.path == "/image.png":
                data = (ROOT / "extension/tests/fixtures/ocr/bilingual.png").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(data)
                return
            if case == "slow" and number > 1:
                time.sleep(3)
            if case == "redirect" and number == 2:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            total = 12 if case in ["limit", "numeric-marker-limit"] else 3
            target = "/story?" + urlencode({"case": case, "page": number + 1})
            if case == "numeric-marker-path": target = f"/marked_{number+1}.html?case={case}"
            title = "分页文章测试"
            text_number = 1 if case == "repeat" else number
            body_text = "CONTENT_PAGE_" + str(text_number) + "。分页正文保持清晰的页面顺序，不应混入导航、其他文章或重复窗口。" * 8
            if case == "different" and number > 1:
                title = "这是另一篇完全不同的文章"
            if case == "size" and number > 1:
                body_text = "长正文" * 180000
            links = f'<link rel="next" href="{html.escape(target)}">' if number < total else ""
            pager = f'<nav class="pagination"><a href="{html.escape(target)}">下一页</a></nav>' if number < total else ""
            if case == "foreign":
                links = '<link rel="next" href="https://example.invalid/story?page=2">'
                pager = ""
            if case == "next-post":
                pager = f'<nav class="pagination"><a rel="next" href="{html.escape(target)}">下一篇</a></nav>'
            if case == "ambiguous":
                links = ""
                pager = '<nav class="pagination"><a href="?case=ambiguous&page=2">下一页</a><a href="?case=ambiguous&pg=2">Next page</a></nav>'
            if case == "hidden":
                links = ""
                pager = f'<nav class="pagination" style="display:none"><a href="{html.escape(target)}">下一页</a></nav>'
            if case == "plain-next":
                links = ""
                pager = f'<div class="unknown-site-pages"><a href="{html.escape(target)}">下一页 &gt;&gt;</a></div>' if number < total else ""
            if case == "canonical":
                title = "分页文章标题" + str(number)
                links += '<link rel="canonical" href="/story?case=canonical&amp;page=1">'
            if case == "square-title":
                title = "分页文章测试" + ("【" + str(number) + "】" if number > 1 else "")
            if case.startswith("numeric"):
                links = ""
                current = "/story?" + urlencode({"case": case, "page": number})
                current_link = f'<a href="{html.escape(current)}">{number}</a>'
                next_link = f'<a href="{html.escape(target)}">{number + 1}</a>' if number < total else ""
                if case == "numeric-missing-current": current_link = ""
                if case == "numeric-hidden-current": current_link = current_link.replace("<a ", '<a hidden ')
                if case == "numeric-label-mismatch": next_link = next_link.replace(f'>{number+1}<', f'>{number+2}<')
                if case == "numeric-hidden": next_link = next_link.replace("<a ", '<a hidden ')
                if case == "numeric-disabled": next_link = next_link.replace("<a ", '<a aria-disabled="true" ')
                if case == "numeric-conflict": next_link += f'<a href="?case={case}&pg={number+1}">{number+1}</a>'
                if case == "numeric-current-conflict": current_link += f'<a href="{html.escape(current)}">{number+3}</a>'
                if case == "numeric-foreign": next_link = f'<a href="https://example.invalid/story?page={number+1}">{number+1}</a>'
                if case.startswith("numeric-marker"):
                    current_link = f'<span aria-current="page">{number}</span>'
                    if case == "numeric-marker-current": current_link = f'<span class="current"><b>{number}</b></span>'
                    if case == "numeric-marker-active": current_link = f'<em class="active">{number}</em>'
                    if case == "numeric-marker-list": current_link = f'<ul><li aria-current="page"><span>{number}</span></li></ul>'
                    if case == "numeric-marker-placeholder": current_link = f'<a href="#" aria-current="page">{number}</a>'
                    if case == "numeric-marker-plain": current_link = f'<span>{number}</span>'
                    if case == "numeric-marker-hidden": current_link = f'<span hidden aria-current="page">{number}</span>'
                    if case == "numeric-marker-disabled": current_link = f'<span aria-disabled="true" aria-current="page">{number}</span>'
                    if case == "numeric-marker-native-disabled": current_link = f'<button disabled aria-current="page">{number}</button>'
                    if case == "numeric-marker-false": current_link = f'<span class="current" aria-current="false">{number}</span>'
                    if case == "numeric-marker-conflict": current_link += f'<em class="current">{number+2}</em>'
                    if case == "numeric-marker-link-conflict": current_link += f'<a href="{html.escape(current)}">{number+2}</a>'
                    if case == "numeric-marker-wrong-url": current_link = f'<a href="{html.escape(target)}"><span class="current">{number}</span></a>'
                    if case == "numeric-marker-nested-pager": current_link = f'<div class="pager">{current_link}</div>'
                    if case == "numeric-marker-comment": current_link = f'<div class="comments">{current_link}</div>'
                    if case == "numeric-marker-stale":
                        current_link = '<span aria-current="page">9</span>'
                        next_link = f'<a href="{html.escape(target)}">10</a>'
                    if case == "numeric-marker-jump": next_link = f'<a href="?case={case}&page={number+2}">{number+1}</a>'
                    if case == "numeric-marker-foreign": next_link = f'<a href="https://example.invalid/story?page={number+1}">{number+1}</a>'
                if case == "numeric-unscoped": pager = current_link + next_link
                else: pager = f'<nav class="page_n">{current_link}{next_link}<a href="{html.escape(target)}" hidden>下一页</a></nav>'
            if case == "suffix":
                target = "/article_" + str(number + 1) + ".html?" + urlencode({"case": case, "page": number + 1})
                # Suffix pagination keeps non-pagination query values unchanged.
                target = target.replace("&page=" + str(number + 1), "")
                links = f'<link rel="next" href="{target}">' if number < total else ""
                pager = ""
            if parsed.path.startswith("/article_"):
                number = int(parsed.path.split("_")[1].split(".")[0])
                body_text = "CONTENT_PAGE_" + str(number) + "。后缀分页正文。" * 30
                links = f'<link rel="next" href="/article_{number+1}.html?case=suffix">' if number < 3 else ""
            if case == "disabled":
                links = ""
                pager = f'<nav class="pagination"><a aria-disabled="true" href="{html.escape(target)}">下一页</a></nav>'
            status = 200
            if case == "blocked" and number > 1:
                status, title, body_text, links, pager = 403, "403 Forbidden", "Access denied", "", ""
            image_number = 1 if case == "repeat" else number
            data = (f'<!doctype html><meta charset="utf-8"><title>{title}（{number}）</title>{links}'
                    f'<article><h1>{title}</h1><p>{body_text}</p><img src="/image.png?page={image_number}" alt="分页图片{image_number}" width="600"></article>{pager}').encode()
            if status == 403:
                data = b'<!doctype html><title>403 Forbidden</title><h1>403 Forbidden</h1><p>Access denied</p>'
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass

        def log_message(self, *_args): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def done(text):
        checks.append(text)
        print(text, flush=True)

    def wait_js(page, expression, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.evaluate(expression): return
            time.sleep(.1)
        raise TimeoutError(expression)

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
                def navigate(case):
                    source.goto(base + "/story?" + urlencode({"case":case,"page":1}))
                    source.wait_for_load_state("networkidle")
                    source.locator("article h1").wait_for(state="visible")
                    return driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                def begin(tab, limits=None):
                    driver.evaluate("""data=>{
                      globalThis.paginationResult=null;globalThis.paginationError=null;globalThis.paginationProgress=[];
                      globalThis.paginationAbort=new AbortController();
                      (async()=>{const first=await webRagPageCapture.capture(data.tab,{collectPagination:true,collectImages:true,signal:paginationAbort.signal});
                      return webRagPaginationCapture.expand(data.tab,first,{signal:paginationAbort.signal,collectImages:true,limits:data.limits,onProgress:p=>paginationProgress.push(p)});})()
                      .then(r=>paginationResult=r,e=>paginationError=e.message);
                    }""", {"tab":tab,"limits":limits})
                def result(timeout=40):
                    wait_js(driver,"paginationResult!==null||paginationError!==null",timeout)
                    assert driver.evaluate("paginationError") is None, driver.evaluate("paginationError")
                    value = driver.evaluate("paginationResult")
                    wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>!(v.webRagPaginationTabs||[]).length)")
                    return value
                tab = navigate("normal")
                before = len(requests)
                plain = driver.evaluate("async id=>webRagPageCapture.capture(id,{})",tab)
                assert plain["pagination"] is None and not any(item["page"]>1 for item in requests[before:])
                done("Default capture never follows pagination")
                original_url = source.url
                source.evaluate("scrollTo(0,80)")
                original_scroll = source.evaluate("scrollY")
                begin(tab)
                combined = result()
                assert combined["diagnostics"]["pagination_page_count"] == 3, combined
                assert combined["diagnostics"]["pagination_stop_reason"] == "complete"
                assert len(combined["images"]) == 3
                assert combined["url"] == original_url and source.url == original_url
                assert source.evaluate("scrollY") == original_scroll
                assert [combined["raw_text"].index(f"CONTENT_PAGE_{n}") for n in (1,2,3)] == sorted(combined["raw_text"].index(f"CONTENT_PAGE_{n}") for n in (1,2,3))
                assert combined["raw_text"].count("分页来源（第 ") == 3
                done("Three real rendered pages merge in order with sources and images, retaining original URL and scroll position")

                for case in ["plain-next","canonical","square-title","numeric"]:
                    tab=navigate(case);begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"]==3,(case,value)
                source.goto(base+"/article.html?case=suffix")
                tab=driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id",source.url)
                begin(tab);value=result()
                assert value["diagnostics"]["pagination_page_count"]==3,value
                done("Unknown pager classes, canonical identity and article_2.html suffix pages are supported")

                source.goto(base+"/story?case=numeric&page=2")
                tab=driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id",source.url)
                before=len(requests);begin(tab);value=result()
                assert value["diagnostics"]["pagination_page_count"]==2 and value["url"]==source.url,value
                assert not any(item["page"]==1 and item["path"]=="/story" for item in requests[before:])
                for case, expected in [("numeric-missing-current","complete"),("numeric-hidden-current","complete"),
                    ("numeric-label-mismatch","complete"),("numeric-hidden","complete"),("numeric-disabled","complete"),
                    ("numeric-unscoped","complete"),("numeric-current-conflict","complete"),
                    ("numeric-foreign","unsupported"),("numeric-conflict","ambiguous")]:
                    tab=navigate(case);before=len(requests);begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"]==1,(case,value)
                    assert value["diagnostics"]["pagination_stop_reason"]==expected,(case,value)
                    assert not any(item["page"]>1 for item in requests[before:]),case
                done("Visible numeric pagers follow consecutive pages from a middle page; missing, hidden, disabled, conflicting, foreign and unscoped hints are rejected")

                for case in ["numeric-marker", "numeric-marker-current", "numeric-marker-active", "numeric-marker-placeholder", "numeric-marker-list"]:
                    tab=navigate(case);before_html=source.content();begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"]==3,(case,value)
                    assert value["diagnostics"]["pagination_stop_reason"]=="complete",(case,value)
                    assert source.content()==before_html,case
                source.goto(base+"/marked_2.html?case=numeric-marker-path")
                tab=driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id",source.url)
                before=len(requests);begin(tab);value=result()
                assert value["diagnostics"]["pagination_page_count"]==2,value
                assert not any(item["path"]=="/marked_1.html" for item in requests[before:])
                for suffix, expected in [("plain","complete"),("hidden","complete"),("disabled","complete"),("native-disabled","complete"),
                    ("false","complete"),("conflict","complete"),("link-conflict","complete"),
                    ("wrong-url","complete"),("nested-pager","complete"),("comment","complete"),
                    ("stale","complete"),("jump","complete"),("foreign","unsupported")]:
                    case="numeric-marker-"+suffix
                    tab=navigate(case);before=len(requests);begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"]==1,(case,value)
                    assert value["diagnostics"]["pagination_stop_reason"]==expected,(case,value)
                    assert not any(item["page"]>1 for item in requests[before:]),case
                tab=navigate("numeric-marker-limit");begin(tab);value=result()
                assert value["diagnostics"]["pagination_page_count"]==5 and value["diagnostics"]["pagination_stop_reason"]=="page_limit",value
                done("Non-link current markers: ARIA, current/active classes, placeholder anchors, middle suffix pages and five-page cap pass; stale, conflicting, nested, hidden, disabled and unrelated markers are rejected without DOM mutation")

                source.goto(base+"/story?case=normal&page=2")
                tab=driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id",source.url)
                before=len(requests);begin(tab);value=result()
                assert value["diagnostics"]["pagination_page_count"]==2 and value["url"]==source.url,value
                assert not any(item["page"]==1 and item["path"]=="/story" for item in requests[before:])
                done("Starting at a middle page follows forward only and retains that page as the document URL")

                for case, expected in [("foreign","unsupported"),("next-post","complete"),("ambiguous","ambiguous"),("hidden","complete"),("disabled","complete")]:
                    tab = navigate(case); before=len(requests);begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"] == 1, (case,value)
                    assert value["diagnostics"]["pagination_stop_reason"] == expected,(case,value)
                    assert not any(item["page"]>1 for item in requests[before:]),case
                done("Foreign origin, next article, ambiguous, hidden and disabled links are not followed")

                for case, expected in [("blocked","page_failed"),("redirect","page_failed"),("different","different_article"),("repeat","repeated_content")]:
                    tab=navigate(case);begin(tab);value=result()
                    assert value["diagnostics"]["pagination_page_count"]==1,(case,value)
                    assert value["diagnostics"]["pagination_stop_reason"]==expected,(case,value)
                    assert "pagination_incomplete" in value["diagnostics"]["quality_warnings"]
                done("Access barriers, redirects, different articles and repeated page content retain the first page with warnings")

                tab=navigate("limit");begin(tab,{"maxPages":100,"maxMs":1000000});value=result(55)
                assert value["diagnostics"]["pagination_page_count"]==5 and value["diagnostics"]["pagination_stop_reason"]=="page_limit"
                done("Caller limits are clamped to five total pages")
                tab=navigate("size");begin(tab);value=result()
                assert len(value["raw_text"])==500000 and value["diagnostics"]["truncated"]
                assert value["diagnostics"]["pagination_stop_reason"]=="size_limit"
                done("Combined content remains within the shared 500000-character limit")
                tab=navigate("slow");begin(tab,{"maxMs":100});value=result()
                assert value["diagnostics"]["pagination_stop_reason"]=="time_limit",value
                done("Pagination deadline returns completed content and cleans up the loading tab")

                tab=navigate("slow");begin(tab)
                wait_js(driver,"paginationProgress.length>0")
                driver.evaluate("paginationAbort.abort()")
                wait_js(driver,"paginationError!==null")
                assert "取消" in driver.evaluate("paginationError")
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>!(v.webRagPaginationTabs||[]).length)")
                done("User cancellation rejects partial replacement and removes temporary tabs")

                tab=navigate("slow");begin(tab)
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>(v.webRagPaginationTabs||[]).length>0)")
                source.evaluate("history.pushState({},'', '/different-route')")
                wait_js(driver,"paginationError!==null")
                assert "页面已变化" in driver.evaluate("paginationError")
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>!(v.webRagPaginationTabs||[]).length)")
                done("Source-route changes reject stale pagination results")

                tab=navigate("slow");begin(tab)
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>(v.webRagPaginationTabs||[]).length>0)")
                driver.close()
                driver=context.new_page();driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>!(v.webRagPaginationTabs||[]).length)")
                assert source.url.startswith(base+"/story")
                done("Closing the client disconnects its ownership port and removes temporary tabs")

                tab=navigate("slow");begin(tab)
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>(v.webRagPaginationTabs||[]).length>0)")
                owned=driver.evaluate("chrome.storage.session.get('webRagPaginationTabs').then(v=>v.webRagPaginationTabs[0].id)")
                driver.evaluate("id=>chrome.tabs.update(id,{active:true})",owned)
                value=result()
                assert value["diagnostics"]["pagination_stop_reason"]=="interrupted"
                assert driver.evaluate("id=>chrome.tabs.get(id).then(()=>true,()=>false)",owned)
                driver.evaluate("id=>chrome.tabs.remove(id)",owned)
                done("A temporary tab claimed by the user remains open and automatic reading stops")

                tab=navigate("normal")
                panel=context.new_page();panel.set_viewport_size({"width":420,"height":1100})
                panel.goto(f"chrome-extension://{extension}/sidepanel.html");panel.wait_for_load_state("networkidle")
                panel.locator("#view-welcome").wait_for(state="visible")
                panel.evaluate("""async data=>{
                  webRagApiClient.getPlugin=()=>({pluginId:'pagination-ui-fixture',pluginName:'本地分页测试',apiKeyConfigured:false});
                  webRagApiClient.ocr.image=async()=>({text:'PAGINATED_IMAGE_OCR',warnings:[]});
                  currentTabId=data.tab;binding={pluginId:'pagination-ui-fixture',pageUrl:data.url,pageTitle:'分页文章测试',mode:'current',stale:false};
                  await sessionStore.setTabBinding(data.tab,binding);renderAppView();switchView('clip');
                }""",{"tab":tab,"url":source.url})
                assert not panel.locator("#clip-pagination").is_checked()
                panel.locator("#clip-mode").select_option("page")
                assert panel.locator("#clip-pagination").is_disabled()
                panel.locator("#clip-mode").select_option("article")
                panel.locator("#clip-pagination").check();panel.locator("#clip-image-ocr").check()
                panel.locator("#clip-btn").click()
                wait_js(panel,"clipScrollProgress && clipScrollProgress.phase==='pagination'")
                assert "正在读取分页" in panel.locator("#clip-status").inner_text()
                wait_js(panel,"!clipBusy")
                text=panel.locator("#clip-preview-text").input_value()
                assert "CONTENT_PAGE_3" in text and text.count("PAGINATED_IMAGE_OCR")==3,text
                assert "分页：3 页" in panel.locator("#clip-preview-diagnostics").inner_text()
                panel.screenshot(path=str(args.artifacts/"preview.png"))
                panel.locator("#clip-preview-text").fill("KEEP_PAGINATION_EDITED_DRAFT")
                panel.locator("#clip-btn").click()
                wait_js(panel,"clipScrollProgress && clipScrollProgress.phase==='pagination'")
                panel.locator("#clip-capture-cancel").click();wait_js(panel,"!clipBusy")
                assert panel.locator("#clip-preview-text").input_value()=="KEEP_PAGINATION_EDITED_DRAFT"
                wait_js(driver,"chrome.storage.session.get('webRagPaginationTabs').then(v=>!(v.webRagPaginationTabs||[]).length)")
                done("Sidepanel mode gating, pagination progress, editable preview, cross-page image OCR handoff and cancellation preserving edits passed (synthetic identity/OCR)")
            finally:
                context.close()
        passed = True
    finally:
        server.shutdown();server.server_close()
        (args.artifacts/"report.json").write_text(json.dumps({"checks":checks,"passed":passed,"backend_required":False,"ocr_stub":True},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()

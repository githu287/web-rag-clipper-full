"""Opt-in real-site pagination audit using the actual unpacked extension.

No backend credentials or model calls. Live page snapshots/text are private
runtime artifacts, not repository fixtures. Does not bypass certificates/login.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
INSPECT = """sample=>{
 const visible=n=>{let p=n;while(p&&p.nodeType===1){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||p.hidden)return false;p=p.parentElement}return true};
 const roots=[...document.querySelectorAll(sample.root)].filter(visible).sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length);
 const root=roots[0],title=[...document.querySelectorAll('h1')].find(n=>visible(n)&&(n.innerText||'').trim());
 const excluded=n=>n.closest('.page_n,.about_pic,.edit,.related,aside,.article-footer,.translation-banner,.code-block-extension-header')||(sample.exclude&&n.closest(sample.exclude));
 const blocks=root?[...root.querySelectorAll('p,li,h2,h3,pre,td,th')].filter(visible).filter(n=>!excluded(n))
 .map(n=>(n.innerText||'').trim()).filter(t=>t.length>=30):[];
 const images=sample.image_root?[...document.querySelectorAll(sample.image_root+' img')].filter(visible).filter(n=>!excluded(n)).map(n=>{
   const lazy=n.getAttribute('data-src')||n.getAttribute('data-original')||n.getAttribute('data-lazy-src');
   const raw=lazy||n.currentSrc||n.src;
   if(!raw)return null;
   if(!lazy&&n.naturalWidth>0&&n.naturalHeight>0&&n.naturalWidth<=48&&n.naturalHeight<=48)return null;
   try{
     const url=new URL(raw,document.baseURI);if(!/^https?:$/.test(url.protocol))return null;
     // A fresh reference load may upgrade an image before capture or leave it
     // lazy. Accept only alternatives actually exposed by this same DOM image,
     // preserving protocol checks in production pagination and image fetching.
     const choices=[raw,lazy,!lazy||n.naturalWidth>32?n.getAttribute('src'):null,n.naturalWidth>32?n.currentSrc:null].filter(Boolean).map(value=>{
       try{const u=new URL(value,document.baseURI);return /^https?:$/.test(u.protocol)?u.href:null}catch{return null}
     }).filter(Boolean);
     return {url:url.href,choices:[...new Set(choices)]};
   }catch{return null}
 }).filter(Boolean):[];
 return {url:location.href,title:title?title.innerText.trim():document.title,root_found:!!root,blocks,images:[...new Set(images.map(image=>image.url))],image_choices:images};
}"""


def compact(text):
    text = re.sub(r"\[([^\]]*)\]\(<[^>]*>\)", r"\1", text)
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum()).lower()


def capture_review_flow(context, extension, source, tab, rounds, record, artifacts):
    """Actual sidepanel controls, production budgets; no document submission."""
    panel = context.new_page()
    panel.set_viewport_size({"width":420,"height":1100})
    try:
        panel.goto(f"chrome-extension://{extension}/sidepanel.html")
        panel.wait_for_load_state("networkidle")
        panel.evaluate("""data=>{
          globalThis.fetch=()=>Promise.reject(new Error('Pagination audit forbids backend calls'));
          webRagApiClient.setPluginDetails({pluginId:'pagination-live-audit',pluginSecret:'ephemeral-pagination-audit'});
          currentTabId=data.tab;binding={pluginId:'pagination-live-audit',pageUrl:data.url,pageTitle:'真实分页验收',mode:'current',stale:false};
          renderAppView();switchView('clip');
        }""", {"tab":tab,"url":source.url})
        panel.locator("#clip-mode").select_option("article")
        panel.locator("#clip-auto-scroll").uncheck()
        panel.locator("#clip-image-ocr").uncheck()
        panel.locator("#clip-pagination").check()
        panel.locator("#clip-btn").click()
        panel.wait_for_function("()=>!clipBusy", timeout=90000)
        if not panel.evaluate("!!clipDraft"):
            raise RuntimeError(panel.locator("#clip-status").inner_text())
        record["initial_pages"] = panel.evaluate("clipDraft.diagnostics.pagination_page_count")
        panel.locator("#clip-preview-title").fill("用户编辑的分页标题")
        panel.locator("#clip-preview-text").fill(panel.locator("#clip-preview-text").input_value()+"\n\nPAGINATION_USER_EDIT_80%")
        record["rounds"] = []
        for number in range(1, rounds+1):
            if not panel.locator("#clip-continue-pages").is_visible(): break
            previous = panel.locator("#clip-preview-text").input_value()
            old_urls = panel.evaluate("clipDraft.diagnostics.pagination_urls")
            panel.locator("#clip-continue-pages").click()
            panel.wait_for_function("()=>!clipBusy", timeout=90000)
            if not panel.locator("#clip-continuation-pending").is_visible():
                raise RuntimeError(panel.locator("#clip-status").inner_text())
            assert panel.locator("#clip-preview-text").input_value()==previous, "pending results replaced edited body"
            proposed = panel.evaluate("clipContinuationPending.session.report.pagination_urls")
            assert proposed[:len(old_urls)]==old_urls
            assert 0<=len(proposed)-len(old_urls)<=5
            delta = panel.locator("#clip-continuation-text").input_value()
            markers = re.findall(r"分页来源（第 \d+ 页）：([^\n]+)", delta)
            assert markers==proposed[len(old_urls):], "delta repeats accepted source pages or changes ordering"
            if number==1:
                panel.locator("#clip-continuation-discard").click()
                assert panel.locator("#clip-preview-text").input_value()==previous
                assert panel.evaluate("clipDraft.diagnostics.pagination_urls")==old_urls
                panel.locator("#clip-continue-pages").click()
                panel.wait_for_function("()=>!clipBusy", timeout=90000)
                assert panel.evaluate("clipContinuationPending.session.report.pagination_urls")==proposed
                record["discard_retries_same_pages"] = True
            panel.screenshot(path=str(artifacts/f"{record['id']}-review-{number}.png"), full_page=True)
            panel.locator("#clip-continuation-apply").click()
            panel.wait_for_function("()=>clipContinuationPending===null")
            text = panel.locator("#clip-preview-text").input_value()
            assert text.startswith(previous) and text.count("PAGINATION_USER_EDIT_80%")==1
            assert panel.locator("#clip-preview-title").input_value()=="用户编辑的分页标题"
            record["rounds"].append({"round":number,"pages_before":len(old_urls),"pages_after":len(proposed),"added_chars":len(text)-len(previous),"edits_preserved":True})
            print(json.dumps({"id":record["id"],**record["rounds"][-1]},ensure_ascii=False),flush=True)
        result = panel.evaluate("({url:clipDraft.url,raw_text:clipDraft.raw_text,images:[],diagnostics:clipDraft.diagnostics})")
        urls = result["diagnostics"]["pagination_urls"]
        assert re.findall(r"分页来源（第 \d+ 页）：([^\n]+)",result["raw_text"])==(urls if len(urls)>1 else [])
        panel.screenshot(path=str(artifacts/f"{record['id']}-final.png"), full_page=True)
        return result
    finally: panel.close()


def main():
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "extension/tests/pagination-sites.json")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--extension-dir", type=Path, default=ROOT / "extension", help="Unpacked extension directory; can point to a previous Git snapshot for comparison")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/pagination-sites")
    parser.add_argument("--continue-rounds", type=int, default=0, help="Use actual sidepanel review/discard/append flow for 1-5 continuation rounds; image OCR stays off")
    args = parser.parse_args()
    if not 0<=args.continue_rounds<=5: parser.error("--continue-rounds must be between 0 and 5")
    samples = json.loads(args.samples.read_text(encoding="utf-8"))["samples"]
    if args.only: samples = [item for item in samples if item["id"] in args.only]
    if not samples: parser.error("No matching samples")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "extension_version": json.loads((args.extension_dir / "manifest.json").read_text(encoding="utf-8"))["version"],
              "method": "Real public pages and actual extension. Per-page reference DOM is independently inspected. No certificates/login bypass, backend or models.",
              "sidepanel_review_flow":bool(args.continue_rounds),"database_writes":False,"cloud_calls":0,"results": []}
    def save(): (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":1280,"height":1000})
        try:
            extension = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(args.extension_dir.resolve())})["id"]
            driver = context.new_page(); driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
            for sample in samples:
                record = {"id": sample["id"], "url": sample["url"], "boundary": sample.get("continued_boundaries",{}).get(str(args.continue_rounds),sample.get("boundary"))}
                report["results"].append(record)
                source, reference = context.new_page(), context.new_page()
                start = time.monotonic()
                try:
                    response = source.goto(sample["url"], wait_until="domcontentloaded", timeout=25000)
                    record["http_status"] = response.status if response else None
                    try: source.wait_for_load_state("networkidle", timeout=2000)
                    except Exception: pass
                    original_url, original_scroll = source.url, source.evaluate("scrollY")
                    tab = driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", source.url)
                    record["image_collection_enabled"] = not bool(args.continue_rounds)
                    if args.continue_rounds:
                        captured = capture_review_flow(context, extension, source, tab, args.continue_rounds, record, args.artifacts)
                    else:
                        captured = driver.evaluate("""async id=>{
                          const first=await webRagPageCapture.capture(id,{collectPagination:true,collectImages:true});
                          return webRagPaginationCapture.expand(id,first,{signal:new AbortController().signal,collectImages:true,onProgress:()=>{}});
                        }""", tab)
                    record["original_tab_unchanged"] = source.url == original_url and source.evaluate("scrollY") == original_scroll
                    record["binding_url_unchanged"] = captured["url"] == original_url
                    record["diagnostics"] = captured["diagnostics"]
                    record["extracted_chars"] = len(captured["raw_text"])
                    record["image_urls"] = [image["url"] for image in captured["images"]]
                    (args.artifacts / (sample["id"] + ".txt")).write_text(captured["raw_text"], encoding="utf-8")
                    sections = re.split(r"分页来源（第 \d+ 页）：[^\n]+\n\n", captured["raw_text"])
                    if len(sections) > 1: sections = sections[1:]
                    urls = captured["diagnostics"]["pagination_urls"]
                    actual_images = {image["url"] for image in captured["images"]}
                    references = []
                    all_images = []
                    image_choices = {}
                    for index, url in enumerate(urls):
                        reference.goto(url, wait_until="domcontentloaded", timeout=25000)
                        try: reference.wait_for_load_state("networkidle", timeout=1800)
                        except Exception: pass
                        inspected = reference.evaluate(INSPECT, sample)
                        choices = {}
                        for image in inspected["image_choices"]:
                            choices.setdefault(image["url"],set()).update(image["choices"])
                        text = compact(sections[index])
                        checks = {"page":index+1,"url":url,"root_found":inspected["root_found"],
                                  "reference_blocks":len(inspected["blocks"]),
                                  "matched_blocks":sum(compact(block) in text for block in inspected["blocks"]),
                                  "title_present":compact(inspected["title"]) in text,
                                  "reference_images":len(set(inspected["images"])),
                                  "matched_images":sum(bool(values & actual_images) for values in choices.values())}
                        references.append(checks)
                        all_images.extend(image for image in inspected["images"] if image not in all_images)
                        for image, values in choices.items(): image_choices.setdefault(image,set()).update(values)
                        reference.screenshot(path=str(args.artifacts / f"{sample['id']}-{index+1}.png"))
                    record["references"] = references
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        remaining = driver.evaluate("chrome.storage.session.get('webRagPaginationTabs').then(v=>(v.webRagPaginationTabs||[]).length)")
                        if not remaining: break
                        time.sleep(.1)
                    record["temporary_tabs_clean"] = not remaining
                    expected_pages = sample.get("continued_pages",{}).get(str(args.continue_rounds),sample["expected_pages"])
                    expected_stop = sample.get("continued_stops",{}).get(str(args.continue_rounds),sample["expected_stop"])
                    expected = len(urls)==expected_pages and captured["diagnostics"]["pagination_stop_reason"]==expected_stop
                    record["reference_unique_images"] = len(all_images)
                    record["image_pool_limited"] = len(all_images)>33
                    image_list = record["image_urls"]
                    record["image_pool_matches"] = (len(image_list)==min(33,len(all_images)) and
                        all(url in image_choices[expected] for url,expected in zip(image_list,all_images[:33]))) if record["image_collection_enabled"] else None
                    record["image_reference_choices"] = {key:sorted(values) for key,values in image_choices.items()}
                    record["text_checks_passed"] = all(r["root_found"] and r["matched_blocks"]==r["reference_blocks"] and r["title_present"] for r in references)
                    record["image_limit_reported"] = "capture_image_limit" in captured["diagnostics"].get("quality_warnings",[])
                    image_ok = not record["image_collection_enabled"] or record["image_pool_matches"] and (not record["image_pool_limited"] or record["image_limit_reported"])
                    content_ok = record["text_checks_passed"] and image_ok
                    passed = expected and content_ok and record["original_tab_unchanged"] and record["binding_url_unchanged"] and record["temporary_tabs_clean"]
                    record["status"] = ("support_boundary_confirmed" if record["boundary"] else "checks_passed") if passed else "needs_review"
                    if passed and record["image_collection_enabled"] and record["image_pool_limited"]:
                        record["status"] = "checks_passed_with_image_limit"
                except Exception as error:
                    record["status"] = "access_or_transport_error" if "net::ERR_" in str(error) else "capture_error"
                    record["error_type"] = type(error).__name__
                    record["error"] = str(error)[:700]
                finally:
                    source.close(); reference.close()
                    record["seconds"] = round(time.monotonic() - start, 3); save()
                    print(json.dumps({key:record.get(key) for key in ("id","status","extracted_chars","references","seconds")},ensure_ascii=False),flush=True)
        finally:
            context.close(); save()


if __name__ == "__main__": main()

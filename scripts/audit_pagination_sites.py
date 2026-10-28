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
 const blocks=root?[...root.querySelectorAll('p,li,h2,h3,pre,td,th')].filter(visible).filter(n=>!n.closest('.page_n,.about_pic,.edit,.related,aside,.article-footer,.translation-banner,.code-block-extension-header'))
 .map(n=>(n.innerText||'').trim()).filter(t=>t.length>=30):[];
 const images=sample.image_root?[...document.querySelectorAll(sample.image_root+' img')].filter(visible).map(n=>n.currentSrc||n.src).filter(Boolean):[];
 return {url:location.href,title:title?title.innerText.trim():document.title,root_found:!!root,blocks,images};
}"""


def compact(text):
    text = re.sub(r"\[([^\]]*)\]\(<[^>]*>\)", r"\1", text)
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum()).lower()


def main():
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "extension/tests/pagination-sites.json")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--extension-dir", type=Path, default=ROOT / "extension", help="Unpacked extension directory; can point to a previous Git snapshot for comparison")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/pagination-sites")
    args = parser.parse_args()
    samples = json.loads(args.samples.read_text(encoding="utf-8"))["samples"]
    if args.only: samples = [item for item in samples if item["id"] in args.only]
    if not samples: parser.error("No matching samples")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "extension_version": json.loads((args.extension_dir / "manifest.json").read_text(encoding="utf-8"))["version"],
              "method": "Real public pages and actual extension. Per-page reference DOM is independently inspected. No certificates/login bypass, backend or models.", "results": []}
    def save(): (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":1280,"height":1000})
        try:
            extension = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path": str(args.extension_dir.resolve())})["id"]
            driver = context.new_page(); driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
            for sample in samples:
                record = {"id": sample["id"], "url": sample["url"], "boundary": sample.get("boundary")}
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
                    for index, url in enumerate(urls):
                        reference.goto(url, wait_until="domcontentloaded", timeout=25000)
                        try: reference.wait_for_load_state("networkidle", timeout=1800)
                        except Exception: pass
                        inspected = reference.evaluate(INSPECT, sample)
                        text = compact(sections[index])
                        checks = {"page":index+1,"url":url,"root_found":inspected["root_found"],
                                  "reference_blocks":len(inspected["blocks"]),
                                  "matched_blocks":sum(compact(block) in text for block in inspected["blocks"]),
                                  "title_present":compact(inspected["title"]) in text,
                                  "reference_images":len(set(inspected["images"])),
                                  "matched_images":len(set(inspected["images"]) & actual_images)}
                        references.append(checks)
                        reference.screenshot(path=str(args.artifacts / f"{sample['id']}-{index+1}.png"))
                    record["references"] = references
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        remaining = driver.evaluate("chrome.storage.session.get('webRagPaginationTabs').then(v=>(v.webRagPaginationTabs||[]).length)")
                        if not remaining: break
                        time.sleep(.1)
                    record["temporary_tabs_clean"] = not remaining
                    expected = len(urls) == sample["expected_pages"] and captured["diagnostics"]["pagination_stop_reason"] == sample["expected_stop"]
                    content_ok = all(r["root_found"] and r["matched_blocks"] == r["reference_blocks"] and r["title_present"] and r["matched_images"] == r["reference_images"] for r in references)
                    passed = expected and content_ok and record["original_tab_unchanged"] and record["binding_url_unchanged"] and record["temporary_tabs_clean"]
                    record["status"] = ("support_boundary_confirmed" if sample.get("boundary") else "checks_passed") if passed else "needs_review"
                except Exception as error:
                    record["status"] = "access_or_transport_error" if "net::ERR_" in str(error) else "capture_error"
                    record["error_type"] = type(error).__name__
                finally:
                    source.close(); reference.close()
                    record["seconds"] = round(time.monotonic() - start, 3); save()
                    print(json.dumps({key:record.get(key) for key in ("id","status","extracted_chars","references","seconds")},ensure_ascii=False),flush=True)
        finally:
            context.close(); save()


if __name__ == "__main__": main()

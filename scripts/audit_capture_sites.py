"""Opt-in live-page audit using the real unpacked extension in isolated Chrome.

No login, challenge solving, backend credentials or source uploads. Reports and
private page snapshots default to ignored .venv/runtime/site-audit/. Live site
failures are observations, not CI failures. Use offline fixtures for regression.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import time
import sys

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

ROOT = Path(__file__).resolve().parent.parent

INSPECT = r"""sample => {
  const visible = el => {let p=el;while(p&&p.nodeType===1){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||p.hidden||p.getAttribute('aria-hidden')==='true')return false;p=p.parentElement;}return true;};
  const roots = Array.from(document.querySelectorAll(sample.root)).filter(visible);
  const root = roots.sort((a,b)=>(b.innerText||'').length-(a.innerText||'').length)[0];
  const text = root ? root.innerText.trim() : '';
  const blocks = root ? Array.from(root.querySelectorAll('p,li,h2,h3,pre,td,th')).filter(visible)
    .filter(el=>!el.closest('.article-footer,.translation-banner,.code-block-extension-header'))
    .filter(el=>!el.parentElement.closest('pre'))
    .map(el=>({tag:el.tagName,text:el.tagName==='PRE' ? (el.querySelector('code')||el).innerText.trim() : el.innerText.trim()})).filter(b=>b.text.length>=30) : [];
  const title = Array.from(document.querySelectorAll(sample.title)).find(visible);
  const codeSamples = root ? Array.from(root.querySelectorAll('pre')).filter(visible).map(pre=>{
    const lines=pre.querySelectorAll('td.hljs-ln-code');
    if(lines.length) return Array.from(lines).map(line=>line.textContent).join('\n');
    return (Array.from(pre.children).find(el=>el.tagName==='CODE')||pre).textContent;
  }).filter(text=>text.trim()) : [];
  return {title:document.title,expected_title:title&&title.innerText.trim(),url:location.href,
    body_chars:(document.body.innerText||'').length,body_preview:(document.body.innerText||'').slice(0,1000),
    reference_chars:text.length,reference_first:(blocks[0]?.text||text).slice(0,80),reference_last:(blocks.at(-1)?.text||text).slice(-80),
    root_found:!!root,blocks,root_selector:root&&root.tagName.toLowerCase()+(root.id?'#'+root.id:'.'+String(root.className).trim().split(/\s+/).join('.')),
    candidates:Array.from(document.querySelectorAll('article,main,#js_content,#article_content,#cnblogs_post_body,.article-content,.TRS_Editor,.TRS_UEDITOR,#UCAP-CONTENT,#detail,.content'))
      .slice(0,30).map(el=>({tag:el.tagName,id:el.id,cls:String(el.className),chars:(el.innerText||'').length})),
    code_blocks:root?root.querySelectorAll('pre').length:0,tables:root?root.querySelectorAll('table').length:0,
    headings:root?root.querySelectorAll('h1,h2,h3,h4,h5,h6').length:0,code_samples:codeSamples};
}"""


def normalized(value: str) -> str:
    # Punctuation/Markdown/whitespace differences should not count as omissions.
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKC", value) if c.isalnum()).lower()


def normalize_code(value: str) -> str:
    import re
    return re.sub(r"[ \t]+$", "", value.replace("\r\n", "\n"), flags=re.M).strip("\n")


def fenced_code_blocks(text: str) -> list[str]:
    import re
    blocks = []
    lines = text.splitlines()
    current = None
    content = []
    for line in lines:
        match = re.match(r"^( *)(`{3,})(.*)$", line)
        if current is None and match:
            current = (len(match[1]), len(match[2]))
            content = []
        elif current is not None:
            indent, width = current
            if match and len(match[2]) >= width and not match[3].strip():
                blocks.append(normalize_code("\n".join(content)))
                current = None
            else:
                content.append(line[indent:] if line.startswith(" " * indent) else line)
    return blocks


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "extension/tests/site-samples.json")
    parser.add_argument("--only", nargs="*", help="Sample IDs to run")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/site-audit")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--auto-scroll", action="store_true", help="Opt in to bounded scrolling before capture")
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    samples = json.loads(args.samples.read_text(encoding="utf-8"))["samples"]
    if args.only:
        samples = [s for s in samples if s["id"] in args.only]
        if not samples:
            parser.error("No matching samples")
    results = []

    def save():
        report = {"date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "extension_version": json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))["version"],
            "auto_scroll": args.auto_scroll,
            "method": "Real extension capture; public pages in a fresh headless Chrome profile. No login or challenge bypass. Coverage compares rendered reference DOM blocks only; not unseen content.",
            "results": results}
        (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="profile-", dir=args.artifacts) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
            viewport={"width": 1280, "height": 1000}, ignore_default_args=["--disable-extensions"],
            args=["--enable-unsafe-extension-debugging"])
        try:
            cdp = context.browser.new_browser_cdp_session()
            extension = cdp.send("Extensions.loadUnpacked", {"path": str(ROOT / "extension")})["id"]
            driver = context.new_page()
            driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
            for sample in samples:
                record = {"id": sample["id"], "site": sample["site"], "url": sample["url"]}
                results.append(record)
                page = context.new_page()
                start = time.monotonic()
                try:
                    response = page.goto(sample["url"], wait_until="domcontentloaded", timeout=20000)
                    record["http_status"] = response.status if response else None
                    try:
                        page.wait_for_load_state("networkidle", timeout=3500)
                    except BrowserTimeout:
                        record["network_busy"] = True
                    # Inspect what the actual browser rendered, before capture.
                    inspected = page.evaluate(INSPECT, sample)
                    record.update({key: value for key, value in inspected.items() if key not in ("blocks", "code_samples")})
                    (args.artifacts / f"{sample['id']}.html").write_text(page.content(), encoding="utf-8")
                    page.screenshot(path=str(args.artifacts / f"{sample['id']}.png"))
                    tab_id = driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", page.url)
                    capture_start = time.monotonic()
                    result = driver.evaluate("async data=>webRagPageCapture.capture(data.id,{mode:'article',autoScroll:data.autoScroll})", {"id": tab_id, "autoScroll": args.auto_scroll})
                    record["capture_seconds"] = round(time.monotonic() - capture_start, 3)
                    # Wait logic can expose newly rendered text; inspect again.
                    inspected = page.evaluate(INSPECT, sample)
                    record.update({key: value for key, value in inspected.items() if key not in ("blocks", "code_samples")})
                    text = result["raw_text"]
                    record["extracted_chars"] = len(text)
                    record["diagnostics"] = result["diagnostics"]
                    (args.artifacts / f"{sample['id']}.txt").write_text(text, encoding="utf-8")
                    import re
                    readable = re.sub(r'\[([^\]]*)\]\(<[^>]*>\)', r'\1', text)
                    readable = re.sub(r'(?m)(^|<br>)\s*(?:[-*+] |\d+\. )', r'\1', readable)
                    readable = readable.replace('<br>', '')
                    compact = normalized(readable)
                    blocks = inspected["blocks"]
                    found = [b for b in blocks if normalized(b["text"]) in compact]
                    missing = [b for b in blocks if normalized(b["text"]) not in compact]
                    record["reference_blocks"] = len(blocks)
                    record["matched_blocks"] = len(found)
                    record["block_coverage"] = round(len(found) / len(blocks), 4) if blocks else None
                    record["missing_blocks"] = [{"tag": b["tag"], "preview": b["text"][:160]} for b in missing[:12]]
                    record["first_present"] = normalized(inspected["reference_first"]) in compact if inspected["reference_first"] else False
                    record["last_present"] = normalized(inspected["reference_last"]) in compact if inspected["reference_last"] else False
                    record["title_present"] = normalized(inspected["expected_title"] or "") in compact if inspected["expected_title"] else None
                    codes = inspected["code_samples"]
                    record["reference_code_blocks"] = len(codes)
                    extracted_codes = fenced_code_blocks(text)
                    record["exact_code_blocks"] = sum(normalize_code(code) in extracted_codes for code in codes)
                    record["noise_found"] = [marker for marker in sample.get("exclude_text", []) if marker in text]
                    body = inspected["body_preview"]
                    if record.get("http_status", 0) in (401, 403, 429) or (not inspected["root_found"] and any(marker in body for marker in
                        ("访问受限", "环境异常", "安全验证", "验证后继续", "Access Denied", "Just a moment", "完成验证", "请求存在异常", "网络环境存在风险", "请登录", "该内容已被发布者删除", "操作频繁"))):
                        record["status"] = "blocked_access"
                    elif not inspected["root_found"]:
                        record["status"] = "reference_missing"
                    elif inspected["reference_chars"] < sample["min_chars"]:
                        record["status"] = "reference_short"
                    elif record["block_coverage"] is not None and record["block_coverage"] < 0.95 or record["exact_code_blocks"] != len(codes):
                        record["status"] = "content_mismatch"
                    elif not record["first_present"] or not record["last_present"] or record["title_present"] is False or record["noise_found"]:
                        record["status"] = "needs_review"
                    else:
                        record["status"] = "checks_passed"
                except Exception as error:
                    record["status"] = "blocked_access" if "访问限制" in str(error) else "blocked_transport" if "net::ERR_" in str(error) or (isinstance(error, BrowserTimeout) and "reference_chars" not in record) else "capture_error"
                    record["error"] = str(error)[:700]
                finally:
                    record["seconds"] = round(time.monotonic() - start, 3)
                    page.close()
                    save()
                    print(json.dumps({key: record.get(key) for key in ("id", "status", "http_status", "reference_chars", "extracted_chars", "block_coverage", "title_present", "seconds")}, ensure_ascii=False), flush=True)
        finally:
            context.close()
            save()


if __name__ == "__main__":
    main()

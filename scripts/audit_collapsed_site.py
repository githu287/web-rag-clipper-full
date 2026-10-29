"""Opt-in live native-details audit using the actual extension; no backend/models.

Checks a bounded set of closed blocks against source DOM text and verifies that
capture leaves the article unchanged. This is not whole-document coverage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile
import unicodedata

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def compact(value):
    value = re.sub(r"\[([^\]]*)\]\(<[^>]*>\)", r"\1", value)
    return "".join(c for c in unicodedata.normalize("NFKC", value) if c.isalnum()).lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://github.com/bregman-arie/devops-exercises")
    parser.add_argument("--root", default="article.markdown-body")
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".venv/runtime/collapsed-site")
    args = parser.parse_args()
    if not 1 <= args.blocks <= 10: parser.error("--blocks must be between 1 and 10")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    report = {"url": args.url, "passed": False, "backend_required": False,
              "extension_version": json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))["version"],
              "scope": "First requested closed native blocks, first nonempty paragraph/list item per block; not whole-document coverage"}
    try:
        with tempfile.TemporaryDirectory(dir=args.artifacts) as profile, sync_playwright() as p:
            context = p.chromium.launch_persistent_context(profile, channel=args.channel, headless=True,
                ignore_default_args=["--disable-extensions"], args=["--enable-unsafe-extension-debugging"], viewport={"width":1280,"height":1000})
            try:
                extension = context.browser.new_browser_cdp_session().send("Extensions.loadUnpacked", {"path":str(ROOT / "extension")})["id"]
                driver = context.new_page(); driver.goto(f"chrome-extension://{extension}/tests/capture-driver.html")
                page = context.new_page()
                response = page.goto(args.url, wait_until="domcontentloaded", timeout=25000)
                report["http_status"] = response.status if response else None
                try: page.wait_for_load_state("networkidle", timeout=2500)
                except Exception: pass
                root = page.locator(args.root).first
                original = root.inner_html()
                states = root.evaluate("root=>[...root.querySelectorAll('details')].map(d=>d.open)")
                refs = root.evaluate("""(root, count)=>[...root.querySelectorAll('details:not([open])')].slice(0,count).map(d=>({
                  summary:d.querySelector('summary')?.textContent.trim(),
                  body:[...d.querySelectorAll('p,li')].find(n=>!n.closest('summary')&&n.textContent.trim().length>=20)?.textContent.trim()
                }))""", args.blocks)
                tab = driver.evaluate("async url=>(await chrome.tabs.query({})).find(t=>t.url===url).id", page.url)
                capture = driver.evaluate("async id=>webRagPageCapture.capture(id,{mode:'article'})", tab)
                text = compact(capture["raw_text"])
                report["checks"] = [{"summary_present":bool(r["summary"]) and compact(r["summary"]) in text,
                                     "body_present":bool(r["body"]) and compact(r["body"]) in text} for r in refs]
                report["original_article_dom_unchanged"] = root.inner_html() == original
                report["details_state_unchanged"] = root.evaluate("root=>[...root.querySelectorAll('details')].map(d=>d.open)") == states
                report["diagnostics"] = capture["diagnostics"]
                report["passed"] = len(refs) == args.blocks and all(c["summary_present"] and c["body_present"] for c in report["checks"]) and report["original_article_dom_unchanged"] and report["details_state_unchanged"] and "collapsed_content_included" in capture["diagnostics"]["quality_warnings"]
                (args.artifacts / "capture.txt").write_text(capture["raw_text"], encoding="utf-8")
                page.screenshot(path=str(args.artifacts / "source.png"))
                if not report["passed"]: raise AssertionError("Live collapsed-block checks failed; inspect private report")
            finally: context.close()
    finally:
        (args.artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed":report["passed"], "blocks":len(report["checks"]), "extension_version":report["extension_version"]}))


if __name__ == "__main__": main()

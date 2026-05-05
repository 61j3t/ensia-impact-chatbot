"""Scrape v2v.ensia.edu.dz with Playwright.

The site is a JS-rendered React SPA — a single landing page with anchor
sections (#about, #apply, #offers, #process, #projects, #privacy). There
are no subpages or backend API to crawl, so we just render the page and
extract the visible text.

Output:
  data/external_text/v2v_ensia/v2v_landing.txt
  data/external_text/v2v_ensia/_summary.json

Run:
    .venv/bin/python scripts/05_scrape_v2v_website.py
    .venv/bin/python scripts/05_scrape_v2v_website.py --rebuild
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data/external_text/v2v_ensia"
SUMMARY_PATH = OUT_DIR / "_summary.json"

URL = "https://v2v.ensia.edu.dz/"
PAGE_LOAD_TIMEOUT_MS = 60_000
WAIT_AFTER_LOAD_MS = 1500   # let any post-load JS settle


def _clean(text: str) -> str:
    """Collapse runs of whitespace and trim per-line."""
    lines = [line.strip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            line = re.sub(r"[ \t]{2,}", " ", line)
            out.append(line)
    return "\n".join(out).strip()


def render_page(url: str) -> tuple[str, str]:
    """Return (title, visible_text) after the SPA finishes rendering."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 ensia-impact-chatbot-scraper"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        # Scroll through the page so any in-view-only content gets rendered.
        page.evaluate(
            "async () => {"
            "  const step = window.innerHeight;"
            "  for (let y = 0; y < document.body.scrollHeight; y += step) {"
            "    window.scrollTo(0, y);"
            "    await new Promise(r => setTimeout(r, 200));"
            "  }"
            "  window.scrollTo(0, 0);"
            "}"
        )
        page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

        title = page.title() or ""
        # innerText returns the visible text the user would see — no script
        # blobs, no hidden elements, no inline styles.
        body_text = page.evaluate("() => document.body.innerText") or ""
        browser.close()
    return title, _clean(body_text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Re-render even if a cached copy exists (for cosmetic edits to "
             "this script). The site has no `modified` signal, so a normal "
             "run always re-renders too.",
    )
    args = parser.parse_args()
    _ = args  # currently the run is unconditional; flag kept for parity

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Rendering {URL} with Playwright (headless Chromium)…")
    title, body = render_page(URL)

    if not body.strip():
        print("✗ rendered page is empty — abort")
        raise SystemExit(1)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        f"[Source: {URL}]\n"
        f"[Title: {title}]\n"
        f"[Language: en]\n"
        f"[Modified: {now}]\n\n"
    )
    out_path = OUT_DIR / "v2v_landing.txt"
    out_path.write_text(header + body, encoding="utf-8")

    summary = [{
        "id": "landing",
        "slug": "v2v_landing",
        "link": URL,
        "title": title,
        "language": "en",
        "modified": now,
        "char_count": len(body),
        "output": str(out_path.relative_to(ROOT)),
        "kind": "landing",
    }]
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=" * 60)
    print("V2V SCRAPE SUMMARY")
    print("=" * 60)
    print(f"Title: {title}")
    print(f"Body:  {len(body):,} chars")
    print(f"Saved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

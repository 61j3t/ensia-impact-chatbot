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
import os
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

# The Projects section renders only 3 startup cards by default behind a
# "Show all startups" toggle, and the FAQ answers sit in collapsed
# accordions — both are client-side interactions the render must trigger
# or we capture an incomplete page. Cards/answers are fetched from a
# backend that is occasionally down; if the toggle can't expand the full
# roster we keep the last-known-good file rather than regress (see main).
STARTUP_MARKER = "STARTUP TEAM"
# Floor that distinguishes "toggle expanded the full cohort" (~22) from
# "only the 3 default cards rendered / backend was down". Tune if the real
# roster shrinks below this.
MIN_STARTUPS = 10

_SCROLL_JS = (
    "async () => {"
    "  const step = window.innerHeight;"
    "  for (let y = 0; y < document.body.scrollHeight; y += step) {"
    "    window.scrollTo(0, y);"
    "    await new Promise(r => setTimeout(r, 200));"
    "  }"
    "  window.scrollTo(0, 0);"
    "}"
)


def _startup_names(body: str) -> list[str]:
    """Pull the startup names out of the rendered text: each card is a
    `STARTUP TEAM` marker followed by the name on the next non-empty line."""
    names: list[str] = []
    lines = [ln.strip() for ln in body.splitlines()]
    for i, ln in enumerate(lines):
        if ln == STARTUP_MARKER:
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j]:
                    names.append(lines[j])
                    break
    return names


def _roster_block(names: list[str]) -> str:
    """A compact, self-contained roster paragraph prepended to the page
    text. The per-card descriptions get split across several ~2000-char
    chunks (and the answer layer truncates each to 1500), so no single
    retrieved chunk holds the whole cohort. This dense names-only block
    fits in ONE chunk, so 'list all / how many startups' retrieves the
    complete set. Phrased to match those intents lexically (BM25) too."""
    if not names:
        return ""
    return (
        f"V2V Incubator — full list of all {len(names)} incubated startups "
        f"(the complete cohort of incubated projects / startups / teams): "
        + ", ".join(names)
        + ".\n\n"
    )


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


_FAQ_Q_RE = re.compile(r"^\d{2}\.\s")


def _reveal_startups(page) -> None:
    """Click the 'Show all startups' toggle so the full roster (not just the
    3 default cards) renders. Best-effort — a missing control must not abort
    the render; the caller's MIN_STARTUPS guard catches a short roster."""
    for make in (
        lambda: page.get_by_role("button", name=re.compile("show all", re.I)),
        lambda: page.get_by_text(re.compile("show all startups", re.I)),
    ):
        try:
            make().first.click(timeout=6000)
            page.wait_for_timeout(2000)  # let the extra cards attach
            return
        except Exception:
            continue
    print("  (‘Show all startups’ toggle not found/clickable)")


def _collect_faq(page) -> str:
    """The FAQ is a single-open accordion, so we can't expand all then read
    once. Click each question, read its now-visible answer, and assemble a
    clean Q&A block. Returns '' if no FAQ found. Best-effort per item."""
    try:
        n = page.get_by_text(_FAQ_Q_RE).count()
    except Exception:
        return ""
    pairs: list[str] = []
    for i in range(n):
        try:
            q_el = page.get_by_text(_FAQ_Q_RE).nth(i)
            question = (q_el.inner_text() or "").strip()
            q_el.click(timeout=1500)
            page.wait_for_timeout(300)
            it = page.evaluate("() => document.body.innerText") or ""
            # Answer = text right after this question up to the next "NN." or
            # a known footer marker.
            m = re.search(
                re.escape(question) + r"\s*(.*?)(?=\n\d{2}\.\s|\nJoin our newsletter|\nFollow Us|\Z)",
                it, re.S,
            )
            answer = (m.group(1).strip() if m else "")
            if answer and not _FAQ_Q_RE.match(answer) and len(answer) > 5:
                pairs.append(f"{question}\n{answer}")
        except Exception:
            pass
    if pairs:
        print(f"  (captured {len(pairs)} FAQ answer(s))")
        return "\n\nFrequently Asked Questions\n\n" + "\n\n".join(pairs)
    return ""


def render_page(url: str) -> tuple[str, str]:
    """Return (title, visible_text) after the SPA finishes rendering AND
    all hidden content (full startup roster + FAQ answers) is expanded."""
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
        # Scroll once so in-view-only content mounts (the toggle/FAQ exist).
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

        # Expand the full startup roster, then scroll so the new cards
        # render before we read the text.
        _reveal_startups(page)
        page.evaluate(_SCROLL_JS)
        page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

        title = page.title() or ""
        # innerText returns the visible text the user would see — no script
        # blobs, no hidden elements, no inline styles. Startup cards stay
        # visible after the toggle, so they're all captured here.
        body_text = page.evaluate("() => document.body.innerText") or ""

        # FAQ answers live in a single-open accordion — collect them
        # separately (click-per-item) and append as a clean Q&A block.
        faq_block = _collect_faq(page)
        browser.close()
    return title, _clean(body_text) + faq_block


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
    out_path = OUT_DIR / "v2v_landing.txt"
    print(f"Rendering {URL} with Playwright (headless Chromium)…")
    title, body = render_page(URL)

    if not body.strip():
        print("✗ rendered page is empty — abort")
        raise SystemExit(1)

    # Integrity guard: don't let a partial render (backend down, toggle
    # failed to expand) overwrite a good roster. If we captured fewer than
    # MIN_STARTUPS cards and a prior file exists, keep the last-known-good
    # and DON'T touch _summary.json — no needless rebuild, no regression.
    # Exit 0 either way: a transient V2V outage must not break the whole
    # nightly pipeline (ensia scrape / chat / index still need to run).
    n_startups = body.count(STARTUP_MARKER)
    if n_startups < MIN_STARTUPS and out_path.exists():
        print()
        print(f"⚠ only {n_startups} startup card(s) captured (expected ≥{MIN_STARTUPS}).")
        print("  The 'Show all startups' toggle likely didn't expand, or the")
        print("  backend was unavailable. Keeping the previous last-known-good")
        print(f"  {out_path.name} and leaving _summary.json untouched.")
        raise SystemExit(0)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        f"[Source: {URL}]\n"
        f"[Title: {title}]\n"
        f"[Language: en]\n"
        f"[Modified: {now}]\n\n"
    )
    # Prepend the compact all-names roster so a single chunk can answer
    # "list all / how many startups"; the detailed cards follow for
    # per-startup questions.
    roster = _roster_block(_startup_names(body))

    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    _atomic_write(out_path, header + roster + body)

    summary = [{
        "id": "landing",
        "slug": "v2v_landing",
        "link": URL,
        "title": title,
        "language": "en",
        "modified": now,
        "char_count": len(body),
        "startup_count": n_startups,
        "output": str(out_path.relative_to(ROOT)),
        "kind": "landing",
    }]
    _atomic_write(
        SUMMARY_PATH,
        json.dumps(summary, ensure_ascii=False, indent=2),
    )

    print()
    print("=" * 60)
    print("V2V SCRAPE SUMMARY")
    print("=" * 60)
    print(f"Title:    {title}")
    print(f"Body:     {len(body):,} chars")
    print(f"Startups: {n_startups}")
    print(f"Saved:    {out_path.relative_to(ROOT)}")
    if n_startups < MIN_STARTUPS:
        print(f"⚠ NOTE: only {n_startups} startups captured (no prior file to fall back to).")


if __name__ == "__main__":
    main()

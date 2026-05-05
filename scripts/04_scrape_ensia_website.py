"""Scrape ensia.edu.dz via the WordPress REST API.

The site is WordPress-based, so we don't need a recursive HTML crawler —
the REST API at /wp-json/wp/v2/{pages,posts} returns every document with
title + rendered HTML content + modified timestamp in a handful of
requests, and there are language-prefixed endpoints for the EN / AR / FR
variants.

What gets fetched:

  Endpoint (per language)              Type
  /wp-json/wp/v2/pages?per_page=100    static pages (incubator, AI lab,
                                       CDE, faculty, programs, …)
  /wp-json/wp/v2/posts?per_page=100    news / announcements

  /            → English (default)
  /ar/         → Arabic
  /fr/         → French

Output:
  data/external_text/ensia_edu_dz/<lang>/<kind>/<safe-slug>.txt
  data/external_text/ensia_edu_dz/_summary.json   (manifest of every doc)

Each .txt file mirrors the layout used elsewhere (extracted_text/, ocr_text/):
the first lines are key:value headers, then a blank line, then plain text.

The scraper is INCREMENTAL: it stores each doc's `modified` timestamp in
the manifest and skips re-fetching pages whose timestamp hasn't changed.
Use --rebuild to force re-fetch everything.

Run:
    .venv/bin/python scripts/04_scrape_ensia_website.py
    .venv/bin/python scripts/04_scrape_ensia_website.py --rebuild
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import unicodedata
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data/external_text/ensia_edu_dz"
SUMMARY_PATH = OUT_DIR / "_summary.json"

BASE = "https://ensia.edu.dz"
LANGS = [
    ("en", ""),     # default — no prefix
    ("ar", "ar/"),
    ("fr", "fr/"),
]
KINDS = ["pages", "posts"]
PER_PAGE = 100
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) ensia-impact-chatbot-scraper"
)
REQUEST_TIMEOUT = 30.0
REQUEST_DELAY_S = 0.3   # polite pause between requests


# ─── helpers ────────────────────────────────────────────────────────────────

def _safe_slug(s: str) -> str:
    """Filesystem-safe slug. Keeps Unicode word chars + dash; replaces the
    rest with underscore. Capped to 80 chars so deep slugs don't blow past
    common path limits."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_")
    return (s or "untitled")[:80]


def _strip_html(rendered_html: str) -> str:
    """Turn WordPress-rendered HTML into clean plain text.

    - Drops <script>, <style>, and any iframe/figure/img the editor injected.
    - Decodes HTML entities (&amp;, &nbsp;, &#8217;, …).
    - Collapses runs of whitespace to single newlines.
    """
    soup = BeautifulSoup(rendered_html, "html.parser")
    for tag in soup(["script", "style", "iframe", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = html.unescape(text)
    # Collapse runs of blank lines and trim trailing spaces per line.
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip()


def _fetch_all(client: httpx.Client, lang_prefix: str, kind: str) -> list[dict]:
    """Page through /wp-json/wp/v2/<kind> until empty. Returns every doc."""
    items: list[dict] = []
    page = 1
    while True:
        url = f"{BASE}/{lang_prefix}wp-json/wp/v2/{kind}"
        params = {
            "per_page": PER_PAGE,
            "page": page,
            "_fields": "id,slug,link,title,date,modified,content",
        }
        r = client.get(url, params=params)
        if r.status_code == 400:
            # WP returns 400 once you page past the end.
            break
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
        time.sleep(REQUEST_DELAY_S)
    return items


def _doc_text(doc: dict, lang: str) -> tuple[str, dict]:
    """Render a WP doc into the (text, manifest_entry) we'll save."""
    title = html.unescape((doc.get("title") or {}).get("rendered", "") or "")
    raw_html = (doc.get("content") or {}).get("rendered", "") or ""
    body = _strip_html(raw_html)
    link = doc.get("link", "")
    modified = doc.get("modified", "")
    slug = doc.get("slug") or _safe_slug(title) or f"id_{doc.get('id','')}"

    header = (
        f"[Source: {link}]\n"
        f"[Title: {title}]\n"
        f"[Language: {lang}]\n"
        f"[Modified: {modified}]\n\n"
    )
    return header + body, {
        "id": doc.get("id"),
        "slug": slug,
        "link": link,
        "title": title,
        "language": lang,
        "modified": modified,
        "char_count": len(body),
    }


# ─── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Re-fetch every page even if the cached `modified` matches.",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache: dict[str, dict] = {}
    if SUMMARY_PATH.exists() and not args.rebuild:
        cache = {
            entry["link"]: entry
            for entry in json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
        }
        print(f"Loaded cache with {len(cache)} entries")

    headers = {"User-Agent": USER_AGENT}
    new_summary: list[dict] = []
    counts: dict[str, int] = {"fetched": 0, "skipped": 0, "errors": 0, "total": 0}

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True) as client:
        for lang, prefix in LANGS:
            for kind in KINDS:
                print(f"\n▶ {lang} / {kind}")
                try:
                    docs = _fetch_all(client, prefix, kind)
                except httpx.HTTPError as e:
                    print(f"  ✗ failed to list: {e}")
                    continue
                print(f"  found {len(docs)} {kind}")

                kind_dir = OUT_DIR / lang / kind
                kind_dir.mkdir(parents=True, exist_ok=True)

                for doc in docs:
                    counts["total"] += 1
                    link = doc.get("link", "")
                    cached = cache.get(link)
                    if (
                        cached
                        and cached.get("modified") == doc.get("modified")
                        and (Path(cached.get("output", "/nonexistent"))).exists()
                    ):
                        new_summary.append(cached)
                        counts["skipped"] += 1
                        continue

                    try:
                        text, entry = _doc_text(doc, lang)
                    except Exception as e:
                        print(f"  ✗ render failed for {link}: {e}")
                        counts["errors"] += 1
                        continue

                    # Always prefix with the WP id so URL-encoded Arabic slugs
                    # (which collapse to indistinguishable `d8_…d9_…` strings
                    # after _safe_slug + 80-char cap) don't collide.
                    doc_id = doc.get("id", "")
                    fname = f"{doc_id}_{_safe_slug(entry['slug'])}.txt"
                    out_path = kind_dir / fname
                    out_path.write_text(text, encoding="utf-8")
                    entry["output"] = str(out_path.relative_to(ROOT))
                    entry["kind"] = kind
                    new_summary.append(entry)
                    counts["fetched"] += 1
                    print(f"  ✓ {entry['title'][:60]}  ({entry['char_count']:,} chars)")

    # Drop any .txt under OUT_DIR that isn't in the new manifest. Catches
    # files from older runs left behind by changed slugs / language moves.
    expected = {
        (ROOT / e["output"]).resolve()
        for e in new_summary if e.get("output")
    }
    orphans: list[Path] = []
    for txt in OUT_DIR.rglob("*.txt"):
        if txt.resolve() not in expected:
            orphans.append(txt)
    for o in orphans:
        o.unlink()
    if orphans:
        print(f"\nRemoved {len(orphans)} orphaned file(s) from previous runs.")

    SUMMARY_PATH.write_text(
        json.dumps(new_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print("SCRAPE SUMMARY")
    print("=" * 60)
    print(f"Total docs:  {counts['total']}")
    print(f"Fetched:     {counts['fetched']}")
    print(f"Skipped:     {counts['skipped']}  (unchanged since last run)")
    print(f"Errors:      {counts['errors']}")
    by_lang: dict[str, int] = {}
    by_lang_chars: dict[str, int] = {}
    for e in new_summary:
        by_lang[e["language"]] = by_lang.get(e["language"], 0) + 1
        by_lang_chars[e["language"]] = by_lang_chars.get(e["language"], 0) + e["char_count"]
    print("\nBy language:")
    for lang in ("en", "ar", "fr"):
        print(f"  {lang}: {by_lang.get(lang, 0)} docs, {by_lang_chars.get(lang, 0):,} chars")
    print(f"\nOutput: {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()

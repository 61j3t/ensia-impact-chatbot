"""Fetch the exact URLs shared in the Telegram chat — no site crawling.

Reads every URL that appears in any message, drops the obviously-not-text
hosts (forms.gle, LinkedIn, Instagram, …), and fetches each remaining URL
once. We do not follow internal links: someone deliberately shared that
specific page, and the rest of the site wasn't curated by anyone in the
chat. depth-0 keeps the external corpus aligned with the chat's editorial
signal and cuts the noisy tail ~10× vs a deep crawl.

Two backends, used in this order per page:

  1. httpx + trafilatura — fast static HTML.
  2. Playwright (headless Chromium) — fallback for JS-rendered sites and
     anti-scraper WAFs. Browser is launched once and reused across all
     hosts that need it.

Output:

  data/external_text/chat_links/
    <host>/                          one directory per origin
      <slug>.txt                     [Source: …] + main text
    _manifest.json                   { url: { file, host, chars, backend, fetched_at } }
    _summary.json                    aggregate stats per host

Run:

  PYTHONPATH=. .venv/bin/python scripts/06_fetch_chat_links.py
  PYTHONPATH=. .venv/bin/python scripts/06_fetch_chat_links.py --hosts djezzy.dz creatorstudio.dz
  PYTHONPATH=. .venv/bin/python scripts/06_fetch_chat_links.py --rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
import warnings
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx
import trafilatura
import urllib3
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "data/messages_enriched.json"
OUT_DIR = ROOT / "data/external_text/chat_links"
MANIFEST_PATH = OUT_DIR / "_manifest.json"
SUMMARY_PATH = OUT_DIR / "_summary.json"


# ─── crawl parameters ───────────────────────────────────────────────────────
# We only fetch the URLs that were actually shared in chat — no descent.
# Someone deliberately posted that page; the rest of the site wasn't
# curated by anyone. depth-0 keeps the corpus aligned with the chat's
# editorial signal and cuts external noise ~10×.
MAX_PAGES_PER_HOST = 30   # still capped in case a host has many seeds
MAX_DEPTH = 0
MIN_USEFUL_CHARS = 300
HTTPX_TIMEOUT_S = 15.0
PLAYWRIGHT_NAVIGATE_TIMEOUT_S = 30.0
POLITE_DELAY_S = 0.7

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,en-US;q=0.9,fr;q=0.8,ar;q=0.7",
}

# Anything ending with these is binary or noise — skip extraction.
SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".css", ".js", ".mjs", ".map",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".mp4", ".mp3", ".webm", ".mov", ".avi", ".m4a",
    ".ico", ".woff", ".woff2", ".ttf",
}

# Path fragments that signal "not user-facing content".
SKIP_PATH_PARTS = (
    "/wp-json/", "/wp-admin/", "/wp-content/uploads/", "/wp-content/plugins/",
    "/admin/", "/login", "/logout", "/signin", "/sign-in", "/register",
    "/api/", "/feed/", "/rss/", "/sitemap", "/.well-known/",
    "/embed/", "/oembed/",
)

# Hosts we never crawl — login walls, pure form UI, social platforms.
SKIP_HOSTS = frozenset({
    "forms.gle", "forms.google.com",
    "linkedin.com", "lnkd.in",
    "instagram.com", "instagr.am",
    "facebook.com", "fb.com", "fb.me", "m.facebook.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "datacamp.com",
    "t.me", "telegram.me", "discord.gg", "discord.com",
    "twitter.com", "x.com",
})

# Hosts we already scrape via the dedicated 04/05 scripts — don't re-cover.
ALREADY_SCRAPED_HOSTS = frozenset({
    "ensia.edu.dz",
    "v2v.ensia.edu.dz",
})


# ─── URL handling ───────────────────────────────────────────────────────────

# Invisible Unicode "trailers" Telegram sometimes pastes onto URLs (RTL/LTR
# marks, word joiners, etc.) — strip them.
_INVISIBLE_TRAIL_RE = re.compile(
    r"[​-‏‪-‮⁠-⁤⁦-⁯]+$"
)
_URL_RE = re.compile(r'https?://[^\s<>"\)\]\}]+', re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Strip fragments, trailing punctuation, invisible chars, fix double-slashes."""
    url = _INVISIBLE_TRAIL_RE.sub("", url).rstrip(".,;:!?)\"'")
    url, _ = urldefrag(url)
    p = urlparse(url)
    host = (p.hostname or "").lower()
    netloc = host
    if p.port:
        netloc = f"{host}:{p.port}"
    path = re.sub(r"/+", "/", p.path or "/").rstrip("/") or "/"
    return urlunparse((p.scheme or "https", netloc, path, "", p.query, ""))


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip("www.")


def should_skip(url: str) -> bool:
    host = hostname(url)
    if host in SKIP_HOSTS or any(host.endswith("." + h) for h in SKIP_HOSTS):
        return True
    if host in ALREADY_SCRAPED_HOSTS:
        return True
    path_l = urlparse(url).path.lower()
    if any(path_l.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    if any(part in path_l for part in SKIP_PATH_PARTS):
        return True
    return False


# ─── seed extraction from chat ──────────────────────────────────────────────

def _flatten(tf) -> str:
    if isinstance(tf, str):
        return tf
    if isinstance(tf, list):
        return "".join(i if isinstance(i, str) else i.get("text", "") for i in tf)
    return ""


def extract_seed_urls() -> list[str]:
    with open(MESSAGES, encoding="utf-8") as f:
        raw = json.load(f)
    seen: set[str] = set()
    for m in raw["chats"]["list"][0]["messages"]:
        if m.get("type") != "message":
            continue
        for u in _URL_RE.findall(_flatten(m.get("text", ""))):
            n = normalize_url(u)
            if n.startswith("http") and not should_skip(n):
                seen.add(n)
        for ent in m.get("text_entities", []) or []:
            if isinstance(ent, dict):
                h = ent.get("href") or ""
                if h.startswith("http"):
                    n = normalize_url(h)
                    if not should_skip(n):
                        seen.add(n)
    return sorted(seen)


def group_by_host(urls: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for u in urls:
        out[hostname(u)].append(u)
    return dict(out)


# ─── fetchers ──────────────────────────────────────────────────────────────

# Quiet the InsecureRequestWarning we'd otherwise spam — many .dz sites have
# broken certs (intermediate chain issues), and we accept that risk because
# we're scraping public marketing pages, not logging in.
warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)


def fetch_httpx(url: str, client: httpx.Client) -> str | None:
    """Return raw HTML (or None on failure / non-HTML)."""
    try:
        r = client.get(url, timeout=HTTPX_TIMEOUT_S, follow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    if "html" not in r.headers.get("content-type", "").lower():
        return None
    return r.text


class PlaywrightFetcher:
    """Singleton headless Chromium that we keep alive across hosts."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        return self

    def __exit__(self, *args):
        try:
            if self._context: self._context.close()
            if self._browser: self._browser.close()
        finally:
            if self._pw: self._pw.stop()

    def fetch(self, url: str) -> str | None:
        page = self._context.new_page()
        try:
            page.goto(
                url,
                wait_until="networkidle",
                timeout=int(PLAYWRIGHT_NAVIGATE_TIMEOUT_S * 1000),
            )
            # Scroll once to trigger any in-view-only rendering.
            page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
            page.wait_for_timeout(800)
            return page.content()
        except Exception:
            return None
        finally:
            page.close()


# ─── parsing ───────────────────────────────────────────────────────────────

def extract(html: str, base_url: str) -> tuple[str, str, set[str]]:
    """Return (main_text, title, same-host internal links)."""
    text = (trafilatura.extract(html, include_comments=False, no_fallback=False)
            or "").strip()
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string.strip() if soup.title and soup.title.string
             else "")

    base_host = hostname(base_url)
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href)
        n = normalize_url(full)
        if hostname(n) != base_host:
            continue
        if should_skip(n):
            continue
        links.add(n)
    return text, title, links


# ─── crawling ──────────────────────────────────────────────────────────────

def safe_dir(host: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", host.lower()).strip("_") or "host"


def safe_slug(url: str) -> str:
    p = urlparse(url)
    path = p.path.strip("/") or "_root"
    stem = unicodedata.normalize("NFC", path)
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_") or "_root"
    if p.query:
        h = hashlib.sha1(p.query.encode()).hexdigest()[:6]
        stem = f"{stem}_{h}"
    # Keep filenames sane; long paths are common on news sites.
    return stem[:120]


def crawl_host(
    host: str,
    seeds: list[str],
    pw: PlaywrightFetcher | None,
    cache: dict,
) -> list[dict]:
    """BFS over same-host pages reachable from any seed URL. Returns a list
    of page dicts {url, title, text, backend, depth, chars}."""
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    pages: list[dict] = []

    with httpx.Client(headers=HTTP_HEADERS, verify=False) as client:
        while queue and len(pages) < MAX_PAGES_PER_HOST:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            # Cache hit → trust it, don't refetch.
            cached = cache.get(url)
            if cached and Path(cached["file"]).exists() and cached.get("chars", 0) >= MIN_USEFUL_CHARS:
                pages.append({
                    "url": url,
                    "title": cached.get("title", ""),
                    "text": None,             # not loaded; signals "already on disk"
                    "backend": cached.get("backend", "cache"),
                    "depth": depth,
                    "chars": cached["chars"],
                })
                continue

            html = fetch_httpx(url, client)
            backend = "httpx"
            if html is None and pw is not None:
                html = pw.fetch(url)
                backend = "playwright"
            if html is None:
                print(f"      ✗ {url}  (both backends failed)")
                continue

            text, title, internal = extract(html, url)
            if len(text) < MIN_USEFUL_CHARS:
                # Even on thin pages, follow links — they might lead to
                # better content one level deeper.
                if depth < MAX_DEPTH:
                    for link in internal:
                        if link not in visited:
                            queue.append((link, depth + 1))
                print(f"      ⚠ {url}  (thin: {len(text)} chars)")
                time.sleep(POLITE_DELAY_S)
                continue

            pages.append({
                "url": url,
                "title": title,
                "text": text,
                "backend": backend,
                "depth": depth,
                "chars": len(text),
            })
            print(f"      ✓ {url}  ({backend}, {len(text)} chars)")

            if depth < MAX_DEPTH:
                for link in internal:
                    if link not in visited:
                        queue.append((link, depth + 1))

            time.sleep(POLITE_DELAY_S)

    return pages


# ─── persistence ───────────────────────────────────────────────────────────

def save_page(host: str, page: dict) -> Path:
    out_dir = OUT_DIR / safe_dir(host)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = safe_slug(page["url"])
    path = out_dir / f"{slug}.txt"
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        f"[Source: {page['url']}]\n"
        f"[Title: {page.get('title', '')}]\n"
        f"[Backend: {page.get('backend', '')}]\n"
        f"[Fetched: {fetched_at}]\n\n"
    )
    path.write_text(header + page["text"], encoding="utf-8")
    return path


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(manifest: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── main ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--hosts", nargs="*", default=None,
        help="Restrict the crawl to these hostnames (e.g. 'djezzy.dz anpt.dz').",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Ignore the manifest cache and re-fetch every URL.",
    )
    parser.add_argument(
        "--no-playwright", action="store_true",
        help="Skip the Playwright fallback (httpx only — useful for quick runs).",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    seeds = extract_seed_urls()
    grouped = group_by_host(seeds)

    if args.hosts:
        targeted = set()
        for h in args.hosts:
            h_norm = h.lower().lstrip("www.")
            for host in grouped:
                if host == h_norm or host.endswith("." + h_norm):
                    targeted.add(host)
        grouped = {h: grouped[h] for h in targeted}
        if not grouped:
            print(f"No matching hosts among seed URLs. Available:")
            for h in sorted(group_by_host(seeds)):
                print(f"  {h}")
            return

    print(f"Will crawl {len(grouped)} host{'s' if len(grouped) != 1 else ''}: "
          f"{', '.join(sorted(grouped))}\n")

    cache = {} if args.rebuild else load_manifest()

    pw_ctx: PlaywrightFetcher | None = None
    if not args.no_playwright:
        print("Launching Playwright Chromium…")
        pw_ctx = PlaywrightFetcher().__enter__()

    try:
        manifest = {} if args.rebuild else dict(cache)
        host_stats: list[dict] = []

        for i, (host, host_seeds) in enumerate(sorted(grouped.items()), 1):
            print(f"\n[{i}/{len(grouped)}] {host}  ({len(host_seeds)} seed{'s' if len(host_seeds) != 1 else ''})")
            pages = crawl_host(host, host_seeds, pw_ctx, cache)

            host_total_chars = 0
            for p in pages:
                # text=None means a cache hit; nothing to rewrite.
                if p["text"] is not None:
                    path = save_page(host, p)
                    manifest[p["url"]] = {
                        "host": host,
                        "file": str(path.relative_to(ROOT)),
                        "title": p["title"],
                        "chars": p["chars"],
                        "backend": p["backend"],
                        "fetched_at": datetime.now(timezone.utc)
                                              .isoformat(timespec="seconds"),
                    }
                host_total_chars += p["chars"]

            host_stats.append({
                "host": host,
                "pages": len(pages),
                "chars": host_total_chars,
            })
            print(f"   → {len(pages)} page(s), {host_total_chars:,} chars total")

        save_manifest(manifest)

        # Aggregate summary.
        SUMMARY_PATH.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hosts": sorted(host_stats, key=lambda s: -s["chars"]),
            "totals": {
                "hosts": len(host_stats),
                "pages": sum(s["pages"] for s in host_stats),
                "chars": sum(s["chars"] for s in host_stats),
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "=" * 70)
        print("DONE")
        print("=" * 70)
        for s in sorted(host_stats, key=lambda s: -s["chars"]):
            print(f"  {s['host']:<35} {s['pages']:>3} pages   {s['chars']:>7,} chars")
        print(f"\n  Total: {sum(s['pages'] for s in host_stats)} pages, "
              f"{sum(s['chars'] for s in host_stats):,} chars across "
              f"{len(host_stats)} hosts")
        print(f"  Saved manifest: {MANIFEST_PATH.relative_to(ROOT)}")

    finally:
        if pw_ctx is not None:
            pw_ctx.__exit__(None, None, None)


if __name__ == "__main__":
    main()

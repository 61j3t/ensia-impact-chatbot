"""Snapshot the state of the corpus and write data/_status.json.

The dashboard reads this single JSON file to render the /data view —
keeps the Next.js side dependency-free (no Python, no ChromaDB driver).

Run standalone or as the final stage of run_pipeline.sh:
    .venv/bin/python scripts/07_status_snapshot.py
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import chromadb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
EXT = DATA / "external_text"
INDEX_DB = ROOT / "chatbot/chroma_db"
INDEX_FILE = INDEX_DB / "chroma.sqlite3"
OUT = DATA / "_status.json"


def _mtime(p: Path) -> str | None:
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def _read_json(p: Path):
    if not p.exists():
        return None
    with p.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Chat messages

def chat_section() -> dict:
    payload = _read_json(DATA / "messages_enriched.json") or {}
    chats = (payload.get("chats") or {}).get("list") or []
    msgs = chats[0]["messages"] if chats else []

    content = [m for m in msgs if m.get("type") == "message"]
    service = [m for m in msgs if m.get("type") == "service"]

    # Topic names from topic_created service messages
    topics = Counter()
    topic_lookup: dict[int, str] = {}
    for m in service:
        if m.get("action") == "topic_created":
            topic_lookup[m["id"]] = m.get("title") or "?"

    # Assign each content message to its topic via reply_to_message_id chain.
    # Simplified: the chunks.py logic already handles this — here we just
    # count by reply_to_top_id or fallback "General".
    for m in content:
        top = m.get("reply_to_message_id")
        # Walk up — but topic ids are roots, so just check direct
        name = topic_lookup.get(top, "General")
        topics[name] += 1

    dates = sorted(m["date"] for m in content if m.get("date"))
    return {
        "total_messages": len(msgs),
        "content_messages": len(content),
        "service_messages": len(service),
        "first_message": dates[0] if dates else None,
        "last_message": dates[-1] if dates else None,
        "topics_top": topics.most_common(8),
        "topics_total": len(topic_lookup),
        "source_file_mtime": _mtime(DATA / "messages_enriched.json"),
    }


# ---------------------------------------------------------------------------
# PDFs

def pdf_section() -> dict:
    native = _read_json(DATA / "extracted_text/_summary.json") or []
    ocr = _read_json(DATA / "ocr_text/_summary.json") or {}
    ocr_pdfs = ocr.get("pdfs") or []

    native_nonempty = [p for p in native if p.get("chars", 0) > 0]
    return {
        "native": {
            "files": len(native),
            "non_empty": len(native_nonempty),
            "total_chars": sum(p.get("chars", 0) for p in native),
            "items": [
                {
                    "file": p["file"],
                    "pages": p.get("pages"),
                    "chars": p.get("chars", 0),
                    "language": p.get("language"),
                }
                for p in native
            ],
            "summary_mtime": _mtime(DATA / "extracted_text/_summary.json"),
        },
        "ocr": {
            "files": len(ocr_pdfs),
            "total_chars": sum(p.get("chars", 0) for p in ocr_pdfs),
            "photos_ocr_count": ocr.get("photos_ocr_count", 0),
            "photos_total_chars": ocr.get("photos_total_chars", 0),
            "items": [
                {
                    "file": p["file"],
                    "pages": p.get("pages"),
                    "chars": p.get("chars", 0),
                }
                for p in ocr_pdfs
            ],
            "summary_mtime": _mtime(DATA / "ocr_text/_summary.json"),
        },
    }


# ---------------------------------------------------------------------------
# Web sources

def ensia_section() -> dict:
    items = _read_json(EXT / "ensia_edu_dz/_summary.json") or []
    by_lang = Counter(x.get("language") for x in items)
    by_kind = Counter(x.get("kind") for x in items)
    last_mod = max(
        (x.get("modified") for x in items if x.get("modified")),
        default=None,
    )
    return {
        "pages": len(items),
        "total_chars": sum(x.get("char_count", 0) for x in items),
        "by_language": dict(by_lang),
        "by_kind": dict(by_kind),
        "last_modified_seen": last_mod,
        "summary_mtime": _mtime(EXT / "ensia_edu_dz/_summary.json"),
    }


def v2v_section() -> dict:
    items = _read_json(EXT / "v2v_ensia/_summary.json") or []
    return {
        "pages": len(items),
        "total_chars": sum(x.get("char_count", 0) for x in items),
        "last_modified_seen": max(
            (x.get("modified") for x in items if x.get("modified")),
            default=None,
        ),
        "summary_mtime": _mtime(EXT / "v2v_ensia/_summary.json"),
    }


def chat_links_section() -> dict:
    manifest = _read_json(EXT / "chat_links/_manifest.json") or {}
    summary = _read_json(EXT / "chat_links/_summary.json") or {}

    by_host: dict[str, dict] = {}
    for url, entry in manifest.items():
        h = entry.get("host", "?")
        slot = by_host.setdefault(
            h,
            {
                "host": h,
                "pages": 0,
                "chars": 0,
                "last_fetched": None,
                "backends": Counter(),
            },
        )
        slot["pages"] += 1
        slot["chars"] += entry.get("chars", 0)
        fetched = entry.get("fetched_at")
        if fetched and (slot["last_fetched"] is None or fetched > slot["last_fetched"]):
            slot["last_fetched"] = fetched
        if entry.get("backend"):
            slot["backends"][entry["backend"]] += 1

    hosts = []
    for h in by_host.values():
        hosts.append(
            {
                "host": h["host"],
                "pages": h["pages"],
                "chars": h["chars"],
                "last_fetched": h["last_fetched"],
                "backend": h["backends"].most_common(1)[0][0] if h["backends"] else None,
            }
        )
    hosts.sort(key=lambda x: x["chars"], reverse=True)

    return {
        "total_urls": len(manifest),
        "total_hosts": len(by_host),
        "total_chars": sum(h["chars"] for h in hosts),
        "generated_at": summary.get("generated_at"),
        "hosts": hosts,
        "manifest_mtime": _mtime(EXT / "chat_links/_manifest.json"),
    }


# ---------------------------------------------------------------------------
# Retrieval index

def index_section() -> dict:
    if not INDEX_FILE.exists():
        return {"built": False}

    try:
        client = chromadb.PersistentClient(path=str(INDEX_DB))
        col = client.get_or_create_collection(name="ensia")
        result = col.get(include=["metadatas", "documents"])
    except Exception as e:
        return {"built": True, "error": str(e), "index_mtime": _mtime(INDEX_FILE)}

    metas = result.get("metadatas") or []
    docs = result.get("documents") or []

    by_source: Counter = Counter()
    by_site: Counter = Counter()
    for m in metas:
        by_source[m.get("source_type") or "?"] += 1
        if m.get("source_type") == "external":
            by_site[m.get("site") or "?"] += 1

    enriched = sum(1 for d in docs if d and "— Linked page:" in d)
    chat_total = by_source.get("chat", 0)

    return {
        "built": True,
        "total_chunks": len(metas),
        "by_source": dict(by_source),
        "by_external_site": dict(by_site),
        "enriched_chat_chunks": enriched,
        "chat_chunks_total": chat_total,
        "index_mtime": _mtime(INDEX_FILE),
    }


# ---------------------------------------------------------------------------

def main() -> None:
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chat": chat_section(),
        "pdfs": pdf_section(),
        "web": {
            "ensia_edu_dz": ensia_section(),
            "v2v_ensia": v2v_section(),
            "chat_links": chat_links_section(),
        },
        "index": index_section(),
    }

    OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(f"✓ Wrote {OUT.relative_to(ROOT)}")
    print(f"  chat content msgs: {snapshot['chat']['content_messages']}")
    print(f"  PDFs (native+OCR): {snapshot['pdfs']['native']['files']} + {snapshot['pdfs']['ocr']['files']}")
    print(f"  ensia.edu.dz pages: {snapshot['web']['ensia_edu_dz']['pages']}")
    print(f"  chat_links hosts/urls: {snapshot['web']['chat_links']['total_hosts']} / {snapshot['web']['chat_links']['total_urls']}")
    print(f"  index chunks: {snapshot['index'].get('total_chunks', '—')}")


if __name__ == "__main__":
    main()

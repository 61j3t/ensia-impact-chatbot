"""Build the corpus of chunks ready for embedding.

Sources combined:
  - data/messages_enriched.json   — chat messages (with OCR'd photo content merged in)
  - data/extracted_text/*.txt     — text from native-text PDFs (skips empty placeholders)
  - data/ocr_text/*.txt           — OCR'd output of scanned PDFs

Each chunk is a dict with the structure:
    {
      "id": "chat_832" | "pdf_ENSIA_0",
      "text": "<the text to embed>",
      "metadata": {
        "source_type": "chat" | "pdf",
        "topic":       str | None,
        "language":    "en" | "ar" | "fr" | "empty",
        "message_id":  int | None,
        "pdf_file":    str | None,
        "chunk_index": int | None,    # which chunk of the PDF
        "date":        ISO str | None,
        "sender":      str | None,
        "text_source": "original" | "ocr" | None,  # for chat msgs
      }
    }
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "data/messages_enriched.json"
EXTRACTED = ROOT / "data/extracted_text"
OCR = ROOT / "data/ocr_text"


# ─── chunking parameters ────────────────────────────────────────────────────
# ~512 tokens at ~4 chars/token. Overlap 64 tokens so context isn't lost at
# chunk boundaries.
PDF_MAX_CHARS = 2000
PDF_OVERLAP_CHARS = 250
# Below this, a chat message is too short to be useful on its own.
MIN_CHAT_CHARS = 20


# ─── helpers ────────────────────────────────────────────────────────────────

def flatten_text(tf) -> str:
    if isinstance(tf, str):
        return tf
    if isinstance(tf, list):
        return "".join(i if isinstance(i, str) else i.get("text", "") for i in tf)
    return ""


def detect_language(text: str) -> str:
    sample = text[:5000]
    if not sample.strip():
        return "empty"
    arabic_chars = len(re.findall(r"[؀-ۿ]", sample))
    if arabic_chars > 30:
        return "ar"
    french_words = {
        "les", "des", "est", "pour", "dans", "une", "avec", "sur", "que", "qui",
        "nous", "vous", "pas", "sont", "cette", "aux", "par", "ont", "le", "la",
        "du", "ce", "se", "ne",
    }
    words = re.findall(r"[a-zA-ZÀ-ÿ]+", sample.lower())
    if not words:
        return "ar" if arabic_chars else "empty"
    french_hits = sum(1 for w in words if w in french_words)
    if french_hits / len(words) > 0.05:
        return "fr"
    return "en"


def split_pdf_text(text: str) -> list[str]:
    """Recursive split: paragraphs → ~PDF_MAX_CHARS chunks with overlap."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        # If a single paragraph is huge, hard-split it.
        if len(p) > PDF_MAX_CHARS:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(p):
                end = min(start + PDF_MAX_CHARS, len(p))
                chunks.append(p[start:end])
                if end == len(p):
                    break
                start = end - PDF_OVERLAP_CHARS
            continue

        candidate = f"{current}\n\n{p}" if current else p
        if len(candidate) <= PDF_MAX_CHARS:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = p

    if current:
        chunks.append(current)

    # Stitch a small overlap between consecutive paragraph-built chunks so
    # context is preserved at boundaries.
    if PDF_OVERLAP_CHARS > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for prev, curr in zip(chunks, chunks[1:]):
            tail = prev[-PDF_OVERLAP_CHARS:]
            overlapped.append(tail + "\n\n" + curr)
        chunks = overlapped

    return chunks


# ─── chat message chunks ────────────────────────────────────────────────────

def _build_topic_resolver(all_messages):
    topic_map = {
        m["id"]: m.get("title", "")
        for m in all_messages
        if m.get("action") == "topic_created"
    }
    by_id = {m["id"]: m for m in all_messages}

    def resolve(msg):
        visited = set()
        cur = msg
        while cur:
            rid = cur.get("reply_to_message_id")
            if rid is None:
                return None
            if rid in topic_map:
                return topic_map[rid]
            if rid in visited:
                return None
            visited.add(rid)
            cur = by_id.get(rid)
        return None

    return resolve


def chat_chunks() -> Iterator[dict]:
    with open(MESSAGES, encoding="utf-8") as f:
        raw = json.load(f)
    all_messages = raw["chats"]["list"][0]["messages"]
    resolve_topic = _build_topic_resolver(all_messages)

    for m in all_messages:
        if m.get("type") != "message":
            continue
        text = flatten_text(m.get("text", "")).strip()
        if len(text) < MIN_CHAT_CHARS:
            continue

        yield {
            "id": f"chat_{m['id']}",
            "text": text,
            "metadata": {
                "source_type": "chat",
                "topic": resolve_topic(m),
                "language": detect_language(text),
                "message_id": m["id"],
                "pdf_file": None,
                "chunk_index": None,
                "date": m.get("date"),
                "sender": m.get("from"),
                "text_source": m.get("text_source"),
            },
        }


# ─── PDF chunks (native + OCR'd) ────────────────────────────────────────────

def _safe_id_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = unicodedata.normalize("NFC", stem)
    return re.sub(r"\W+", "_", stem, flags=re.UNICODE).strip("_")


def pdf_chunks() -> Iterator[dict]:
    """Yield chunks from extracted_text/ + ocr_text/, deduping on stem."""
    seen_stems: set[str] = set()

    # extracted_text first (native text usually cleaner than OCR).
    for path in sorted(EXTRACTED.glob("*.txt")):
        if path.name.startswith("_"):
            continue
        if path.stat().st_size == 0:
            continue  # empty placeholder for scanned PDFs (handled by OCR pass)
        stem = unicodedata.normalize("NFC", path.stem)
        seen_stems.add(stem)
        yield from _chunks_from_pdf_file(path)

    # OCR PDFs — only those whose stem isn't already covered.
    for path in sorted(OCR.glob("*.txt")):
        if path.name.startswith("_"):
            continue
        stem = unicodedata.normalize("NFC", path.stem)
        # OCR'd filenames sometimes differ slightly from the native extract
        # (e.g. "Arrete_008" vs "Arreté_008"). Normalize and skip if covered.
        if stem in seen_stems:
            continue
        if path.stat().st_size == 0:
            continue
        seen_stems.add(stem)
        yield from _chunks_from_pdf_file(path)


def _chunks_from_pdf_file(path: Path) -> Iterator[dict]:
    text = path.read_text(encoding="utf-8")
    pieces = split_pdf_text(text)
    safe_id = _safe_id_from_filename(path.name)
    for i, piece in enumerate(pieces):
        yield {
            "id": f"pdf_{safe_id}_{i}",
            "text": piece,
            "metadata": {
                "source_type": "pdf",
                "topic": None,
                "language": detect_language(piece),
                "message_id": None,
                "pdf_file": path.name,
                "chunk_index": i,
                "date": None,
                "sender": None,
                "text_source": "ocr" if path.parent.name == "ocr_text" else "original",
            },
        }


# ─── unified loader ─────────────────────────────────────────────────────────

def all_chunks() -> list[dict]:
    return [*chat_chunks(), *pdf_chunks()]


if __name__ == "__main__":
    chunks = all_chunks()
    by_kind = {}
    for c in chunks:
        k = c["metadata"]["source_type"]
        by_kind[k] = by_kind.get(k, 0) + 1
    by_lang = {}
    for c in chunks:
        l = c["metadata"]["language"]
        by_lang[l] = by_lang.get(l, 0) + 1

    print(f"Total chunks: {len(chunks)}")
    print(f"By source: {by_kind}")
    print(f"By language: {by_lang}")
    print(f"\nFirst 3 chunks:")
    for c in chunks[:3]:
        print(f"  {c['id']} ({c['metadata']['source_type']}, {c['metadata']['language']}, {len(c['text'])} chars)")

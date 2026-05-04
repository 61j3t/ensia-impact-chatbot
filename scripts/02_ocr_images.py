"""OCR all photo-only Telegram messages and the 2 scanned PDFs.

Track A: OCR photos from messages with no text caption (the chatbot can't
         see these without OCR — Q&A topic is 71% photos).
Track B: OCR the 2 scanned PDF decrees (Arreté 008, Arrêté 1275).

By default this script is INCREMENTAL — it loads any existing
data/ocr_text/photos.json and only OCRs messages that aren't already
recorded with a successful result. Use --rebuild to force re-OCR of
everything.

Outputs:
  - data/ocr_text/photos.json           — message_id → ocr_text mapping
  - data/ocr_text/<pdf_name>.txt        — extracted text for scanned PDFs
  - data/ocr_text/_summary.json         — stats
"""

import argparse
import json
import re
from pathlib import Path

import pymupdf
import pytesseract
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PHOTOS_DIR = ROOT / "data/chats/chat_1/photos"
PDF_DIR = ROOT / "data/chats/chat_1/files"
OUT_DIR = ROOT / "data/ocr_text"
OUT_DIR.mkdir(parents=True, exist_ok=True)

OCR_LANGS = "ara+fra+eng"


def flatten_text(tf):
    if isinstance(tf, str):
        return tf
    if isinstance(tf, list):
        return "".join(i if isinstance(i, str) else i.get("text", "") for i in tf)
    return ""


def clean_ocr(text: str) -> str:
    lines = [re.sub(r" {2,}", " ", line.strip()) for line in text.splitlines()]
    out, blank = [], 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip()


def ocr_image(img_path: Path) -> str:
    img = Image.open(img_path)
    text = pytesseract.image_to_string(img, lang=OCR_LANGS)
    return clean_ocr(text)


def find_photo_only_messages():
    """Return list of (message_id, photo_path, topic_title) for photo-only msgs."""
    with open(ROOT / "data/result.json", encoding="utf-8") as f:
        raw = json.load(f)
    all_msgs = raw["chats"]["list"][0]["messages"]

    # Build topic map
    topic_map = {m["id"]: m.get("title", "") for m in all_msgs if m.get("action") == "topic_created"}
    msgs_by_id = {m["id"]: m for m in all_msgs}

    def resolve_topic(msg):
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
            cur = msgs_by_id.get(rid)
        return None

    results = []
    for m in all_msgs:
        if m["type"] != "message":
            continue
        if "photo" not in m:
            continue
        text = flatten_text(m.get("text", ""))
        if text.strip():  # has caption — already useful
            continue
        photo_rel = m["photo"]
        photo_path = ROOT / "data" / photo_rel
        if not photo_path.exists():
            continue
        results.append({
            "id": m["id"],
            "photo": str(photo_path.relative_to(ROOT)),
            "topic": resolve_topic(m),
            "sender": m.get("from", "Unknown"),
            "date": m.get("date", ""),
        })
    return results


def track_a_photos(rebuild: bool = False):
    print("=" * 60)
    print("TRACK A: OCR photo-only messages")
    print("=" * 60)

    out_path = OUT_DIR / "photos.json"
    cached_by_id: dict = {}
    if out_path.exists() and not rebuild:
        cached_by_id = {
            r["id"]: r
            for r in json.loads(out_path.read_text(encoding="utf-8"))
            if r.get("char_count", 0) > 20  # re-try low-text/failed ones
        }
        if cached_by_id:
            print(f"Resuming from cache: {len(cached_by_id)} photos already OCR'd")

    targets = find_photo_only_messages()
    todo = [t for t in targets if t["id"] not in cached_by_id]
    print(f"Found {len(targets)} photo-only messages, {len(todo)} need OCR\n")

    results = list(cached_by_id.values())
    for i, t in enumerate(todo, 1):
        try:
            text = ocr_image(ROOT / t["photo"])
        except Exception as e:
            text = ""
            err = str(e)
            print(f"  [{i}/{len(todo)}] msg {t['id']} ({t['topic']}) — FAILED: {err}")
            results.append({**t, "ocr_text": "", "char_count": 0, "error": err})
            continue

        char_count = len(text)
        results.append({**t, "ocr_text": text, "char_count": char_count})
        status = "✓" if char_count > 20 else "⚠ low text"
        print(f"  [{i}/{len(todo)}] msg {t['id']} ({t['topic']}) — {status} {char_count} chars")

    # Stable order by message id for clean diffs.
    results.sort(key=lambda r: r["id"])
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    successful = [r for r in results if r["char_count"] > 20]
    total_chars = sum(r["char_count"] for r in successful)
    print(f"\n  Saved {len(results)} OCR results to {out_path.relative_to(ROOT)}")
    print(f"  {len(successful)} photos yielded substantive text (>20 chars)")
    print(f"  Total OCR text extracted: {total_chars:,} chars")

    # Per-topic breakdown
    from collections import Counter
    topic_chars = {}
    for r in successful:
        topic = r["topic"] or "_unassigned"
        topic_chars[topic] = topic_chars.get(topic, 0) + r["char_count"]
    print(f"\n  By topic (chars recovered):")
    for topic, chars in sorted(topic_chars.items(), key=lambda x: -x[1]):
        count = sum(1 for r in successful if (r["topic"] or "_unassigned") == topic)
        print(f"    {topic}: {chars:,} chars ({count} photos)")
    return results


def track_b_pdfs(rebuild: bool = False):
    print("\n" + "=" * 60)
    print("TRACK B: OCR scanned PDF decrees")
    print("=" * 60)

    scanned_pdfs = ["Arreté 008.pdf", "Arrêté 1275.pdf"]
    results = []
    for pdf_name in scanned_pdfs:
        pdf_path = PDF_DIR / pdf_name
        if not pdf_path.exists():
            print(f"  ✗ {pdf_name}: not found")
            continue

        out_name = re.sub(r"[^\w\-]+", "_", Path(pdf_name).stem) + ".txt"
        out_path = OUT_DIR / out_name
        if out_path.exists() and not rebuild and out_path.stat().st_size > 0:
            existing = out_path.read_text(encoding="utf-8")
            print(f"  • {pdf_name}: cached ({len(existing):,} chars), skipping")
            results.append({
                "file": pdf_name,
                "pages": existing.count("\n\n") + 1,
                "chars": len(existing),
                "words": len(existing.split()),
                "output": str(out_path.relative_to(ROOT)),
            })
            continue

        doc = pymupdf.open(pdf_path)
        page_texts = []
        for page_num, page in enumerate(doc, 1):
            # Render page to image at 300 DPI for OCR
            pix = page.get_pixmap(dpi=300)
            img_bytes = pix.tobytes("png")
            from io import BytesIO
            img = Image.open(BytesIO(img_bytes))
            text = pytesseract.image_to_string(img, lang=OCR_LANGS)
            page_texts.append(clean_ocr(text))
            print(f"  ✓ {pdf_name} page {page_num}/{len(doc)}: {len(text)} chars")
        doc.close()

        full_text = "\n\n".join(page_texts)
        out_path.write_text(full_text, encoding="utf-8")

        results.append({
            "file": pdf_name,
            "pages": len(page_texts),
            "chars": len(full_text),
            "words": len(full_text.split()),
            "output": str(out_path.relative_to(ROOT)),
        })
        print(f"     → {out_path.name}: {len(full_text):,} chars total")

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--rebuild", action="store_true",
                        help="Force re-OCR of all photos and scanned PDFs")
    args = parser.parse_args()

    photo_results = track_a_photos(rebuild=args.rebuild)
    pdf_results = track_b_pdfs(rebuild=args.rebuild)

    summary = {
        "photos_ocr_count": sum(1 for r in photo_results if r["char_count"] > 20),
        "photos_total_chars": sum(r["char_count"] for r in photo_results),
        "pdfs": pdf_results,
    }
    (OUT_DIR / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n" + "=" * 60)
    print("OCR COMPLETE")
    print("=" * 60)
    print(f"Output dir: {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()

"""Merge OCR text from photo-only messages back into the chat data.

For each Telegram message that had a photo and empty text, replace its
empty `text` with the OCR'd text. This closes the EDA's "84 photo-only
messages" content gap so the chatbot can index everything through one
unified pipeline.

Inputs:
  - data/result.json              — original Telegram export
  - data/ocr_text/photos.json     — OCR results by message id

Output:
  - data/messages_enriched.json   — chat data with OCR merged in

For audit/debug, each enriched message gets these extra fields:
  - text_source:    "original" | "ocr" | "ocr_failed"
  - original_text:  the original text field (preserved for reference)
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULT_JSON = ROOT / "data/result.json"
OCR_PHOTOS = ROOT / "data/ocr_text/photos.json"
OUT_PATH = ROOT / "data/messages_enriched.json"

OCR_MIN_CHARS = 20  # below this, treat OCR as failed (matches Track A threshold)


def flatten_text(tf):
    if isinstance(tf, str):
        return tf
    if isinstance(tf, list):
        return "".join(i if isinstance(i, str) else i.get("text", "") for i in tf)
    return ""


def main():
    with open(RESULT_JSON, encoding="utf-8") as f:
        raw = json.load(f)

    with open(OCR_PHOTOS, encoding="utf-8") as f:
        ocr_results = json.load(f)
    ocr_by_id = {r["id"]: r for r in ocr_results}

    chat = raw["chats"]["list"][0]
    messages = chat["messages"]

    merged_count = 0
    failed_count = 0
    untouched_count = 0

    for msg in messages:
        if msg.get("type") != "message":
            continue

        original_text = flatten_text(msg.get("text", ""))
        msg["original_text"] = original_text

        ocr = ocr_by_id.get(msg["id"])
        if ocr is None:
            msg["text_source"] = "original"
            untouched_count += 1
            continue

        ocr_text = ocr.get("ocr_text", "")
        if len(ocr_text) >= OCR_MIN_CHARS:
            msg["text"] = ocr_text
            msg["text_entities"] = [{"type": "plain", "text": ocr_text}]
            msg["text_source"] = "ocr"
            merged_count += 1
        else:
            msg["text_source"] = "ocr_failed"
            failed_count += 1

    new_content = json.dumps(raw, ensure_ascii=False, indent=1)
    # Only touch the output file if the content actually changed — otherwise
    # downstream stages (e.g. the index build) needlessly think data updated.
    if OUT_PATH.exists() and OUT_PATH.read_text(encoding="utf-8") == new_content:
        changed = False
    else:
        OUT_PATH.write_text(new_content, encoding="utf-8")
        changed = True

    print("=" * 60)
    print("MERGE COMPLETE")
    print("=" * 60)
    print(f"Messages with OCR text merged: {merged_count}")
    print(f"Messages where OCR yielded no usable text: {failed_count}")
    print(f"Messages untouched (had original text or no photo): {untouched_count}")
    print(f"\nOutput: {OUT_PATH.relative_to(ROOT)} ({'updated' if changed else 'unchanged'})")
    print(f"Size:   {OUT_PATH.stat().st_size / 1024:.0f} KB")

    # Quick verification: count messages with usable text now
    text_msgs = [m for m in messages if m.get("type") == "message"]
    has_text = sum(1 for m in text_msgs if flatten_text(m.get("text", "")).strip())
    print(f"\nContent messages with text (before): {sum(1 for m in text_msgs if m.get('original_text', '').strip())}")
    print(f"Content messages with text (after):  {has_text}")
    print(f"Net gain from OCR: +{has_text - sum(1 for m in text_msgs if m.get('original_text', '').strip())} messages")


if __name__ == "__main__":
    main()

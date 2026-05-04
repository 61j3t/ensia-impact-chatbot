"""Validate the golden test set against the actual corpus.

Checks for each query:
  - Every expected_sources message id exists in messages_enriched.json
  - Every expected_sources PDF file exists in extracted_text/ or ocr_text/
  - The message/PDF text contains the `must_contain` substring (if specified)
  - At least one expected_keyword is found in the source text (sanity check
    that the query and source are actually related)
  - Adversarial queries have empty expected_sources and must_refuse=true

Run: .venv/bin/python eval/validate_golden_set.py
"""

import json
import unicodedata
from pathlib import Path


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)

import argparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = ROOT / "eval/golden_set.json"
MESSAGES = ROOT / "data/messages_enriched.json"
EXTRACTED = ROOT / "data/extracted_text"
OCR = ROOT / "data/ocr_text"


def flatten_text(tf):
    if isinstance(tf, str):
        return tf
    if isinstance(tf, list):
        return "".join(i if isinstance(i, str) else i.get("text", "") for i in tf)
    return ""


def find_pdf(name):
    target = nfc(name)
    for d in (EXTRACTED, OCR):
        for candidate in d.glob("*.txt"):
            if nfc(candidate.name) == target:
                return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description="Validate a query set against the corpus")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_SET),
                        help="Path to query set JSON (default: eval/golden_set.json)")
    args = parser.parse_args()

    with open(args.path, encoding="utf-8") as f:
        golden = json.load(f)

    with open(MESSAGES, encoding="utf-8") as f:
        raw = json.load(f)
    msgs_by_id = {m["id"]: m for m in raw["chats"]["list"][0]["messages"]}

    errors = []
    warnings = []
    ok = 0

    for q in golden["queries"]:
        qid = q["id"]
        is_adversarial = q.get("must_refuse", False)

        if is_adversarial:
            if q["expected_sources"]:
                errors.append(f"{qid}: adversarial query must have empty expected_sources")
            if q["expected_keywords"]:
                warnings.append(f"{qid}: adversarial query has expected_keywords (will be ignored)")
            ok += 1
            continue

        if not q["expected_sources"]:
            errors.append(f"{qid}: non-adversarial query has empty expected_sources")
            continue

        for src in q["expected_sources"]:
            if src["kind"] == "message":
                mid = src["ref"]
                if mid not in msgs_by_id:
                    errors.append(f"{qid}: message {mid} not found in corpus")
                    continue
                msg = msgs_by_id[mid]
                text = flatten_text(msg.get("text", ""))
                if not text.strip():
                    warnings.append(f"{qid}: message {mid} has empty text")
                    continue
                # Sanity check: at least one expected_keyword should appear
                hits = [k for k in q["expected_keywords"] if k.lower() in text.lower()]
                if not hits:
                    warnings.append(
                        f"{qid}: message {mid} contains NONE of the expected keywords "
                        f"({q['expected_keywords']}) — query/source may be mismatched"
                    )

            elif src["kind"] == "pdf":
                pdf_path = find_pdf(src["ref"])
                if pdf_path is None:
                    errors.append(f"{qid}: PDF {src['ref']!r} not found")
                    continue
                text = pdf_path.read_text(encoding="utf-8")
                if "must_contain" in src:
                    if src["must_contain"].lower() not in text.lower():
                        errors.append(
                            f"{qid}: PDF {src['ref']} does not contain {src['must_contain']!r}"
                        )

            else:
                errors.append(f"{qid}: unknown kind {src['kind']!r}")

        if not any(e.startswith(f"{qid}:") for e in errors):
            ok += 1

    print("=" * 60)
    print("GOLDEN SET VALIDATION")
    print("=" * 60)
    total = len(golden["queries"])
    print(f"Total queries:     {total}")
    print(f"Validated cleanly: {ok}")
    print(f"Errors:            {len(errors)}")
    print(f"Warnings:          {len(warnings)}")

    # Category breakdown
    from collections import Counter
    cats = Counter(q["category"] for q in golden["queries"])
    langs = Counter(q["language"] for q in golden["queries"])
    print(f"\nCategories: {dict(cats)}")
    print(f"Languages:  {dict(langs)}")
    print(f"Adversarial (must_refuse=true): {sum(1 for q in golden['queries'] if q.get('must_refuse'))}")

    if errors:
        print("\n--- ERRORS ---")
        for e in errors:
            print(f"  ✗ {e}")
    if warnings:
        print("\n--- WARNINGS ---")
        for w in warnings:
            print(f"  ⚠ {w}")

    print()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Extract text from all PDFs in data/chats/chat_1/files/ for chatbot indexing.

Outputs:
  - data/extracted_text/<pdf_name>.txt  — plain text per PDF
  - data/extracted_text/_summary.json   — extraction stats per PDF
"""

import json
import re
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data/chats/chat_1/files"
OUT_DIR = ROOT / "data/extracted_text"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def detect_language(text: str) -> str:
    sample = text[:5000]
    if not sample.strip():
        return "empty"
    arabic_chars = len(re.findall(r"[؀-ۿ]", sample))
    if arabic_chars > 50:
        return "Arabic"
    french_words = {"les", "des", "est", "pour", "dans", "une", "avec", "sur",
                    "que", "qui", "nous", "vous", "pas", "sont", "cette", "aux",
                    "par", "ont", "le", "la", "du", "ce", "se", "ne"}
    words = re.findall(r"[a-zA-ZÀ-ÿ]+", sample.lower())
    if not words:
        return "unknown"
    french_hits = sum(1 for w in words if w in french_words)
    if french_hits / len(words) > 0.05:
        return "French"
    return "English"


def clean_text(text: str) -> str:
    # sort=True introduces layout whitespace; collapse and trim it.
    # Strip per-line whitespace, collapse internal multi-space runs,
    # and squash runs of blank lines.
    lines = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r" {2,}", " ", line)
        lines.append(line)

    out = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    return "\n".join(out).strip()


def extract_pdf(pdf_path: Path) -> dict:
    doc = pymupdf.open(pdf_path)
    pages_text = []
    for page in doc:
        pages_text.append(page.get_text())
    doc.close()

    raw_text = "\n\n".join(pages_text)
    text = clean_text(raw_text)

    return {
        "file": pdf_path.name,
        "pages": len(pages_text),
        "chars": len(text),
        "words": len(text.split()),
        "language": detect_language(text),
        "text": text,
    }


def safe_filename(name: str) -> str:
    base = Path(name).stem
    # Keep Arabic, Latin, digits, dashes; replace anything else with _
    safe = re.sub(r"[^\w؀-ۿ\-]+", "_", base, flags=re.UNICODE)
    return safe.strip("_") + ".txt"


def main():
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    print(f"Found {len(pdfs)} PDFs in {PDF_DIR}\n")

    summary = []
    for pdf in pdfs:
        try:
            result = extract_pdf(pdf)
        except Exception as e:
            print(f"  ✗ {pdf.name}: FAILED ({e})")
            summary.append({"file": pdf.name, "error": str(e)})
            continue

        out_name = safe_filename(pdf.name)
        out_path = OUT_DIR / out_name
        out_path.write_text(result["text"], encoding="utf-8")

        summary.append({k: v for k, v in result.items() if k != "text"} | {"output": str(out_path.relative_to(ROOT))})
        print(f"  ✓ {pdf.name}")
        print(f"     → {out_path.name}: {result['pages']} pages, "
              f"{result['chars']:,} chars, {result['words']:,} words, lang={result['language']}")

    summary_path = OUT_DIR / "_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # Aggregate stats
    successful = [s for s in summary if "error" not in s]
    total_chars = sum(s["chars"] for s in successful)
    total_words = sum(s["words"] for s in successful)
    total_pages = sum(s["pages"] for s in successful)
    by_lang = {}
    for s in successful:
        by_lang.setdefault(s["language"], 0)
        by_lang[s["language"]] += s["chars"]

    print("\n" + "=" * 60)
    print("EXTRACTION SUMMARY")
    print("=" * 60)
    print(f"PDFs processed: {len(successful)} / {len(pdfs)}")
    print(f"Total pages:    {total_pages:,}")
    print(f"Total chars:    {total_chars:,}")
    print(f"Total words:    {total_words:,}")
    print(f"\nBy language (chars):")
    for lang, count in sorted(by_lang.items(), key=lambda x: -x[1]):
        print(f"  {lang}: {count:,} ({count/total_chars*100:.0f}%)")
    print(f"\nOutput: {OUT_DIR}/")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# End-to-end pipeline: raw data → enriched chat → searchable index.
#
# Idempotent. Re-running is safe and incremental:
#   - PDF extraction always re-runs (fast, ~5s)
#   - OCR is cached per-photo and per-scanned-PDF (only new ones get OCR'd)
#   - Merge always re-runs (fast)
#   - Index adds new chunks; full rebuild only when data is newer than index
#
# Use --rebuild to force re-OCR and a fresh index from scratch.
#
# Usage:
#   bash scripts/run_pipeline.sh
#   bash scripts/run_pipeline.sh --rebuild

set -euo pipefail

cd "$(dirname "$0")/.."
PY=".venv/bin/python"
[[ -x "$PY" ]] || { echo "✗ .venv not found — create one and pip install -r requirements.txt" >&2; exit 1; }

REBUILD_FLAG=""
if [[ "${1:-}" == "--rebuild" ]]; then
    REBUILD_FLAG="--rebuild"
fi

echo "▶ 1/7  Extract text from PDFs"
"$PY" scripts/01_extract_pdfs.py

echo
echo "▶ 2/7  OCR photo-only messages and scanned PDFs"
"$PY" scripts/02_ocr_images.py $REBUILD_FLAG

echo
echo "▶ 3/7  Merge OCR text into chat messages"
"$PY" scripts/03_merge_ocr.py

echo
echo "▶ 4/7  Scrape ensia.edu.dz (WordPress REST API)"
"$PY" scripts/04_scrape_ensia_website.py $REBUILD_FLAG

echo
echo "▶ 5/7  Scrape v2v.ensia.edu.dz (Playwright headless Chromium)"
"$PY" scripts/05_scrape_v2v_website.py $REBUILD_FLAG

echo
echo "▶ 6/7  Crawl chat-shared links (multi-page, httpx + Playwright fallback)"
"$PY" scripts/06_fetch_chat_links.py $REBUILD_FLAG

# If any source data is newer than the index, force a rebuild so the
# index reflects the updated content.
INDEX_FILE="chatbot/chroma_db/chroma.sqlite3"
INDEX_REBUILD=""
if [[ -n "$REBUILD_FLAG" ]]; then
    INDEX_REBUILD="--rebuild"
elif [[ -f "$INDEX_FILE" ]]; then
    for src in \
        "data/messages_enriched.json" \
        "data/external_text/ensia_edu_dz/_summary.json" \
        "data/external_text/v2v_ensia/_summary.json" \
        "data/external_text/chat_links/_manifest.json"; do
        if [[ -f "$src" && "$src" -nt "$INDEX_FILE" ]]; then
            echo
            echo "  ($src is newer than the index → forcing index rebuild)"
            INDEX_REBUILD="--rebuild"
            break
        fi
    done
fi

echo
echo "▶ 7/7  Build / refresh retrieval index"
"$PY" -m chatbot.index $INDEX_REBUILD

echo
echo "✓ Pipeline complete. Try a query:"
echo "    $PY -m chatbot.retrieve \"what is the CDE?\""

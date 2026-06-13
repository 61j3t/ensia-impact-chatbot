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

# Resolve the Python interpreter: prefer the local .venv when present
# (dev machines), fall back to system python in container environments
# (HF Space Docker image, which doesn't use virtualenvs). Override by
# exporting PY=/path/to/python before invoking.
if [[ -z "${PY:-}" ]]; then
    if [[ -x ".venv/bin/python" ]]; then
        PY=".venv/bin/python"
    elif command -v python >/dev/null 2>&1; then
        PY="$(command -v python)"
    elif command -v python3 >/dev/null 2>&1; then
        PY="$(command -v python3)"
    else
        echo "✗ no Python interpreter found (tried .venv, python, python3)" >&2
        exit 1
    fi
fi
echo "Using Python: $PY"

REBUILD_FLAG=""
if [[ "${1:-}" == "--rebuild" ]]; then
    REBUILD_FLAG="--rebuild"
fi

# Optional stage 0: pull new messages from Telegram via Telethon. Skipped
# if the credentials aren't set — for users who maintain their data with
# manual Telegram Desktop exports, nothing changes.
if [[ -n "${TELEGRAM_API_ID:-}" ]] || grep -qE '^TELEGRAM_API_ID=.+' .env 2>/dev/null; then
    echo "▶ 0/8  Sync new messages from Telegram"
    "$PY" scripts/00_telegram_sync.py
    echo
fi

echo "▶ 1/8  Extract text from PDFs"
"$PY" scripts/01_extract_pdfs.py

echo
echo "▶ 2/8  OCR photo-only messages and scanned PDFs"
"$PY" scripts/02_ocr_images.py $REBUILD_FLAG

echo
echo "▶ 3/8  Merge OCR text into chat messages"
"$PY" scripts/03_merge_ocr.py

echo
echo "▶ 4/8  Scrape ensia.edu.dz (WordPress REST API)"
"$PY" scripts/04_scrape_ensia_website.py $REBUILD_FLAG

echo
echo "▶ 5/8  Scrape v2v.ensia.edu.dz (Playwright headless Chromium)"
"$PY" scripts/05_scrape_v2v_website.py $REBUILD_FLAG

echo
echo "▶ 6/8  Crawl chat-shared links (multi-page, httpx + Playwright fallback)"
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
echo "▶ 7/8  Build / refresh retrieval index"
"$PY" -m chatbot.index $INDEX_REBUILD

echo
echo "▶ 8/8  Write data status snapshot for the dashboard"
"$PY" scripts/07_status_snapshot.py

echo
echo "✓ Pipeline complete. Try a query:"
echo "    $PY -m chatbot.retrieve \"what is the CDE?\""

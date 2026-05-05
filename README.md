# ENSIA Impact Chatbot

Building a chatbot that answers questions about content shared in the
ENSIA IMPACT Telegram supergroup (18 topic channels: Startups, Research,
Opportunities, Companies, Q&A, etc.).

## Project structure

```
ensia-impact-chatbot/
├── eda.ipynb                   ← Exploratory data analysis (start here)
├── scripts/
│   ├── 01_extract_pdfs.py             ← Extract text from native-text PDFs
│   ├── 02_ocr_images.py               ← OCR photo-only messages + scanned PDFs (incremental)
│   ├── 03_merge_ocr.py                ← Merge OCR back into chat messages
│   ├── 04_scrape_ensia_website.py     ← Pull every page/post from ensia.edu.dz (EN/AR/FR) via WP REST API
│   ├── 05_scrape_v2v_website.py       ← Render v2v.ensia.edu.dz with Playwright/Chromium (JS-rendered SPA)
│   └── run_pipeline.sh                ← One command: runs 01→02→03→04→05 + retrieval index build
├── docs/
│   └── architecture.md         ← Retrieval system architecture plan
├── chatbot/
│   ├── chunks.py               ← Chunk loader (chat + PDFs + OCR), language tags
│   ├── index.py                ← Builds ChromaDB index with BGE-M3 embeddings
│   ├── retrieve.py             ← Dense + reranker retriever + CLI
│   ├── memory.py               ← SQLite conversation memory (per chat+user, 24h TTL)
│   ├── answer.py               ← End-to-end: rewrite → retrieve → LLM-as-router → citation-driven sources
│   ├── telegram_bot.py         ← Telegram frontend (@ensia_impact_group_bot)
│   └── chroma_db/              ← Persistent vector store (gitignored, regenerable)
├── .env                        ← Local secrets (gitignored). Copy from .env.example.
├── .env.example                ← Template — keys you need to fill in
├── eval/
│   ├── golden_set.json         ← 35 hand-curated test queries with expected sources
│   ├── validation_set.json     ← 19 held-out queries (used once, no tuning)
│   ├── validate_golden_set.py  ← Sanity-checks a query set against the corpus
│   ├── retrieval_report.py     ← Canonical retrieval eval: golden + validation in one report
│   ├── run_eval.py             ← Per-set retrieval eval (ad-hoc)
│   ├── stress_test.py          ← Perturbation robustness probe
│   ├── k_sweep.py              ← Recall@k curve and score-distribution probe
│   └── reports/                ← Markdown eval reports per run
└── data/
    ├── result.json             ← Raw Telegram export (input)
    ├── chats/                  ← Raw photos and files (input)
    ├── extracted_text/         ← Output of 01: PDF text + _summary.json
    ├── ocr_text/               ← Output of 02: photos.json + scanned PDFs
    ├── messages_enriched.json  ← Output of 03: chat with OCR merged
    ├── external_text/          ← Output of 04: scraped pages from ensia.edu.dz (EN/AR/FR)
    └── conversations.db        ← Per-user chat memory (gitignored, regenerable)
```

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium    # for the v2v.ensia.edu.dz scraper
brew install tesseract tesseract-lang              # OCR backend, ara+fra+eng

# Configure secrets — copy template, then add your API key
cp .env.example .env
# edit .env to set GROQ_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY)
```

The chatbot uses [LiteLLM](https://github.com/BerriAI/litellm) so swapping LLM providers is a one-line change in `.env`. Default is `groq/llama-3.3-70b-versatile`.

## Pipeline

### Single command

```bash
bash scripts/run_pipeline.sh             # idempotent + incremental
bash scripts/run_pipeline.sh --rebuild   # force re-OCR + fresh index
```

The script chains all four stages and is safe to re-run anytime — perfect for picking up new data:

| Stage | When it does work | When it skips |
|---|---|---|
| 1. Extract PDFs | Always (fast, ~5s) | — |
| 2. OCR photos & scanned PDFs | New photos / new scanned PDFs only | Anything already OCR'd |
| 3. Merge OCR into chat | Always; rewrites file only if content changed | — |
| 4. Scrape ensia.edu.dz | Re-fetches only pages whose `modified` timestamp changed | Unchanged pages skipped via the manifest |
| 5. Scrape v2v.ensia.edu.dz | Always re-renders (single-page app, no `modified` signal) | — |
| 6. Build retrieval index | Adds new chunks; full rebuild if any source data is newer than the index | Re-runs are no-ops when nothing changed |

### Manual / per-stage runs

```bash
.venv/bin/python scripts/01_extract_pdfs.py
.venv/bin/python scripts/02_ocr_images.py    # add --rebuild to force re-OCR
.venv/bin/python scripts/03_merge_ocr.py
.venv/bin/python -m chatbot.index            # add --rebuild to wipe and re-embed

.venv/bin/python -m chatbot.retrieve "what advice for ENSIA students?"
PYTHONPATH=. .venv/bin/python -m chatbot.answer "what is the CDE at ENSIA?"
PYTHONPATH=. .venv/bin/python eval/run_eval.py --label phase2

# Run the Telegram bot (long-polls; keep it running in a terminal/server)
PYTHONPATH=. .venv/bin/python -m chatbot.telegram_bot
```

## Data summary

After the full pipeline:

| Source | Chars | Notes |
|---|---|---|
| Telegram messages (text + OCR merged) | ~155k | 397/410 messages have usable text |
| PDFs (native text, 10 files) | ~398k | English/French/Arabic |
| PDFs (OCR'd, 2 scanned decrees) | ~9k | Arabic legal decrees |
| ensia.edu.dz (628 pages/posts, EN+AR+FR) | ~494k | Scraped via WP REST API |
| v2v.ensia.edu.dz (landing page) | ~6k | Rendered with Playwright (JS SPA) |
| **Total** | **~1.06 M chars / ~178 k words** | |

Strongest topical areas: Companies, Startups, Opportunities, Events, Resources by Students, plus the official school pages (programs, AI Lab, faculty, news) on ensia.edu.dz.

## Evaluation

The golden set in `eval/golden_set.json` contains 35 queries spanning:

| Category | Count | Purpose |
|---|---|---|
| `real_qa` | 9 | Real student questions from the Q&A topic |
| `factual` | 14 | Fact recall from chat or PDFs |
| `multi_source` | 2 | Answer requires combining multiple sources |
| `summarization` | 2 | Summarize a chapter / section of a PDF |
| `multilingual` | 3 | Queries in Arabic / French |
| `adversarial` | 5 | Out-of-corpus — chatbot must refuse |

Each non-adversarial query specifies the exact `expected_sources` (message IDs and/or PDF files) that the retriever should surface in its top-k. This gives us **Recall@k** as a deterministic metric.

Run `.venv/bin/python eval/validate_golden_set.py [path]` after modifying any query set to confirm every reference still resolves to real content. Default path is `eval/golden_set.json`; pass `eval/validation_set.json` to validate the held-out set.

The canonical retrieval-only report runs both query sets through the production pipeline (BGE-M3 + reranker, top-10 → top-5) and writes one consolidated markdown:

```bash
PYTHONPATH=. .venv/bin/python eval/retrieval_report.py
# → eval/reports/<timestamp>_retrieval.md
```

Re-run any time the retriever config changes. Latest run: **88.6% Recall (any) @5** across 44 non-adversarial queries (97% on the golden set, 71% on the held-out validation set — that gap is the overfit signal).

## Telegram bot

Long-polling bot that wraps the answer pipeline:

```bash
PYTHONPATH=. .venv/bin/python -m chatbot.telegram_bot
```

Behavior:

- **DMs**: replies to every text message.
- **Groups**: replies only when `@`-mentioned, when the user replies to one of its messages, or when the message starts with `/ask <question>`.
- **Memory**: remembers the last 5 exchanges per `(chat, user)` for 24 hours. Follow-ups like "and how do I apply?" work — a small LLM rewrite resolves them into standalone queries before retrieval. `/reset` clears your history.
- **LLM-as-router**: one `litellm.completion(...)` call per turn handles small talk, answerable server questions, server-adjacent unanswerables ("don't have specific info, try asking about X"), and clearly off-topic redirects. The presence of `[chunk_id]` citations in the LLM's reply drives whether the sources block is shown — small talk and redirects come back without a noisy sources list.
- **Sources**: each row is a Telegram deep link. Chat citations jump into the right topic thread; PDF citations jump to the message that originally posted the PDF.
- **Footer**: single `⏱ Xs` total wall-clock line. Per-stage timings (`rewrite`, `retrieval`, `answer`) live in server logs.

See `docs/architecture.md` for the full pipeline diagram and component details.

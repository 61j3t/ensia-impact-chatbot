# Retrieval Architecture

## Constraints

- **Corpus size**: ~95k words / ~675 chunks (small)
- **Languages**: English (76%), Arabic (3%), French (1%) in chat — but PDFs flip this: 54% Arabic, 41% English, 5% French. Multilingual retrieval is mandatory.
- **Local-first**: Index built and served locally.

## Pipeline

```
        ┌────────────────────────────────────────────────────┐
        │ Telegram bot (chatbot/telegram_bot.py)             │
        │   • DMs: every message                             │
        │   • Groups: @-mention, reply, or /ask              │
        │   • /reset clears conversation memory              │
        │   • Replies cite sources as deep links into the    │
        │     chat (PDFs link to the message that posted     │
        │     them); sources block hidden when LLM didn't    │
        │     cite anything                                  │
        └────────────────────────────────────────────────────┘
                                │
                                ▼  user message
        ┌────────────────────────────────────────────────────┐
        │ 1. Load conversation memory                        │
        │    SQLite, last 5 exchanges per (chat, user),      │
        │    24h TTL                                         │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 2. Query rewriter (only if history non-empty)      │
        │    Cheap LLM call resolves "tell me more" / "and   │
        │    the deadlines?" into a self-contained query.    │
        │    Best-effort — falls back to raw query on error. │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 3. Embed query (BGE-M3, 1024-dim)                  │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 4. Dense similarity search (ChromaDB)              │
        │    Cosine, top-10 candidates                       │
        │    Optional metadata filter                        │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 5. Cross-encoder rerank (bge-reranker-v2-m3)       │
        │    Re-score 10 → keep top-k=5                      │
        │    Inputs truncated to 512 tokens per pair         │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 6. LLM as router (LiteLLM → any provider)          │
        │    Default: groq/llama-3.3-70b-versatile           │
        │    Receives system prompt + history + top-5 +      │
        │    user message. System prompt routes to one of:   │
        │      • small talk → reply, no citations            │
        │      • answerable Q → cite [chunk_id] inline       │
        │      • adjacent but no answer → suggest topics     │
        │      • off-topic → polite redirect                 │
        │    30 s timeout; on timeout, polite "try again"    │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 7. Citation-driven sources                         │
        │    If the LLM cited any [chunk_id] → surface those │
        │    5 retrieved chunks as a sources block.          │
        │    Otherwise → no sources block.                   │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
        ┌────────────────────────────────────────────────────┐
        │ 8. Save exchange to memory                         │
        │    User message + assistant reply, unless the call │
        │    failed (LLM timeout, internal error).           │
        └────────────────────────────────────────────────────┘
                                │
                                ▼
                Answer + (optional sources)  →  Telegram reply
                                                (⏱ total time)
```

## Components

### Embedding model: `BAAI/bge-m3`

- 568M parameters, ~2.3 GB on disk; produces 1024-dim embeddings
- Native multilingual support across 100+ languages (Arabic, French, English handled equally well)
- 8K context window — long enough for any PDF chunk we build
- No input prefix required (unlike e5)
- Run via `sentence-transformers`; uses local GPU acceleration when available
- Embeddings L2-normalized so cosine distance ≡ dot-product

**Alternatives considered:**
- `intfloat/multilingual-e5-small` (118M): much smaller and faster. We started with this as a baseline; BGE-M3 added +3.4 points Recall@5 and +6.7 on strict Recall@5.
- `jina-embeddings-v3` (570M): comparable quality, no built-in sparse mode.
- `multilingual-e5-large` (560M): comparable size, but BGE-M3 outperforms it on multilingual benchmarks.
- OpenAI / Voyage / Cohere APIs: better quality but adds cost and a network dep; overkill for a 675-chunk corpus.

### Reranker: `BAAI/bge-reranker-v2-m3`

- Cross-encoder that scores `(query, candidate_text)` pairs jointly
- 568M parameters, ~2.1 GB on disk
- Multilingual; handles Arabic+French+English in the same query
- Applied to the top-10 dense candidates → top-5 returned to caller
- Inputs truncated to **512 tokens** per (query, candidate) pair
  (`CrossEncoder(..., max_length=512)`). Default is 8192; capping at 512
  is the main steady-state speed-up since most reranker time goes into
  tokenizing/scoring long PDF chunks
- Reranker scores cleanly separate in-corpus matches (~0.5–0.9) from
  out-of-corpus queries (<0.2), so the score itself drives the refusal
  gate above

#### Speed tuning history

The reranker dominates query-time compute. Two cheap knobs cut its work
materially without hurting Recall@5 on the eval set:

| Lever | Value | Effect |
|---|---|---|
| `CANDIDATE_POOL` | 20 → **10** | Reranker scores half as many pairs per query |
| Reranker `max_length` | 8192 → **512** | Long PDF chunks no longer dominate tokenization |

Combined, these are roughly a 3× steady-state speed-up versus the
original config (rerank pool 20, no max-length cap). Recall@5 measured
on the golden set stayed at 96.7% after the change.

The MPS allocator on Apple Silicon occasionally puts the reranker
process into uninterruptible sleep for tens of seconds — neither knob
addresses that tail. A retrieval timeout / dense-only fallback would,
but isn't currently implemented.

### Vector store: ChromaDB (persistent)

- Local-first, no server, on-disk under `chatbot/chroma_db/`
- Single collection `ensia` with cosine distance (`hnsw:space: cosine`)
- 675-chunk index is <100 MB
- Supports metadata filters at query time (`topic`, `source_type`, `language`)

### LLM as router (LiteLLM, default `groq/llama-3.3-70b-versatile`)

The LLM is the routing layer — every non-error path goes through one
`litellm.completion(...)` call. The system prompt instructs it to pick
one of four behaviors based on the user message:

1. **Small talk** (greetings, "thanks", introducing themselves, asking
   what the bot does): reply warmly and briefly. **No citations.**
2. **Answerable server question**: answer using only the supplied
   context. Cite chunks inline as `[chunk_id]`.
3. **Server-adjacent but unanswerable**: acknowledge missing info and
   suggest related topics from the server. No fake citations.
4. **Clearly off-topic** (write code, weather, world events, math
   homework, info about real people): polite redirect to server topics.
   The prompt is explicit about being **tolerant** — borderline goes to
   case 3, not 4.

#### Why LLM-as-router instead of an upfront classifier

Replaces an earlier deterministic refusal gate that hard-refused below
score 0.20. That gate fired on greetings (e.g. "Hi there" scored above
threshold and pulled five awkward "welcome"-ish chunks; "My name is X"
scored below threshold and got a cold canned refusal). Letting the LLM
decide handles all four cases naturally with one call and a single
prompt.

#### Citation-driven sources

The bot doesn't infer when to show a sources block from the score; it
infers from whether the LLM used `[chunk_id]` in the response. A regex
checks for any citation; if absent, the sources block is hidden. Small
talk replies and polite redirects therefore never carry a noisy sources
list.

#### Provider configuration

- Provider/model selected via `CHATBOT_LLM_MODEL` in `.env`. Examples:
  `groq/llama-3.3-70b-versatile`, `groq/llama-3.1-8b-instant`,
  `anthropic/claude-sonnet-4-5`, `openai/gpt-5`, `gemini/gemini-2.5-pro`.
- API keys in `.env` (gitignored): `GROQ_API_KEY`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `GEMINI_API_KEY`.
- 30 s `timeout` on the answer call. On `litellm.exceptions.Timeout` the
  bot returns a polite "trouble reaching the model" message instead of
  hanging on a stalled upstream (we hit a 76 s Groq spike during testing).

#### Tier (informational only)

`tier` ∈ {`high`, `mid`, `low`} reports the retrieval-confidence band
based on top-1 reranker score (≥0.50 / 0.20–0.50 / <0.20). It's logged
but no longer changes routing — the LLM is the only decision-maker.

### Conversation memory (`chatbot/memory.py`)

SQLite-backed rolling window of recent turns, scoped per (chat_id,
user_id) so different users in the same group don't share context.

- File: `data/conversations.db` (gitignored, regenerable, contains
  user-supplied content).
- Schema: `(chat_id, user_id, turn_idx, role, content, ts)` with a
  composite primary key and a `(chat_id, user_id, turn_idx DESC)` index.
- Read API: `recent_turns(chat_id, user_id, n=5, max_age_hours=24)`
  returns up to 5 exchanges (≤10 rows) ordered oldest→newest.
- Write API: `add_turns(chat_id, user_id, turns)` appends atomically
  with a serialized `turn_idx`.
- `/reset` command (`reset(chat_id, user_id)`) clears all rows for the
  caller and returns the deleted row count.
- TTL is enforced at read time via timestamp filter; old rows aren't
  proactively deleted (cheap, lets you bump the TTL later without
  losing history).
- Refused turns (LLM timeout, internal error) are **not** saved — they
  carry no useful context. Successful small-talk replies **are** saved
  so follow-ups can refer to them.

### Query rewriter

Helper in `chatbot/answer.py`. Triggered when memory has any prior
turns. Asks the same model (default Groq Llama 3.3 70B) to convert a
follow-up like "and how do I apply?" into a standalone query like
"how do I apply for decree 1275?" before retrieval embeds it.

- Best-effort: any failure (timeout, parse error, exception) falls back
  to the raw query.
- Tight budgets: 8 s timeout, 200 max tokens, last 4 history rows in
  the prompt to keep cost low.
- The original user message is what's saved to history and what the
  answer LLM sees in its messages list — only the retrieval step uses
  the rewritten form.

### Telegram frontend (`chatbot/telegram_bot.py`)

The user-facing surface. Long-polls Telegram, forwards text messages
through `answer()`, replies with the answer + (optional) sources block
+ total wall time.

- Bot identity: `@ensia_impact_group_bot`, token in `.env`
  (`TELEGRAM_BOT_TOKEN`).
- Models load **once at startup** (BGE-M3 + reranker, ~30–60 s) and the
  Retriever is reused across all messages.
- Group-chat scoping: replies only when `@`-mentioned, when the user
  replies to a bot message, or when the message starts with `/ask`.
  In direct messages every text message is treated as a query.
- Commands: `/start`, `/help`, `/ask <q>`, **`/reset`** (clears memory
  for the (chat, user) pair).
- Memory is **per-user even in groups** — each student gets isolated
  context. Loaded before each query, saved after a successful reply.
- Source rows are **clickable Telegram deep links**:
  - Chat sources → `https://t.me/c/<chat_id>/<topic_id>/<msg_id>` jumps
    to the message in its topic thread.
  - PDF sources → link to the chat message that originally posted the
    PDF (`message["file"]` field in the export). The link target is
    looked up via a `pdf_filename → message_id` map built at startup.
- Sources block is **only rendered when the LLM cited at least one
  `[chunk_id]`** in its answer — see "Citation-driven sources" above.
- Reply footer: a single `⏱ Xs` total wall-time line. Per-stage timings
  (rewrite / retrieval / answer) are kept in server logs only.
- Lookup maps cached at startup:
  - `topic_id_by_msg`: walks reply chains so chat sources can deep-link
    into the right topic thread (407/410 content messages resolvable).
  - `pdf_path_by_metakey`: maps a chunk's `pdf_file` metadata back to
    the original `.pdf` on disk (NFC-normalized to handle macOS NFD).
  - `msg_id_by_pdf_name`: maps each shared PDF to the Telegram message
    that posted it.

### Chunking

- **Chat messages**: one message = one chunk. Messages with <20 chars are dropped.
- **PDFs**: paragraph-aware recursive split, target ~2000 chars per chunk with ~250-char overlap between consecutive chunks.
- **OCR'd photo content**: already merged into the chat messages by `scripts/03_merge_ocr.py`, so it flows through the same chat-message chunking path.

Each chunk carries this metadata in ChromaDB (None values are stored as empty string for ChromaDB compatibility):

```python
{
  "source_type": "chat" | "pdf",
  "topic":       str | "",       # chat topic name (None for PDFs)
  "language":    "en" | "ar" | "fr" | "empty",
  "message_id":  int | "",       # for chat
  "pdf_file":    str | "",       # for PDFs
  "chunk_index": int | "",       # which chunk of the PDF
  "date":        ISO str | "",
  "sender":      str | "",
  "text_source": "original" | "ocr" | "",
}
```

## File layout

```
chatbot/
├── chunks.py              ← Builds the chunk list from messages_enriched.json + extracted_text/ + ocr_text/
├── index.py               ← Embeds chunks with BGE-M3, writes to ChromaDB. Idempotent.
├── retrieve.py            ← Retriever class: dense top-10 → rerank (max_length=512) → top-k. CLI for smoke-tests.
├── memory.py              ← SQLite conversation memory: per (chat, user), 5-turn window, 24h TTL, /reset.
├── answer.py              ← End-to-end: rewrite (if history) → retrieve → LLM-as-router → citation-driven sources + per-stage timings.
├── telegram_bot.py        ← Telegram frontend: history load/save, /reset, deep-link sources, single ⏱ footer.
└── chroma_db/             ← Persistent ChromaDB store
data/
├── messages_enriched.json ← Chat with OCR merged (input to chunks.py)
├── extracted_text/        ← Per-PDF text (input to chunks.py)
├── ocr_text/              ← OCR'd photo + scanned-PDF text (input to chunks.py)
└── conversations.db       ← SQLite store for conversation memory (gitignored)
eval/
├── golden_set.json         ← 35 hand-curated queries with expected sources (used during retriever tuning)
├── validation_set.json     ← 19 held-out queries used once for unbiased generalization estimate
├── validate_golden_set.py  ← Sanity-check that every expected source actually exists in the corpus
├── retrieval_report.py     ← Canonical retrieval-only eval: golden + validation, recall, scores, misses
├── run_eval.py             ← Per-set retrieval eval, useful for ad-hoc runs (--set <path>)
├── stress_test.py          ← Robustness probe: 4 query perturbations × every query
├── k_sweep.py              ← Sweeps k to surface recall@k curve and score distribution
└── reports/                ← Markdown reports per run
.env                        ← Local secrets (gitignored): GROQ_API_KEY, TELEGRAM_BOT_TOKEN, CHATBOT_LLM_MODEL
.env.example                ← Template — copy to .env and fill in
```

## Sources

- [Best Embedding Model for RAG 2026: 10 Models Compared - Milvus Blog](https://milvus.io/blog/choose-embedding-model-rag-2026.md)
- [BAAI/bge-m3 · Hugging Face](https://huggingface.co/BAAI/bge-m3)
- [BGE-M3 vs Jina Embeddings v3 | VIPS Learn](https://learn.engineering.vips.edu/compare/bge-m3-vs-jina-embeddings-v3)
- [MTEB Leaderboard](https://huggingface.co/spaces/mteb/leaderboard)
- [The Best Open-Source Embedding Models in 2026 - BentoML](https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models)

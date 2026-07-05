"""End-to-end answer generation: rewrite → retrieve → LLM → answer.

The LLM is the router. Every non-error path goes through one
litellm.completion() call. The system prompt instructs it to pick one
of: small talk, cited answer, server-adjacent redirect, or off-topic
redirect. Whether to surface a sources block is decided by whether the
LLM cited any [chunk_id] in its response.

If conversation history is supplied, a small rewriter call resolves
follow-ups into standalone retrieval queries before embedding.

Uses LiteLLM so the same code works with any provider — change
CHATBOT_LLM_MODEL in .env to swap. Defaults to Groq llama-3.3-70b.

API:
    from chatbot.answer import answer
    result = answer("What is the CDE at ENSIA?")
    print(result["answer"])
    for s in result["sources"]:
        print(f"  - {s['id']}: {s['preview']}")

Result shape:
    {
      "query":          str,
      "answer":         str,
      "refused":        bool,
      "refusal_reason": str | None,
      "model":          str,           # LiteLLM model string used
      "top_score":      float,         # reranker score of #1 hit
      "tier":           "hard_refuse" | "soft" | "normal",
      "sources":        [{id, preview, metadata, score}, ...],
    }

CLI:
    .venv/bin/python -m chatbot.answer "what is the CDE?"
    .venv/bin/python -m chatbot.answer "Salam, decree 1275?" --k 3
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm
from dotenv import load_dotenv

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("CHATBOT_LLM_MODEL", "gemini/gemini-2.5-flash")
# Fallback chain tried in order after the primary fails. We interleave
# providers (Gemini → Groq → Gemini → Groq) so a single-provider outage
# doesn't take out two attempts in a row. Override via
# CHATBOT_FALLBACK_MODELS=a,b,c.
FALLBACK_MODELS: list[str] = [
    m.strip()
    for m in os.getenv(
        "CHATBOT_FALLBACK_MODELS",
        "groq/llama-3.3-70b-versatile,gemini/gemini-2.0-flash,groq/llama-3.1-8b-instant",
    ).split(",")
    if m.strip()
]

# Per-model rate-limit cooldown: when a model returns 429 we record
# `model → unix_ts_until_which_we_skip`. Subsequent requests skip the
# model entirely (and don't waste retries trying it) until the timestamp
# passes. 60s matches Gemini free tier's RPM reset window; Groq's 429
# usually clears within 10-15s but the longer cooldown is fine — it just
# means we use the other provider for the duration.
_model_cooldown_until: dict[str, float] = {}
RATE_LIMIT_COOLDOWN_S = float(os.getenv("CHATBOT_RATE_LIMIT_COOLDOWN_S", "60"))
HARD_REFUSAL_THRESHOLD = 0.20
SOFT_REFUSAL_THRESHOLD = 0.50

# Transient errors we retry on. Anything else propagates immediately so
# we don't hide real bugs behind retries.
_TRANSIENT_LLM_ERRORS: tuple[type[Exception], ...] = (
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError,
)


def _user_message_for_llm_error(exc: Exception) -> str:
    """User-friendly text for a transient LLM error that exhausted retries."""
    if isinstance(exc, litellm.exceptions.ServiceUnavailableError):
        return (
            "The model is over capacity right now — please try again "
            "in a few seconds."
        )
    if isinstance(exc, litellm.exceptions.RateLimitError):
        return (
            "I've hit the model's rate limit. Try again in a moment "
            "(or use a shorter question)."
        )
    if isinstance(exc, litellm.exceptions.Timeout):
        return (
            "The model took too long to respond. Try again in a moment."
        )
    if isinstance(exc, litellm.exceptions.APIConnectionError):
        return (
            "Couldn't reach the model service. Try again in a moment."
        )
    return (
        "The model is temporarily unavailable. Try again in a moment."
    )


def _is_cooled_down(model: str) -> bool:
    return _model_cooldown_until.get(model, 0.0) > time.monotonic()


def _set_cooldown(model: str, seconds: float = RATE_LIMIT_COOLDOWN_S) -> None:
    _model_cooldown_until[model] = time.monotonic() + seconds


def _llm_attempt_specs(
    primary: str,
    fallbacks: list[str],
) -> list[tuple[str, float]]:
    """3 + N attempts: primary 3× with backoff, then each fallback once.

    The primary retries cover transient overloads (e.g. Groq/Gemini
    `ServiceUnavailableError` or `Timeout`) that often clear within a
    couple seconds. `RateLimitError` is handled differently in the
    runners: instead of retrying the same model (which would burn more
    of its rate budget), we record it as cooled-down and advance to
    the next model immediately. So under sustained load the chain
    naturally shifts to whichever provider still has headroom."""
    specs: list[tuple[str, float]] = [
        (primary, 0.0),
        (primary, 0.5),
        (primary, 1.5),
    ]
    for fb in fallbacks:
        specs.append((fb, 0.5))
    return specs


def _complete_llm_with_retry(
    primary_model: str,
    messages: list[dict],
    *,
    temperature: float,
    timeout: float,
) -> tuple[str, str]:
    """Non-streaming LLM call with retry + fallback.

    Iterates the attempt chain. Two flavors of skip:
      • A model with an active cooldown (from a recent RateLimitError)
        is skipped — but only if some other chain member is fresh.
      • A model that gets rate-limited DURING this request is added
        to a per-request skip-set so we don't retry it on a later
        chain entry with the same name.

    Returns `(answer_text, model_used)`. Propagates the last transient
    exception if every attempt fails; the caller turns that into a
    user-friendly refusal."""
    specs = _llm_attempt_specs(primary_model, FALLBACK_MODELS)
    fresh_exists = any(not _is_cooled_down(m) for m, _ in specs)
    rate_limited_now: set[str] = set()
    last_exc: Exception | None = None
    for m, delay in specs:
        if m in rate_limited_now:
            continue
        if fresh_exists and _is_cooled_down(m):
            logger.info("Skip %s (cooled down from prior 429)", m)
            continue
        if delay:
            time.sleep(delay)
        try:
            r = litellm.completion(
                model=m, messages=messages,
                temperature=temperature, timeout=timeout,
            )
            return r.choices[0].message.content.strip(), m
        except litellm.exceptions.RateLimitError as e:
            last_exc = e
            _set_cooldown(m)
            rate_limited_now.add(m)
            logger.warning(
                "RateLimit on %s — cooled down for %.0fs",
                m, RATE_LIMIT_COOLDOWN_S,
            )
        except _TRANSIENT_LLM_ERRORS as e:
            last_exc = e
            logger.warning(
                "LLM completion failed on %s (%s): %s",
                m, type(e).__name__, str(e)[:120],
            )
    assert last_exc is not None  # unreachable: attempts list is non-empty
    raise last_exc


def _stream_llm_with_retry(
    primary_model: str,
    messages: list[dict],
    stream_callback: Any,
    *,
    temperature: float,
    timeout: float,
) -> tuple[str, str]:
    """Streaming LLM call with retry + fallback.

    Only retries when NO chunks have been delivered to the user yet —
    once we've started streaming text, retrying would replace partial
    output with the new attempt's output (confusing) so we propagate
    and let the caller surface a refusal.

    Returns `(final_text, model_used)`."""
    specs = _llm_attempt_specs(primary_model, FALLBACK_MODELS)
    fresh_exists = any(not _is_cooled_down(m) for m, _ in specs)
    rate_limited_now: set[str] = set()
    last_exc: Exception | None = None
    for m, delay in specs:
        if m in rate_limited_now:
            continue
        if fresh_exists and _is_cooled_down(m):
            logger.info("Skip %s (cooled down from prior 429)", m)
            continue
        if delay:
            time.sleep(delay)
        chunks: list[str] = []
        try:
            resp = litellm.completion(
                model=m, messages=messages,
                temperature=temperature, timeout=timeout,
                stream=True,
            )
            for piece in resp:
                delta = ""
                try:
                    delta = piece.choices[0].delta.content or ""
                except Exception:
                    pass
                if not delta:
                    continue
                chunks.append(delta)
                try:
                    stream_callback(delta, "".join(chunks))
                except Exception:
                    # UI plumbing; never let it kill the read loop.
                    pass
            return "".join(chunks).strip(), m
        except litellm.exceptions.RateLimitError as e:
            last_exc = e
            _set_cooldown(m)
            rate_limited_now.add(m)
            logger.warning(
                "Stream RateLimit on %s — cooled down for %.0fs",
                m, RATE_LIMIT_COOLDOWN_S,
            )
            if chunks:
                # Already streamed — caller will surface refusal.
                raise
        except _TRANSIENT_LLM_ERRORS as e:
            last_exc = e
            logger.warning(
                "LLM stream failed on %s (%s): %s",
                m, type(e).__name__, str(e)[:120],
            )
            if chunks:
                raise
    assert last_exc is not None
    raise last_exc


SYSTEM_PROMPT_TEMPLATE = """\
You are the assistant for the ENSIA Impact community — a Telegram group for \
ENSIA students, faculty, and partners covering startups, research, internships, \
opportunities, patents, events, and more.

**Today's date is {today}.** Treat this as ground truth when reasoning about \
deadlines or recency.

Each user message arrives with CONTEXT — chunks the search system pulled from \
either the Telegram chat history, the PDFs shared in it, or pages on the \
ensia.edu.dz website. Every chunk header includes a date (the chat message's \
posting date, the page's last-modified date, etc.). The CONTEXT may or may not \
be relevant to the message; you decide. Chunk IDs look like `chat_832`, \
`pdf_ENSIA_3`, or `ext_ensia_edu_dz_<slug>_0`.

**Time-sensitive content** — when a chunk uses relative phrasing like \
"deadline in 30 days", "submissions close next month", "tomorrow at 2 PM", \
those phrases were written on the chunk's date, NOT today. Compute the \
absolute date silently in your head, then compare to today.

Rules for how you talk about dates in your reply:

  - **Be decisive.** If the absolute date is before today, say "the \
deadline has passed" or "the call has closed". Do NOT hedge with "likely", \
"probably", "may have".
  - **Do NOT show your arithmetic.** Phrases like "the submission deadline \
mentioned is 58 days from November 17, 2025" leak your reasoning and read \
like a robot. The user doesn't care how you computed it.
  - **Do NOT refer to "the context", "the chunk", "what I have", or \
similar internal labels** — speak naturally about the topic itself.
  - **Closed deadlines → next step.** When a call is closed, point the user \
to the source URL / site for the current edition. The URL is in **addition** \
to the [chunk_id] citation — never instead of it. e.g.: "the 2025 call has \
closed [chat_832] — check algerianpresidentaward.dz for the latest one".
  - **Recent calls (under ~3 months old per chunk date) → can be quoted \
normally**, but if you mention the deadline include the chunk's date so the \
user can verify ("the call posted on 2026-04-12 closes on May 30 [chat_999]").
  - **Old calls (over ~3 months) without a clear absolute date** → describe \
the program without quoting deadlines, cite the chunk, and point to the \
source ("ANPT runs an incubation program for early-stage startups \
[chat_614] — see anpt.dz for the current intake").

Pick the right behavior:

1. SMALL TALK (greeting, "thanks", introducing themselves, asking who you are \
or what you do, casual chat):
   Reply warmly and briefly. On a first-time greeting, mention you can help \
find information from the ENSIA Impact server. **Do NOT cite anything for \
small talk** — ignore the CONTEXT.

2. SERVER QUESTION the CONTEXT actually answers:
   Answer using only the CONTEXT. **Cite chunks inline as [chunk_id]** right \
after the claim they support, e.g. "[chat_832]", "[pdf_ENSIA_3]", \
"[ext_ensia_edu_dz_11406_incubator_0]". **Each citation gets its own pair \
of brackets — write `[chat_832] [pdf_ENSIA_3]`, NEVER \
`[chat_832, pdf_ENSIA_3]`.** A URL or domain name mentioned in the reply \
(e.g. for the user to visit) does NOT replace the citation — every factual \
claim that came from the CONTEXT still needs its [chunk_id].

3. SERVER QUESTION the CONTEXT does NOT cover:
   Say you don't have specific info on that yet, and suggest related server \
topics they could ask about (CDE, the incubator, internships, decree 1275, \
scholarships, events, etc.). **Do NOT use brackets at all** — not as \
evidence, not as a list of "things I checked", not even like "[1] or [2]". \
A refusal must contain zero `[chunk_id]` markers.

4. CLEARLY OFF-TOPIC (write code, weather, world events, math homework, \
personal/private info about real people):
   Politely say your scope is the ENSIA Impact server's content and invite \
them to ask about server topics. **Be tolerant** — if a question is even \
plausibly server-related, treat it as case 3 instead of this one. **No \
brackets in this case either.**

LANGUAGE: reply in the language of the CURRENT user message (the latest one), \
even if earlier messages in the conversation used a different language. Match \
the user's switch immediately — English / French / Arabic.
TONE: warm but concise. Two or three sentences is usually enough. EXCEPTION — \
if the user asks to list something or asks for "all"/"which"/"how many" of a \
set (e.g. all incubated startups) and the CONTEXT contains the full list, \
give the COMPLETE list — enumerate every item. Do not abbreviate with "among \
others" or defer to a website when the full list is right there in the CONTEXT.
NEVER make up [chunk_id] citations. Only cite IDs that appear in the \
CONTEXT, and only in case 2 above.
"""


def _system_prompt() -> str:
    """Compose the system prompt with today's date injected."""
    today = datetime.now(timezone.utc).date().isoformat()
    return SYSTEM_PROMPT_TEMPLATE.format(today=today)

# Detects [chunk_id] citations in the LLM's answer. Used to decide whether
# to show the "📚 Sources" block — if the LLM didn't cite anything, the
# answer was either small talk or a redirect, and a sources block would be
# noisy/misleading.
# Allows dots and dashes in the body of the ID so we match chat_links
# chunks whose host directory keeps the original dot (e.g.
# "ext_chat_links_erasmusplus.dz__overview-and-objectives_0").
#
# Also matches comma-separated groups inside a single bracket, e.g.
# `[chat_42, pdf_X_0]` — some models (Gemini in particular) like to
# combine citations that way. The renumber pass splits these out into
# `[1] [2]` so the rendered text doesn't leak raw chunk IDs.
_CHUNK_ID_RE = re.compile(r"(?:chat|pdf|ext)_[\w.\-]+")
_CITATION_RE = re.compile(
    r"\[\s*"
    rf"({_CHUNK_ID_RE.pattern}(?:\s*,\s*{_CHUNK_ID_RE.pattern})*)"
    r"\s*\]"
)

# Phrases that strongly indicate the LLM is refusing rather than answering,
# even when it cited chunks. If any of these appear in the answer, we strip
# the citations and zero out the sources block — they'd be misleading
# next to "I don't have info on that". The system prompt forbids brackets
# in refusals; this regex is a backstop for when the LLM ignores it.
_REFUSAL_PATTERNS = re.compile(
    r"(?i)\b("
    r"(?:do(?:n'?t| not)) have (?:any |specific )?(?:info(?:rmation)?|"
    r"data|details|specifics|context)|"
    r"no (?:specific |particular )?(?:info(?:rmation)? )?(?:about|on|"
    r"regarding) [a-z]|"
    r"(?:cou|ca)(?:l)?d(?:n'?t| not) find|"
    r"I (?:do(?:n'?t| not)) (?:see|have|know)|"
    r"not (?:in|present in|found in|provided in) the context|"
    r"outside (?:my |the bot's )?scope"
    r")\b"
)


def _scrub_citations(text: str) -> str:
    """Strip `[chunk_id]` markers and the immediate punctuation/conjunctions
    that were tying them together (e.g. `[1], [2], or [3]`). Used after a
    refusal-pattern match — the citations don't actually support the
    (non-)answer, so we'd rather render clean prose than a noisy list."""
    text = _CITATION_RE.sub("", text)
    # Iteratively collapse leftover punctuation and conjunctions until a
    # pass changes nothing.
    for _ in range(4):
        before = text
        text = re.sub(r",\s*(?=,|\s*(?:or|and)\b\s*[.,!?])", "",
                      text, flags=re.IGNORECASE)
        text = re.sub(r"\s+(?:or|and)\s+(?=[.,!?])", "",
                      text, flags=re.IGNORECASE)
        text = re.sub(r"\s*,\s*(?=[.!?])", "", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        if text == before:
            break
    return text.strip()


def _renumber_citations(
    answer_text: str, sources: list[dict]
) -> tuple[str, list[dict]]:
    """Replace `[chunk_id]` citations with `[1]`, `[2]`, … in order of first
    appearance. Returns (rewritten_text, ordered_cited_sources) where each
    source has a `number` field pointing to its bracket label.

    Citations that don't match any retrieved source are left untouched (the
    LLM was told not to do that, but we don't want to silently strip them).

    Refusal short-circuit: if the answer matches `_REFUSAL_PATTERNS`, we
    scrub all citations and return an empty sources list, even if the LLM
    listed brackets like "[1], [2], or [3]" to enumerate what it checked.
    """
    if _REFUSAL_PATTERNS.search(answer_text):
        return _scrub_citations(answer_text), []

    sources_by_id = {s["id"]: s for s in sources}
    cite_to_num: dict[str, int] = {}

    # First pass — walk every citation group (single or comma-split),
    # assign sequential numbers to each known chunk in first-seen order.
    for m in _CITATION_RE.finditer(answer_text):
        for cid in [p.strip() for p in m.group(1).split(",")]:
            if cid in sources_by_id and cid not in cite_to_num:
                cite_to_num[cid] = len(cite_to_num) + 1

    def _sub(m):
        ids = [p.strip() for p in m.group(1).split(",")]
        # Render the group as adjacent numbered brackets ([1] [2]). IDs
        # that didn't match any source pass through unchanged inside
        # their own bracket — the LLM was told not to invent IDs, but
        # we don't want to silently delete unrecognised ones either.
        out = []
        for cid in ids:
            if cid in cite_to_num:
                out.append(f"[{cite_to_num[cid]}]")
            else:
                out.append(f"[{cid}]")
        return " ".join(out)

    rewritten = _CITATION_RE.sub(_sub, answer_text)

    ordered: list[dict] = []
    for cid, num in sorted(cite_to_num.items(), key=lambda kv: kv[1]):
        entry = dict(sources_by_id[cid])
        entry["number"] = num
        ordered.append(entry)
    return rewritten, ordered

REWRITER_SYSTEM_PROMPT = """\
You rewrite follow-up questions into self-contained queries for a search system.

Given a short conversation and a new user message, output ONE standalone \
question that captures the user's intent without needing the prior context. \
If the message is already self-contained, output it unchanged. Do NOT answer \
the question — only rewrite.

Output only the rewritten query. No quotes, no labels, no explanation.\
"""

REWRITER_TIMEOUT_S = 8
REWRITER_MAX_TOKENS = 200
LLM_TIMEOUT_S = 30


# ─── helpers ────────────────────────────────────────────────────────────────

def _chunk_label(metadata: dict) -> str:
    """Compact human-readable label for a retrieved chunk. Always includes
    a date when one is available — the LLM uses it to reason about whether
    deadlines / events mentioned inside the chunk are still relevant."""
    src = metadata.get("source_type")
    if src == "chat":
        topic = metadata.get("topic") or "no topic"
        sender = metadata.get("sender") or "unknown"
        date = (metadata.get("date") or "")[:10] or "?"
        return f"chat msg | topic: {topic} | sender: {sender} | posted: {date}"
    if src == "pdf":
        return (f"PDF: {metadata.get('pdf_file', '?')} | "
                f"chunk #{metadata.get('chunk_index', '?')}")
    if src == "external":
        # `date` was populated from the page's Modified / Fetched header.
        date = (metadata.get("date") or "")[:10] or "?"
        site = metadata.get("site") or "?"
        title = metadata.get("title") or metadata.get("url") or "?"
        return f"web page | site: {site} | title: {title} | last modified: {date}"
    return "unknown source"


def _measure_context(model: str, messages: list[dict]) -> tuple[int, int | None]:
    """Return (tokens_used, max_input_tokens) for this LLM call.

    `tokens_used` is best-effort: LiteLLM's token_counter handles each
    supported provider via its native tokenizer; if it errors out (rare
    but happens for unfamiliar models) we approximate at ~4 chars per
    token, which is close enough for a progress badge.

    `max_input_tokens` comes from LiteLLM's `model_cost` dict. If we
    can't look it up the second element is None and the bot just won't
    show a percentage."""
    used = 0
    try:
        used = litellm.token_counter(model=model, messages=messages)
    except Exception:
        used = sum(len(m.get("content", "")) for m in messages) // 4

    max_input: int | None = None
    try:
        info = litellm.get_model_info(model)
        max_input = (
            info.get("max_input_tokens")
            or info.get("max_tokens")
            or None
        )
    except Exception:
        max_input = None
    return used, max_input


def _build_context(hits: list[dict], max_chars_per_chunk: int = 1500) -> str:
    """Render retrieved chunks into a context block the LLM can read.

    Each chunk is prefixed with its ID and a short metadata header.
    Long PDF chunks are truncated to max_chars_per_chunk to control prompt size.
    """
    parts = []
    for h in hits:
        text = h["text"]
        if len(text) > max_chars_per_chunk:
            text = text[:max_chars_per_chunk].rstrip() + " […truncated]"
        header = f"[{h['id']}] ({_chunk_label(h['metadata'])})"
        parts.append(f"{header}\n{text}")
    return "\n\n---\n\n".join(parts)


def _quick_complete(
    messages: list[dict],
    primary: str,
    *,
    timeout: float,
    max_tokens: int,
    temperature: float = 0.0,
) -> str | None:
    """Small auxiliary LLM call (query rewrite, translation) that survives
    a rate-limited primary. Tries primary then fallbacks, once each,
    skipping models currently on cooldown when a fresh one exists.
    Fail-fast — no per-model retries (these calls are cheap and
    latency-sensitive). Returns the stripped text, or None if every model
    fails. Without this, a 429 on the primary (frequent on Gemini's free
    tier) silently disables the rewrite/translation entirely."""
    chain = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    fresh_exists = any(not _is_cooled_down(m) for m in chain)
    for m in chain:
        if fresh_exists and _is_cooled_down(m):
            continue
        try:
            r = litellm.completion(
                model=m, messages=messages,
                temperature=temperature, timeout=timeout, max_tokens=max_tokens,
            )
            out = (r.choices[0].message.content or "").strip()
            if out:
                return out
        except litellm.exceptions.RateLimitError:
            _set_cooldown(m)
        except _TRANSIENT_LLM_ERRORS:
            continue
        except Exception:
            continue
    return None


def _strip_wrapping_quotes(text: str) -> str:
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1].strip()
    return text


def _rewrite_for_retrieval(
    query: str,
    history: list[dict],
    model: str,
) -> str:
    """Resolve pronouns / elided context in a follow-up question so the
    retriever can score it on its own. Best-effort: if the rewriter call
    fails or times out, fall back to the raw query."""
    if not history:
        return query

    # Last 4 rows = last 2 exchanges. Keeps the prompt small.
    recent = history[-4:]
    history_text = "\n".join(
        f"{t['role'].upper()}: {t['content']}" for t in recent
    )
    user_msg = (
        f"Conversation:\n{history_text}\n\n"
        f"New message: {query}\n\nStandalone query:"
    )
    rewritten = _quick_complete(
        [
            {"role": "system", "content": REWRITER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        model, timeout=REWRITER_TIMEOUT_S, max_tokens=REWRITER_MAX_TOKENS,
    )
    if not rewritten:
        return query
    return _strip_wrapping_quotes(rewritten) or query


# Arabic block + common French diacritics/markers. Cheap signal for "this
# query isn't English" — we don't need precise language ID, just whether
# to spend a translation call. A query with any Arabic codepoint, or
# French-specific characters, is treated as non-English.
_ARABIC_RE = re.compile(r"[؀-ۿ]")
_FRENCH_RE = re.compile(r"[àâäéèêëîïôöùûüçœ]", re.IGNORECASE)

TRANSLATOR_SYSTEM_PROMPT = """\
You translate a user's search query into English for a retrieval system.

Output ONLY the English translation of the query — no quotes, no labels, no \
explanation. Keep proper nouns (ENSIA, V2V, Sonatrach, décret 1275, etc.) \
as-is. If the query is already English, output it unchanged.\
"""
TRANSLATOR_TIMEOUT_S = 8
TRANSLATOR_MAX_TOKENS = 120


def _looks_non_english(text: str) -> bool:
    """True if the text contains Arabic codepoints or French-specific
    characters — i.e. it's worth a translation pass to also retrieve
    against the (majority-English) corpus."""
    return bool(_ARABIC_RE.search(text) or _FRENCH_RE.search(text))


def _response_lang_directive(query: str) -> str:
    """Explicit response-language instruction for the CURRENT query, so an
    earlier-turn language (e.g. a prior Arabic question still in history)
    doesn't bleed into the reply. Script-based and conservative: Arabic
    codepoints → Arabic; French diacritics → French; otherwise English.
    Accentless French is the one miss — rare, and the strengthened system
    prompt still nudges the model to match."""
    if _ARABIC_RE.search(query):
        return "Respond in Arabic."
    if _FRENCH_RE.search(query):
        return "Respond in French."
    return "Respond in English."


def _translate_to_english(query: str, model: str) -> str | None:
    """Best-effort English translation of a non-English query, used as a
    second retrieval ranking so the English half of the corpus gets a
    fair vote (BGE-M3 otherwise ranks same-language chunks far higher).

    Returns None if translation fails, is empty, or comes back unchanged —
    the caller then just retrieves with the original query."""
    out = _quick_complete(
        [
            {"role": "system", "content": TRANSLATOR_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        model, timeout=TRANSLATOR_TIMEOUT_S, max_tokens=TRANSLATOR_MAX_TOKENS,
    )
    if not out:
        return None
    out = _strip_wrapping_quotes(out)
    if not out or out.strip().lower() == query.strip().lower():
        return None
    return out


def _format_sources(hits: list[dict], max_preview_chars: int = 160) -> list[dict]:
    out = []
    for h in hits:
        preview = h["text"][:max_preview_chars].replace("\n", " ")
        if len(h["text"]) > max_preview_chars:
            preview += "…"
        out.append({
            "id": h["id"],
            "score": round(h["score"], 4),
            "metadata": h["metadata"],
            "preview": preview,
        })
    return out


# ─── main entry point ──────────────────────────────────────────────────────

def answer(
    query: str,
    *,
    k: int = 5,
    model: str | None = None,
    retriever: Retriever | None = None,
    history: list[dict] | None = None,
    rerank: bool = False,
    stream_callback: Any = None,
) -> dict[str, Any]:
    """Generate an answer for `query` using retrieve → (optional) rerank → LLM.

    The LLM is the router: every non-error path goes through it. The
    SYSTEM_PROMPT instructs it to handle four cases — small talk,
    answerable server questions, server-adjacent unanswerables, and
    out-of-scope. Whether to show a sources block downstream is decided
    purely by whether the LLM cited any [chunk_id] in its response (we
    detect this and zero out the sources list when it didn't).

    `history`, if provided, is a list of prior `{role, content}` turns
    (oldest → newest). When non-empty, a small rewriter call resolves
    follow-ups into standalone queries before retrieval, and the full
    history is forwarded to the answer LLM.

    Returns a dict with timings: {rewrite, retrieval, answer}, all in
    seconds. The `tier` field reports the retrieval confidence band
    (high/mid/low) but has no effect on routing — it's informational.

    `rerank` defaults to False: ablation on the eval set showed the
    cross-encoder reranker only flips 2 of 44 non-adversarial queries
    while costing ~16 s/query on HF cpu-basic. Users who want the slow,
    more accurate pass can opt in via the bot's /deep command, which
    sets rerank=True.

    `stream_callback`, if provided, is invoked with `(delta, full_text)`
    on every streamed chunk from the LLM. It runs in the calling thread
    (this function is sync), so callers using asyncio must marshal to
    their event loop via `loop.call_soon_threadsafe`. The returned
    `answer` field is the FINAL text (with citation renumbering and the
    refusal scrubber applied) — same as the non-streaming path.
    """
    model = model or DEFAULT_MODEL
    retriever = retriever or Retriever()
    history = history or []

    # ── Rewrite follow-ups into standalone queries for retrieval ─────────
    t_rw = time.monotonic()
    if history:
        retrieval_query = _rewrite_for_retrieval(query, history, model)
    else:
        retrieval_query = query

    # Assemble the retrieval query set, fused via RRF in search(). The
    # rewrite helps follow-ups, but it can over-narrow or even translate
    # a self-contained question (injecting unrelated context), so we
    # never let it REPLACE the user's words:
    #   - retrieval_query (rewritten standalone) — the primary
    #   - the original query, whenever the rewrite changed it — so the
    #     user's actual intent always gets a vote
    #   - an English translation of the ORIGINAL when it's non-English.
    #     Detection is on the ORIGINAL (not the rewrite, which may have
    #     already anglicised it): our corpus is majority-English and
    #     BGE-M3 ranks same-language chunks above cross-language ones, so
    #     a non-English query otherwise under-retrieves the English half.
    extra_queries: list[str] = []
    if query.strip() and query.strip() != retrieval_query.strip():
        extra_queries.append(query)
    if _looks_non_english(query):
        translated = _translate_to_english(query, model)
        if translated:
            extra_queries.append(translated)
    rewrite_s = time.monotonic() - t_rw

    # ── Retrieve always; pass whatever we get to the LLM ─────────────────
    t0 = time.monotonic()
    hits = retriever.search(
        retrieval_query, k=k, rerank=rerank,
        extra_queries=extra_queries or None,
    )
    retrieval_s = time.monotonic() - t0
    top_score = hits[0]["score"] if hits else 0.0
    sources = _format_sources(hits)

    if top_score >= SOFT_REFUSAL_THRESHOLD:
        tier = "high"
    elif top_score >= HARD_REFUSAL_THRESHOLD:
        tier = "mid"
    else:
        tier = "low"

    context_block = _build_context(hits) if hits else "(no relevant content found)"
    # Trailing language directive keyed on the CURRENT query (not the
    # conversation history) — otherwise an earlier Arabic turn makes the
    # model answer a later English question in Arabic. Last line the model
    # reads, so it carries weight.
    user_message = (
        f"CONTEXT:\n{context_block}\n\n"
        f"USER MESSAGE: {query}\n\n"
        f"({_response_lang_directive(query)})"
    )

    messages = [{"role": "system", "content": _system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # Context-fill estimate: how much of the model's input window we're
    # using on this turn. Counted across system + history + context +
    # query — i.e. the actual payload going to the LLM. Best-effort:
    # LiteLLM doesn't know every model's window, so we fall back to a
    # rough chars/4 estimate when token_counter / get_model_info fail.
    ctx_tokens, ctx_max = _measure_context(model, messages)

    # ── LLM call with retry + fallback ──────────────────────────────────
    # Wraps the actual call so a transient Groq incident (over-capacity,
    # 429, internal 500) doesn't get surfaced to the user as a generic
    # error. 4 attempts total: primary 3× with backoff, then a lighter
    # fallback model on the same provider.
    t1 = time.monotonic()
    model_used = model
    try:
        if stream_callback is not None:
            answer_text, model_used = _stream_llm_with_retry(
                model, messages, stream_callback,
                temperature=0.2, timeout=LLM_TIMEOUT_S,
            )
        else:
            answer_text, model_used = _complete_llm_with_retry(
                model, messages, temperature=0.2, timeout=LLM_TIMEOUT_S,
            )
    except _TRANSIENT_LLM_ERRORS as e:
        logger.warning(
            "LLM unavailable after %d attempts: %s",
            len(_llm_attempt_specs(model, FALLBACK_MODELS)),
            type(e).__name__,
        )
        return {
            "query": query,
            "retrieval_query": retrieval_query if retrieval_query != query else None,
            "answer": _user_message_for_llm_error(e),
            "refused": True,
            "refusal_reason": f"{type(e).__name__}: {str(e)[:200]}",
            "model": model,
            "model_used": None,
            "top_score": top_score,
            "tier": "llm_unavailable",
            "has_citations": False,
            "sources": [],
            "context_tokens": ctx_tokens,
            "context_max": ctx_max,
            "timings": {"rewrite": rewrite_s, "retrieval": retrieval_s,
                        "answer": time.monotonic() - t1},
        }
    answer_s = time.monotonic() - t1

    # The LLM decides whether sources are relevant: if it cited any chunks,
    # we surface the sources block; otherwise (small talk, redirect, etc.)
    # we hide it entirely.
    # Renumber inline citations to [1], [2], … and trim the sources block
    # to only the chunks the LLM actually cited (in citation order).
    answer_text, surfaced_sources = _renumber_citations(answer_text, sources)
    has_citations = bool(surfaced_sources)

    return {
        "query": query,
        "retrieval_query": retrieval_query if retrieval_query != query else None,
        "answer": answer_text,
        "refused": False,
        "refusal_reason": None,
        "model": model,
        # `model_used` differs from `model` when the fallback model
        # actually answered. Useful for monitoring how often the primary
        # is unavailable.
        "model_used": model_used,
        "top_score": top_score,
        "tier": tier,
        "has_citations": has_citations,
        "sources": surfaced_sources,
        "context_tokens": ctx_tokens,
        "context_max": ctx_max,
        "timings": {"rewrite": rewrite_s, "retrieval": retrieval_s, "answer": answer_s},
    }


# ─── CLI ────────────────────────────────────────────────────────────────────

def _print_result(result: dict) -> None:
    print(f"\nQuery:       {result['query']}")
    print(f"Model:       {result['model']}")
    print(f"Top score:   {result['top_score']:.3f}  (tier: {result['tier']})")
    if result["refused"]:
        print(f"Refused:     {result['refusal_reason']}")
    print(f"\nAnswer:\n{result['answer']}\n")
    print("Sources:")
    for s in result["sources"]:
        md = s["metadata"]
        loc = (
            f"chat | topic={md.get('topic') or '—'}"
            if md.get("source_type") == "chat"
            else f"pdf={md.get('pdf_file', '?')}"
        )
        print(f"  [{s['score']:+.3f}] {s['id']} ({loc})")
        print(f"          {s['preview']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("query", help="Natural-language question")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--model", default=None,
                        help="Override CHATBOT_LLM_MODEL (e.g. anthropic/claude-sonnet-4-5)")
    args = parser.parse_args()

    result = answer(args.query, k=args.k, model=args.model)
    _print_result(result)


if __name__ == "__main__":
    main()

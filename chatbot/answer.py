"""End-to-end answer generation: retrieve → build context → LLM → answer.

Three-tier refusal based on the top-1 reranker score (calibrated by the
k-sweep experiment in eval/reports/2026-05-04_1013_k_sweep.md):

   score < 0.20   → hard refuse (no LLM call)
   0.20–0.50      → soft mode: pass context + extra "verify or refuse" instruction
   score ≥ 0.50   → normal mode: standard answer prompt

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
import os
import re
import time
from pathlib import Path
from typing import Any

import litellm
from dotenv import load_dotenv

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_MODEL = os.getenv("CHATBOT_LLM_MODEL", "groq/llama-3.3-70b-versatile")
HARD_REFUSAL_THRESHOLD = 0.20
SOFT_REFUSAL_THRESHOLD = 0.50

SYSTEM_PROMPT = """\
You are the assistant for the ENSIA Impact community — a Telegram group for \
ENSIA students, faculty, and partners covering startups, research, internships, \
opportunities, patents, events, and more.

Each user message arrives with CONTEXT — chunks the search system pulled from \
the server's content. The CONTEXT may or may not be relevant to the message; \
you decide.

Pick the right behavior:

1. SMALL TALK (greeting, "thanks", introducing themselves, asking who you are \
or what you do, casual chat):
   Reply warmly and briefly. On a first-time greeting, mention you can help \
find information from the ENSIA Impact server. **Do NOT cite anything for \
small talk** — ignore the CONTEXT.

2. SERVER QUESTION the CONTEXT actually answers:
   Answer using only the CONTEXT. **Cite chunks inline as [chunk_id]** right \
after the claim they support, e.g. "[chat_832]", "[pdf_ENSIA_3]".

3. SERVER QUESTION the CONTEXT does NOT cover:
   Say you don't have specific info on that yet, and suggest related server \
topics they could ask about (CDE, the incubator, internships, decree 1275, \
scholarships, events, etc.). Don't fabricate citations.

4. CLEARLY OFF-TOPIC (write code, weather, world events, math homework, \
personal/private info about real people):
   Politely say your scope is the ENSIA Impact server's content and invite \
them to ask about server topics. **Be tolerant** — if a question is even \
plausibly server-related, treat it as case 3 instead of this one.

LANGUAGE: respond in the user's language (English / French / Arabic / mixed).
TONE: warm but concise. Two or three sentences is usually enough.
NEVER make up [chunk_id] citations. Only cite IDs that appear in the CONTEXT.
"""

# Detects [chunk_id] citations in the LLM's answer. Used to decide whether
# to show the "📚 Sources" block — if the LLM didn't cite anything, the
# answer was either small talk or a redirect, and a sources block would be
# noisy/misleading.
_CITATION_RE = re.compile(r"\[(chat_\d+|pdf_[\w\-]+_\d+)\]")

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
    """Compact human-readable label for a retrieved chunk."""
    if metadata.get("source_type") == "chat":
        topic = metadata.get("topic") or "no topic"
        sender = metadata.get("sender") or "unknown"
        date = (metadata.get("date") or "")[:10]
        return f"chat msg | topic: {topic} | sender: {sender} | date: {date}"
    if metadata.get("source_type") == "pdf":
        return f"PDF: {metadata.get('pdf_file', '?')} | chunk #{metadata.get('chunk_index', '?')}"
    return "unknown source"


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
    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": REWRITER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            timeout=REWRITER_TIMEOUT_S,
            max_tokens=REWRITER_MAX_TOKENS,
        )
        rewritten = (response.choices[0].message.content or "").strip()
        # Strip surrounding quotes if the model added them anyway.
        if (rewritten.startswith('"') and rewritten.endswith('"')) or (
            rewritten.startswith("'") and rewritten.endswith("'")
        ):
            rewritten = rewritten[1:-1].strip()
        return rewritten or query
    except Exception:
        return query


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
) -> dict[str, Any]:
    """Generate an answer for `query` using retrieve → reranker → LLM.

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
    rewrite_s = time.monotonic() - t_rw

    # ── Retrieve always; pass whatever we get to the LLM ─────────────────
    t0 = time.monotonic()
    hits = retriever.search(retrieval_query, k=k, rerank=True)
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
    user_message = f"CONTEXT:\n{context_block}\n\nUSER MESSAGE: {query}"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # ── LLM call (with timeout to bound worst-case latency) ──────────────
    t1 = time.monotonic()
    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=0.2,
            timeout=LLM_TIMEOUT_S,
        )
    except litellm.exceptions.Timeout:
        return {
            "query": query,
            "retrieval_query": retrieval_query if retrieval_query != query else None,
            "answer": (
                "I'm having trouble reaching the model right now — please try "
                "again in a moment."
            ),
            "refused": True,
            "refusal_reason": f"LLM timeout after {LLM_TIMEOUT_S}s",
            "model": model,
            "top_score": top_score,
            "tier": "llm_timeout",
            "sources": [],
            "timings": {"rewrite": rewrite_s, "retrieval": retrieval_s,
                        "answer": time.monotonic() - t1},
        }
    answer_s = time.monotonic() - t1
    answer_text = response.choices[0].message.content.strip()

    # The LLM decides whether sources are relevant: if it cited any chunks,
    # we surface the sources block; otherwise (small talk, redirect, etc.)
    # we hide it entirely.
    has_citations = bool(_CITATION_RE.search(answer_text))
    surfaced_sources = sources if has_citations else []

    return {
        "query": query,
        "retrieval_query": retrieval_query if retrieval_query != query else None,
        "answer": answer_text,
        "refused": False,
        "refusal_reason": None,
        "model": model,
        "top_score": top_score,
        "tier": tier,
        "has_citations": has_citations,
        "sources": surfaced_sources,
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

"""Single consolidated retrieval-only evaluation of the current pipeline.

Runs both the golden set (used during retriever tuning) and the held-out
validation set through the production retriever (BGE-M3 + bge-reranker-v2-m3,
top-10 → top-5, reranker max_length=512). Produces ONE markdown report
covering:

  • Recall@5 overall, per dataset (golden vs validation), per category,
    per language
  • First-hit position histogram (does k=5 leave recall on the table?)
  • Score distribution: in-corpus hits / in-corpus misses / adversarial
  • Adversarial top-1 reranker scores (refusal-signal calibration)
  • Failure list with the top-5 the retriever returned

This file replaces the older per-config reports (phase1, phase2, phase3,
validation, k_sweep, stress_dense). Re-run any time the retriever
config changes.

Output: eval/reports/<timestamp>_retrieval.md

Usage:
    PYTHONPATH=. .venv/bin/python eval/retrieval_report.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

from chatbot.retrieve import (
    CANDIDATE_POOL,
    MODEL_NAME,
    RERANK_MAX_TOKENS,
    RERANKER_NAME,
    Retriever,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval/golden_set.json"
VALIDATION = ROOT / "eval/validation_set.json"
REPORTS = ROOT / "eval/reports"


# ─── matching ───────────────────────────────────────────────────────────────

def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def hit_matches_expected(hit: dict, expected: dict) -> bool:
    md = hit["metadata"]
    if expected["kind"] == "message":
        return (
            md.get("source_type") == "chat"
            and md.get("message_id") == expected["ref"]
        )
    if expected["kind"] == "pdf":
        if md.get("source_type") != "pdf":
            return False
        if nfc(md.get("pdf_file", "")) != nfc(expected["ref"]):
            return False
        if "must_contain" in expected:
            return expected["must_contain"].lower() in hit["text"].lower()
        return True
    return False


def first_hit_position(query: dict, hits: list[dict]) -> int | None:
    """1-based index of the first hit matching any expected source, else None."""
    for i, h in enumerate(hits):
        if any(hit_matches_expected(h, exp) for exp in query["expected_sources"]):
            return i + 1
    return None


# ─── data loading ───────────────────────────────────────────────────────────

def load_set(path: Path, label: str) -> list[dict]:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for q in bundle["queries"]:
        q["_set"] = label
        out.append(q)
    return out


# ─── main run ──────────────────────────────────────────────────────────────

def run(k: int) -> dict:
    queries = load_set(GOLDEN, "golden") + load_set(VALIDATION, "validation")
    retriever = Retriever()

    print(f"Running {len(queries)} queries through the retriever (k={k})…", flush=True)
    rows = []
    for i, q in enumerate(queries, 1):
        hits = retriever.search(q["query"], k=k, rerank=True)
        is_adv = q.get("must_refuse", False)
        first = None if is_adv else first_hit_position(q, hits)
        rows.append({
            "query": q,
            "hits": hits,
            "is_adversarial": is_adv,
            "first_hit_position": first,
            "any_expected_hit": (first is not None) if not is_adv else None,
            "all_expected_hit": (
                None if is_adv else _all_expected_hit(q, hits)
            ),
            "top1_score": hits[0]["score"] if hits else None,
        })
        if i % 10 == 0 or i == len(queries):
            print(f"  [{i}/{len(queries)}]", flush=True)

    return {"k": k, "rows": rows, "queries": queries}


def _all_expected_hit(query: dict, hits: list[dict]) -> bool:
    for exp in query["expected_sources"]:
        if not any(hit_matches_expected(h, exp) for h in hits):
            return False
    return True


# ─── aggregation helpers ────────────────────────────────────────────────────

def _recall(rows: list[dict], *, mode: str) -> tuple[int, int]:
    """mode: 'any' or 'all'. Returns (hits, total) over non-adversarial rows."""
    hits = total = 0
    key = "any_expected_hit" if mode == "any" else "all_expected_hit"
    for r in rows:
        if r["is_adversarial"]:
            continue
        total += 1
        if r[key]:
            hits += 1
    return hits, total


def _bucket_recall(rows, key_fn) -> dict[str, dict[str, tuple[int, int]]]:
    """Group rows by a key, return {key: {'any': (h,t), 'all': (h,t)}}."""
    out: dict[str, dict[str, list[int]]] = {}
    for r in rows:
        if r["is_adversarial"]:
            continue
        k = key_fn(r)
        b = out.setdefault(k, {"any": [0, 0], "all": [0, 0]})
        b["any"][1] += 1
        b["all"][1] += 1
        if r["any_expected_hit"]:
            b["any"][0] += 1
        if r["all_expected_hit"]:
            b["all"][0] += 1
    return {k: {m: (v[0], v[1]) for m, v in d.items()} for k, d in out.items()}


def _stats(values: list[float]) -> str:
    if not values:
        return "n=0"
    qs = statistics.quantiles(values, n=4) if len(values) >= 4 else [min(values), statistics.median(values), max(values)]
    return (
        f"n={len(values)}, min={min(values):.3f}, p25={qs[0]:.3f}, "
        f"median={statistics.median(values):.3f}, p75={qs[-1]:.3f}, "
        f"max={max(values):.3f}"
    )


# ─── rendering ──────────────────────────────────────────────────────────────

def render(report: dict) -> str:
    k = report["k"]
    rows = report["rows"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    out: list[str] = []
    out.append(f"# Retrieval Performance Report\n")
    out.append(f"_{timestamp}, k={k}_\n\n")

    # ── Pipeline under test ──────────────────────────────────────────────
    out.append("## Pipeline tested\n\n")
    out.append(
        f"- Embedding model: `{MODEL_NAME}`\n"
        f"- Reranker: `{RERANKER_NAME}`\n"
        f"- Candidate pool (dense): **{CANDIDATE_POOL}**\n"
        f"- Reranker max input length: **{RERANK_MAX_TOKENS} tokens**\n"
        f"- Final top-k returned: **{k}**\n\n"
    )

    # ── Headline ─────────────────────────────────────────────────────────
    h_any, t_any = _recall(rows, mode="any")
    h_all, t_all = _recall(rows, mode="all")
    out.append("## Headline\n\n")
    out.append(
        f"- **Recall (any expected source) @{k}**: "
        f"**{h_any/t_any:.1%}** ({h_any}/{t_any})\n"
    )
    out.append(
        f"- **Recall (all expected sources) @{k}**: "
        f"**{h_all/t_all:.1%}** ({h_all}/{t_all})\n"
    )
    n_adv = sum(1 for r in rows if r["is_adversarial"])
    out.append(f"- Adversarial queries: {n_adv} (no LLM step in this report — "
               "see `chatbot/answer.py` for refusal handling)\n\n")

    # ── By dataset ───────────────────────────────────────────────────────
    out.append("## By dataset (overfit signal)\n\n")
    out.append("Golden = used during retriever tuning. Validation = held-out, used once.\n\n")
    out.append("| Dataset | Count | Recall (any) | Recall (all) |\n")
    out.append("|---|---|---|---|\n")
    by_set = _bucket_recall(rows, lambda r: r["query"]["_set"])
    for s in ("golden", "validation"):
        if s not in by_set:
            continue
        a_h, a_t = by_set[s]["any"]
        l_h, l_t = by_set[s]["all"]
        out.append(
            f"| {s} | {a_t} | "
            f"{a_h/a_t:.0%} ({a_h}/{a_t}) | "
            f"{l_h/l_t:.0%} ({l_h}/{l_t}) |\n"
        )
    out.append("\n")

    # ── By category ──────────────────────────────────────────────────────
    out.append("## By category\n\n")
    out.append("| Category | Count | Recall (any) | Recall (all) |\n")
    out.append("|---|---|---|---|\n")
    by_cat = _bucket_recall(rows, lambda r: r["query"]["category"])
    for cat in sorted(by_cat):
        a_h, a_t = by_cat[cat]["any"]
        l_h, l_t = by_cat[cat]["all"]
        out.append(
            f"| {cat} | {a_t} | "
            f"{a_h/a_t:.0%} ({a_h}/{a_t}) | "
            f"{l_h/l_t:.0%} ({l_h}/{l_t}) |\n"
        )
    out.append("\n")

    # ── By language ──────────────────────────────────────────────────────
    out.append("## By language\n\n")
    out.append("| Language | Count | Recall (any) |\n")
    out.append("|---|---|---|\n")
    by_lang = _bucket_recall(rows, lambda r: r["query"]["language"])
    for lang in sorted(by_lang):
        a_h, a_t = by_lang[lang]["any"]
        out.append(f"| {lang} | {a_t} | {a_h/a_t:.0%} ({a_h}/{a_t}) |\n")
    out.append("\n")

    # ── First-hit position histogram ─────────────────────────────────────
    out.append("## First-hit position\n\n")
    out.append("Where does the first matching expected source land in the top-k?\n\n")
    out.append("| Position | Count |\n|---|---|\n")
    pos_counter: Counter = Counter()
    for r in rows:
        if r["is_adversarial"]:
            continue
        p = r["first_hit_position"]
        if p is None:
            pos_counter["miss"] += 1
        else:
            pos_counter[p] += 1
    for p in sorted(x for x in pos_counter if x != "miss"):
        out.append(f"| #{p} | {pos_counter[p]} |\n")
    if pos_counter.get("miss"):
        out.append(f"| not in top-{k} | {pos_counter['miss']} |\n")
    out.append("\n")

    # ── Score distribution ───────────────────────────────────────────────
    out.append("## Top-1 reranker score distribution\n\n")
    in_hit = [r["top1_score"] for r in rows
              if not r["is_adversarial"] and r["any_expected_hit"]
              and r["top1_score"] is not None]
    in_miss = [r["top1_score"] for r in rows
               if not r["is_adversarial"] and not r["any_expected_hit"]
               and r["top1_score"] is not None]
    adv = [r["top1_score"] for r in rows
           if r["is_adversarial"] and r["top1_score"] is not None]
    out.append("| Query class | Stats |\n|---|---|\n")
    out.append(f"| In-corpus, hit at least one expected source | {_stats(in_hit)} |\n")
    out.append(f"| In-corpus, missed all expected sources      | {_stats(in_miss)} |\n")
    out.append(f"| Adversarial (must_refuse=true)              | {_stats(adv)} |\n\n")
    out.append(
        "_The chatbot no longer uses a hard refusal threshold — these scores "
        "are reported only as a calibration signal. Refusal is now handled "
        "by the LLM-as-router system prompt._\n\n"
    )

    # ── Failures ─────────────────────────────────────────────────────────
    failures = [r for r in rows if not r["is_adversarial"] and not r["any_expected_hit"]]
    out.append(f"## Misses ({len(failures)})\n\n")
    if not failures:
        out.append("_None._\n\n")
    else:
        out.append("Queries where no expected source landed in the top-k.\n\n")
        for r in failures:
            q = r["query"]
            out.append(f"### {q['id']} (`{q['_set']}`, `{q['category']}`)\n")
            out.append(f"**Query**: {q['query']}\n\n")
            out.append("**Expected**:\n")
            for exp in q["expected_sources"]:
                if exp["kind"] == "message":
                    out.append(f"  - chat msg `{exp['ref']}`\n")
                else:
                    must = f' (must contain `{exp["must_contain"]}`)' if "must_contain" in exp else ""
                    out.append(f"  - PDF `{exp['ref']}`{must}\n")
            out.append("\n**Top-k retrieved**:\n")
            for h in r["hits"]:
                md = h["metadata"]
                if md.get("source_type") == "chat":
                    where = f"chat msg {md.get('message_id')} | {md.get('topic') or '—'}"
                else:
                    where = f"PDF {md.get('pdf_file')} | chunk {md.get('chunk_index')}"
                preview = h["text"][:100].replace("\n", " ")
                out.append(f"  - [{h['score']:+.3f}] `{h['id']}` ({where}) — {preview}\n")
            out.append("\n")

    # ── Adversarial detail ───────────────────────────────────────────────
    out.append("## Adversarial top-1 scores\n\n")
    out.append("| ID | Set | Top-1 score |\n|---|---|---|\n")
    for r in rows:
        if not r["is_adversarial"]:
            continue
        out.append(f"| {r['query']['id']} | {r['query']['_set']} | {r['top1_score']:.3f} |\n")
    out.append("\n")

    # ── Per-query summary ────────────────────────────────────────────────
    out.append("## Per-query summary\n\n")
    out.append("| ID | Set | Cat | Lang | First hit | Top-1 |\n")
    out.append("|---|---|---|---|---|---|\n")
    for r in rows:
        q = r["query"]
        if r["is_adversarial"]:
            first = "(adv)"
        elif r["first_hit_position"] is None:
            first = "❌"
        else:
            first = f"#{r['first_hit_position']}"
        score = f"{r['top1_score']:.3f}" if r["top1_score"] is not None else "—"
        out.append(
            f"| {q['id']} | {q['_set']} | {q['category']} | {q['language']} | "
            f"{first} | {score} |\n"
        )

    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    report = run(k=args.k)
    md = render(report)

    out_path = REPORTS / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_retrieval.md"
    out_path.write_text(md, encoding="utf-8")

    h_any, t_any = _recall(report["rows"], mode="any")
    h_all, t_all = _recall(report["rows"], mode="all")
    print()
    print(f"Recall (any) @{args.k}: {h_any/t_any:.1%} ({h_any}/{t_any})")
    print(f"Recall (all) @{args.k}: {h_all/t_all:.1%} ({h_all}/{t_all})")
    print(f"Report: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

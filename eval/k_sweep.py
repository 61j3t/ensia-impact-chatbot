"""K-sweep and reranker-score distribution analysis.

Two questions answered in one report:

  1. Does Recall@k plateau at k=5, or are we leaving recall on the table
     by truncating early?

  2. Where do in-corpus queries' top-1 reranker scores fall vs adversarial
     queries' scores? Is there a clean threshold for retrieval-side refusal?

Method: for each of the 54 queries (golden + validation), retrieve k=20
with reranker enabled. Then derive Recall@1, @3, @5, @10, @20 from the
same hit list (no need to re-run for each k).

Output:
  eval/reports/<timestamp>_k_sweep.md

Usage:
  PYTHONPATH=. .venv/bin/python eval/k_sweep.py
"""

from __future__ import annotations

import argparse
import json
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval/golden_set.json"
VALIDATION = ROOT / "eval/validation_set.json"
REPORTS = ROOT / "eval/reports"

K_VALUES = [1, 2, 3, 4, 5]
MAX_K = max(K_VALUES)


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


def first_hit_index(query: dict, hits: list[dict]) -> int | None:
    """Return the 1-based index of the first hit matching any expected source."""
    for i, h in enumerate(hits):
        if any(hit_matches_expected(h, exp) for exp in query["expected_sources"]):
            return i + 1
    return None


def load_all_queries() -> list[dict]:
    queries: list[dict] = []
    for path in (GOLDEN, VALIDATION):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for q in bundle["queries"]:
            q["_source_set"] = path.stem
            queries.append(q)
    return queries


def run_sweep() -> dict:
    import torch
    print("Loading retriever (BGE-M3 + reranker, both MPS)…", flush=True)
    retriever = Retriever()
    queries = load_all_queries()
    print(f"Running {len(queries)} queries at k={MAX_K} (reranker on)", flush=True)

    rows: list[dict] = []
    t_start = time.time()
    for qi, q in enumerate(queries, 1):
        hits = retriever.search(q["query"], k=MAX_K, rerank=True)
        # Avoid MPS allocator buildup that stalls batch use.
        if torch.backends.mps.is_available():
            torch.mps.synchronize()
            torch.mps.empty_cache()
        is_adv = q.get("must_refuse", False)
        first_at = None if is_adv else first_hit_index(q, hits)
        rows.append({
            "query": q,
            "is_adversarial": is_adv,
            "first_hit_position": first_at,
            "all_scores": [h["score"] for h in hits],
            "all_ids": [h["id"] for h in hits],
        })
        if qi % 10 == 0 or qi == len(queries):
            elapsed = time.time() - t_start
            rate = qi / elapsed if elapsed > 0 else 0
            eta = (len(queries) - qi) / rate if rate > 0 else 0
            print(f"  [{qi}/{len(queries)}] {rate:.1f} q/s, ETA {eta:.0f}s", flush=True)

    return {"rows": rows, "queries": queries}


# ─── analysis ───────────────────────────────────────────────────────────────

def recall_at_k(rows: list[dict], k: int, *, category_filter: str | None = None) -> tuple[int, int]:
    hits = 0
    total = 0
    for r in rows:
        if r["is_adversarial"]:
            continue
        if category_filter is not None and r["query"]["category"] != category_filter:
            continue
        total += 1
        pos = r["first_hit_position"]
        if pos is not None and pos <= k:
            hits += 1
    return hits, total


def find_threshold(in_corpus_hit_scores: list[float],
                   adversarial_scores: list[float]) -> dict:
    """Find the best top-1 reranker score threshold separating in-corpus hits
    from adversarial queries. We sweep candidate thresholds and report the one
    that maximises (true_positive_rate + true_negative_rate) / 2 — balanced
    accuracy.
    """
    if not in_corpus_hit_scores or not adversarial_scores:
        return {"threshold": None, "tp_rate": 0, "tn_rate": 0, "balanced": 0}

    candidates = sorted(set(in_corpus_hit_scores + adversarial_scores))
    best = {"threshold": candidates[0], "tp_rate": 0.0, "tn_rate": 0.0, "balanced": 0.0}
    for t in candidates:
        tp = sum(1 for s in in_corpus_hit_scores if s >= t)
        tn = sum(1 for s in adversarial_scores if s < t)
        tp_rate = tp / len(in_corpus_hit_scores)
        tn_rate = tn / len(adversarial_scores)
        balanced = (tp_rate + tn_rate) / 2
        if balanced > best["balanced"]:
            best = {"threshold": t, "tp_rate": tp_rate, "tn_rate": tn_rate, "balanced": balanced}
    return best


# ─── reporting ──────────────────────────────────────────────────────────────

def render_report(report: dict) -> str:
    rows = report["rows"]
    out: list[str] = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out.append(f"# K-sweep & Score-distribution Analysis\n_{timestamp}_\n\n")
    out.append(
        f"Pipeline: BGE-M3 dense → top-{MAX_K} → reranker (bge-reranker-v2-m3)\n"
        f"Corpus: {sum(1 for r in rows if not r['is_adversarial'])} non-adversarial + "
        f"{sum(1 for r in rows if r['is_adversarial'])} adversarial queries from "
        f"golden_set.json + validation_set.json.\n\n"
    )

    # ── Recall@k curve (overall) ──────────────────────────────────────────
    out.append("## Recall@k curve\n\n")
    out.append("| k | Recall (overall) | Δ vs k=1 |\n|---|---|---|\n")
    base_hits, base_total = recall_at_k(rows, 1)
    base_recall = base_hits / base_total if base_total else 0
    for k in K_VALUES:
        hits, total = recall_at_k(rows, k)
        recall = hits / total if total else 0
        delta = (recall - base_recall) * 100
        sign = "+" if delta >= 0 else ""
        out.append(f"| {k} | {recall:.1%} ({hits}/{total}) | {sign}{delta:.1f} |\n")
    out.append("\n")

    # ── Where exactly do hits land? ──────────────────────────────────────
    out.append("## First-hit position histogram\n\n")
    positions = [r["first_hit_position"] for r in rows
                 if not r["is_adversarial"] and r["first_hit_position"] is not None]
    bins = [(1, 1), (2, 3), (4, 5), (6, 10), (11, 20)]
    out.append("| Position | Count | % of all non-adversarial queries |\n|---|---|---|\n")
    total_non_adv = sum(1 for r in rows if not r["is_adversarial"])
    for lo, hi in bins:
        c = sum(1 for p in positions if lo <= p <= hi)
        pct = c / total_non_adv * 100
        label = f"#{lo}" if lo == hi else f"#{lo}–{hi}"
        out.append(f"| {label} | {c} | {pct:.0f}% |\n")
    not_in_top20 = total_non_adv - len(positions)
    out.append(f"| Not in top-{MAX_K} | {not_in_top20} | {not_in_top20/total_non_adv*100:.0f}% |\n\n")

    # ── Recall@k by category ──────────────────────────────────────────────
    out.append("## Recall@k by category\n\n")
    categories = sorted({r["query"]["category"] for r in rows
                         if not r["is_adversarial"]})
    header = "| Category | " + " | ".join(f"@{k}" for k in K_VALUES) + " | n |\n"
    out.append(header)
    out.append("|" + "---|" * (len(K_VALUES) + 2) + "\n")
    for cat in categories:
        cells = [cat]
        n = sum(1 for r in rows if not r["is_adversarial"]
                and r["query"]["category"] == cat)
        for k in K_VALUES:
            hits, total = recall_at_k(rows, k, category_filter=cat)
            cells.append(f"{hits/total:.0%}" if total else "—")
        cells.append(str(n))
        out.append("| " + " | ".join(cells) + " |\n")
    out.append("\n")

    # ── Score distribution analysis ──────────────────────────────────────
    out.append("## Top-1 reranker score distribution\n\n")

    # Bucket queries
    in_corpus_hit = []   # has expected source in top-k AND has scores
    in_corpus_miss = []
    adversarial = []
    for r in rows:
        score = r["all_scores"][0] if r["all_scores"] else None
        if score is None:
            continue
        if r["is_adversarial"]:
            adversarial.append(score)
        elif r["first_hit_position"] is not None:
            in_corpus_hit.append(score)
        else:
            in_corpus_miss.append(score)

    def stats(values: list[float]) -> str:
        if not values:
            return "—"
        v = sorted(values)
        n = len(v)

        def pct(p):
            return v[int(n * p)] if int(n * p) < n else v[-1]

        return f"n={n}, min={min(v):.3f}, p25={pct(0.25):.3f}, median={pct(0.5):.3f}, p75={pct(0.75):.3f}, max={max(v):.3f}"

    out.append("| Query class | Top-1 score stats |\n|---|---|\n")
    out.append(f"| In-corpus, retrieved an expected source | {stats(in_corpus_hit)} |\n")
    out.append(f"| In-corpus, missed all expected sources  | {stats(in_corpus_miss)} |\n")
    out.append(f"| Adversarial (must_refuse=true)          | {stats(adversarial)} |\n\n")

    # Threshold analysis
    threshold = find_threshold(in_corpus_hit, adversarial)
    out.append("## Refusal-threshold analysis\n\n")
    out.append("If the chatbot rejects answers when top-1 reranker score < T:\n\n")
    if threshold["threshold"] is None:
        out.append("_Insufficient data for threshold analysis._\n\n")
    else:
        out.append(f"- **Optimal threshold** (balanced accuracy): T = **{threshold['threshold']:.3f}**\n")
        out.append(f"- True-positive rate (real queries kept): **{threshold['tp_rate']:.1%}**\n")
        out.append(f"- True-negative rate (adversarial refused): **{threshold['tn_rate']:.1%}**\n")
        out.append(f"- Balanced accuracy: **{threshold['balanced']:.1%}**\n\n")

        # Show what happens at a few candidate thresholds
        out.append("Threshold trade-offs:\n\n")
        out.append("| T | Real queries kept | Adversarial refused |\n|---|---|---|\n")
        for t in [0.1, 0.2, 0.3, 0.4, 0.5]:
            tp = sum(1 for s in in_corpus_hit if s >= t)
            tn = sum(1 for s in adversarial if s < t)
            out.append(
                f"| {t} | {tp/len(in_corpus_hit):.0%} ({tp}/{len(in_corpus_hit)}) "
                f"| {tn/len(adversarial):.0%} ({tn}/{len(adversarial)}) |\n"
            )
        out.append("\n")

    # ── Detailed per-query positions ──────────────────────────────────────
    out.append("## Per-query first-hit position\n\n")
    out.append("| ID | Set | Category | First hit at | Top-1 score |\n|---|---|---|---|---|\n")
    for r in sorted(rows, key=lambda r: r["query"]["id"]):
        q = r["query"]
        score = r["all_scores"][0] if r["all_scores"] else None
        if r["is_adversarial"]:
            pos = "(adv)"
        elif r["first_hit_position"] is None:
            pos = f"❌ not in top-{MAX_K}"
        else:
            pos = f"#{r['first_hit_position']}"
        score_str = f"{score:.3f}" if score is not None else "—"
        out.append(f"| {q['id']} | {q['_source_set'].replace('_set','')} | "
                   f"{q['category']} | {pos} | {score_str} |\n")
    out.append("\n")

    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    print("Running k-sweep…", flush=True)

    report = run_sweep()
    md = render_report(report)
    out_path = REPORTS / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_k_sweep.md"
    out_path.write_text(md, encoding="utf-8")

    rows = report["rows"]
    print()
    print("=" * 60)
    print("HEADLINE")
    print("=" * 60)
    for k in K_VALUES:
        hits, total = recall_at_k(rows, k)
        print(f"  Recall@{k:>2}: {hits/total:.1%} ({hits}/{total})")
    print(f"\nReport: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

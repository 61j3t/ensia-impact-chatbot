"""Reranker ablation: measure recall@5 + latency for different rerank
strategies, so we can decide whether to skip it (and when).

Strategies evaluated:

    dense_only       — never run the reranker
    rerank_always    — always run the reranker (production default)
    threshold(t1,t2) — skip rerank when dense top-1 score > t1 AND
                       (top-1 - top-2) > t2; else rerank

For each strategy, on each query, we record:
  • whether the retrieval surfaced any of the expected sources in top-5
  • the latency of the retrieval call(s)
  • for threshold variants: whether the reranker was skipped

Each query is retrieved ONCE with dense-only and ONCE with rerank, then
each strategy is evaluated by picking which to use. That way the per-
query time we compare is real wall-clock (this machine) for each
branch.

Note: latency numbers come from whatever machine you run this on. The
RANKING / recall numbers are deployment-independent, which is what we
actually need to pick a strategy. We've already measured wall-clock
latency on the deployed HF Space (~17s with rerank, much less without).

Usage:
    PYTHONPATH=. .venv/bin/python eval/rerank_ablation.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import unicodedata
from pathlib import Path

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden_set.json"
VALIDATION = ROOT / "eval" / "validation_set.json"

# Thresholds to evaluate. (t1, t2) means: skip rerank when top1 dense
# score > t1 AND (top1 - top2) > t2. Lower thresholds = skip more, hurt
# recall more. Higher = skip less, save less time.
THRESHOLD_CONFIGS = [
    (0.75, 0.03),
    (0.80, 0.05),
    (0.85, 0.05),
    (0.85, 0.10),
]


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


def any_expected_in_top_k(query: dict, hits: list[dict]) -> bool:
    expected = query.get("expected_sources") or []
    if not expected:
        return False  # adversarial queries — handled separately
    return any(
        hit_matches_expected(h, e) for h in hits for e in expected
    )


def load_set(path: Path, label: str) -> list[dict]:
    bundle = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for q in bundle["queries"]:
        q["_set"] = label
        out.append(q)
    return out


def evaluate_strategy(
    rows: list[dict],
    pick_fn,
) -> dict:
    """Apply pick_fn(row) → top-K hits, then measure recall + latency."""
    correct = 0
    total = 0
    latencies = []
    skipped_rerank = 0
    for row in rows:
        q = row["query"]
        if not q.get("expected_sources"):
            continue  # only score non-adversarial queries
        choice = pick_fn(row)  # {hits, latency, skipped}
        latencies.append(choice["latency"])
        if choice["skipped"]:
            skipped_rerank += 1
        total += 1
        if any_expected_in_top_k(q, choice["hits"]):
            correct += 1
    return {
        "recall": correct / total if total else 0.0,
        "correct": correct,
        "total": total,
        "p50_latency": statistics.median(latencies) if latencies else 0.0,
        "mean_latency": statistics.mean(latencies) if latencies else 0.0,
        "skip_rate": skipped_rerank / total if total else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()
    K = args.k

    print("Loading queries…")
    queries = load_set(GOLDEN, "golden") + load_set(VALIDATION, "validation")
    print(f"  {len(queries)} queries total")

    print("Loading retriever (BGE-M3 + reranker)…")
    r = Retriever()
    # Warm up.
    r.search("hello", k=1, rerank=True)
    print("  ready")
    print()

    # For each query, run dense-only AND reranked. Record both. We'll
    # then evaluate each strategy against this cached data.
    rows = []
    print(f"Running both branches on {len(queries)} queries…")
    for i, q in enumerate(queries, 1):
        # dense-only
        t0 = time.perf_counter()
        dense = r.search(q["query"], k=K, rerank=False)
        dense_lat = time.perf_counter() - t0
        # reranked
        t0 = time.perf_counter()
        rerank = r.search(q["query"], k=K, rerank=True)
        rerank_lat = time.perf_counter() - t0

        rows.append({
            "query": q,
            "dense_hits": dense,
            "dense_lat": dense_lat,
            "rerank_hits": rerank,
            "rerank_lat": rerank_lat,
        })
        if i % 10 == 0 or i == len(queries):
            print(f"  {i}/{len(queries)}")
    print()

    # ─── strategies ─────────────────────────────────────────────────
    def pick_dense_only(row):
        return {"hits": row["dense_hits"], "latency": row["dense_lat"], "skipped": True}

    def pick_rerank_always(row):
        return {"hits": row["rerank_hits"], "latency": row["rerank_lat"], "skipped": False}

    def pick_threshold(t1, t2):
        def pick(row):
            d = row["dense_hits"]
            if len(d) >= 2 and d[0]["dense_score"] > t1 and (
                d[0]["dense_score"] - d[1]["dense_score"] > t2
            ):
                # Skip rerank — use dense top-K, only paid dense latency.
                return {"hits": d, "latency": row["dense_lat"], "skipped": True}
            # Have to rerank; pay BOTH dense + rerank time (dense is part
            # of the rerank path in production anyway, so this matches).
            return {
                "hits": row["rerank_hits"],
                "latency": row["rerank_lat"],
                "skipped": False,
            }
        return pick

    strategies = {
        "dense_only": pick_dense_only,
        "rerank_always": pick_rerank_always,
    }
    for t1, t2 in THRESHOLD_CONFIGS:
        strategies[f"threshold({t1},{t2})"] = pick_threshold(t1, t2)

    # ─── report ─────────────────────────────────────────────────────
    print("=" * 78)
    print(
        f"{'Strategy':<22} {'Recall@'+str(K):>10} {'Skip %':>8} "
        f"{'Mean lat':>10} {'p50 lat':>10}"
    )
    print("=" * 78)

    baseline = None
    for name, pick in strategies.items():
        m = evaluate_strategy(rows, pick)
        if name == "rerank_always":
            baseline = m
        rec = f"{m['recall']*100:.1f}% ({m['correct']}/{m['total']})"
        skip = f"{m['skip_rate']*100:.0f}%"
        mean_l = f"{m['mean_latency']*1000:.0f} ms"
        p50_l = f"{m['p50_latency']*1000:.0f} ms"
        marker = " ⭐" if name == "rerank_always" else ""
        print(f"{name:<22} {rec:>10} {skip:>8} {mean_l:>10} {p50_l:>10}{marker}")
    print("=" * 78)

    if baseline:
        print()
        print("Notes:")
        print("  ⭐ = current production default (always rerank)")
        print(f"  Latency above is local (this machine). On HF cpu-basic:")
        print(f"    dense-only ≈ {100} ms")
        print(f"    rerank      ≈ {17_000} ms")
        print(
            "  So a threshold that skips X% of rerank calls saves "
            "≈ X% × 17s per query on average."
        )


if __name__ == "__main__":
    main()

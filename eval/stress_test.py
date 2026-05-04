"""Stress test the retriever with automatic query perturbations.

For each non-adversarial query in golden_set.json + validation_set.json,
runs four programmatic perturbations and re-runs retrieval, then reports
how often each perturbation still surfaces an expected source.

Perturbations:
  1. lowercase_no_punct  — drop case + punctuation
  2. truncate_3          — keep only the first 3 words
  3. typos               — inject character drops / swaps / dupes at ~7%
  4. reverse             — reverse word order

Adversarial queries are excluded from recall metrics but tracked for
score stability — we report their top-1 reranker score under each
perturbation (low scores should stay low).

Output:
  eval/reports/<timestamp>_stress.md

Usage:
  PYTHONPATH=. .venv/bin/python eval/stress_test.py
  PYTHONPATH=. .venv/bin/python eval/stress_test.py --k 5
"""

from __future__ import annotations

import argparse
import json
import random
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval/golden_set.json"
VALIDATION = ROOT / "eval/validation_set.json"
REPORTS = ROOT / "eval/reports"

TYPO_RATE = 0.07  # chance of mutating a given alpha char
SEED = 42


# ─── perturbations ──────────────────────────────────────────────────────────

def perturb_lowercase_no_punct(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", q.lower(), flags=re.UNICODE)).strip()


def perturb_truncate_3(q: str) -> str:
    return " ".join(q.split()[:3])


def perturb_typos(q: str, seed: int) -> str:
    rng = random.Random(seed)
    chars = list(q)
    out: list[str] = []
    i = 0
    while i < len(chars):
        c = chars[i]
        if c.isalpha() and rng.random() < TYPO_RATE:
            op = rng.random()
            if op < 0.34:
                # drop the char
                pass
            elif op < 0.67 and i + 1 < len(chars) and chars[i + 1].isalpha():
                # swap with next
                out.append(chars[i + 1])
                out.append(c)
                i += 2
                continue
            else:
                # duplicate
                out.append(c)
                out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def perturb_reverse(q: str) -> str:
    return " ".join(reversed(q.split()))


PERTURBATIONS = [
    ("original",          lambda q, s: q),
    ("lowercase_no_punct", lambda q, s: perturb_lowercase_no_punct(q)),
    ("truncate_3",         lambda q, s: perturb_truncate_3(q)),
    ("typos",              lambda q, s: perturb_typos(q, s)),
    ("reverse",            lambda q, s: perturb_reverse(q)),
]


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


def any_expected_in_hits(query: dict, hits: list[dict]) -> bool:
    return any(
        any(hit_matches_expected(h, exp) for h in hits)
        for exp in query["expected_sources"]
    )


# ─── main ───────────────────────────────────────────────────────────────────

def load_all_queries() -> list[dict]:
    queries: list[dict] = []
    for path in (GOLDEN, VALIDATION):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for q in bundle["queries"]:
            q["_source_set"] = path.stem
            queries.append(q)
    return queries


def stress_test(k: int, rerank: bool) -> dict:
    import time
    label = "BGE-M3 + reranker" if rerank else "BGE-M3 dense only"
    print(f"Loading retriever ({label})…", flush=True)
    retriever = Retriever()
    queries = load_all_queries()
    total = len(queries) * len(PERTURBATIONS)
    print(
        f"Will run {len(queries)} queries × {len(PERTURBATIONS)} perturbations "
        f"= {total} retrievals",
        flush=True,
    )

    results: dict[str, list[dict]] = {p[0]: [] for p in PERTURBATIONS}

    done = 0
    t_start = time.time()
    for qi, q in enumerate(queries):
        seed = SEED + qi  # deterministic per query
        for name, fn in PERTURBATIONS:
            perturbed = fn(q["query"], seed)
            if not perturbed.strip():
                results[name].append({
                    "query": q,
                    "perturbed": perturbed,
                    "hits": [],
                    "hit_any": False,
                    "top1_score": None,
                    "skipped": True,
                })
                done += 1
                continue
            hits = retriever.search(perturbed, k=k, rerank=rerank)
            top1_score = hits[0]["score"] if hits else None
            if q.get("must_refuse", False):
                hit_any = None
            else:
                hit_any = any_expected_in_hits(q, hits)
            results[name].append({
                "query": q,
                "perturbed": perturbed,
                "hits": hits,
                "hit_any": hit_any,
                "top1_score": top1_score,
                "skipped": False,
            })
            done += 1
            if done % 20 == 0 or done == total:
                elapsed = time.time() - t_start
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  [{done}/{total}] {rate:.2f} q/s, ETA {eta:.0f}s", flush=True)

    return {"k": k, "rerank": rerank, "results": results, "queries": queries}


# ─── reporting ──────────────────────────────────────────────────────────────

def render_report(report: dict) -> str:
    out: list[str] = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    k = report["k"]
    out.append(f"# Retrieval Stress Test\n_{timestamp}, k={k}_\n\n")

    out.append(
        "Each query was run in 5 forms (original + 4 perturbations).  \n"
        "Adversarial queries are excluded from the recall stats but their "
        "top-1 reranker score is tracked.\n\n"
    )

    # ── Headline: Recall(any)@k per perturbation ──
    out.append("## Recall (any expected source) @k by perturbation\n\n")
    out.append("| Perturbation | Recall (any) @k | Δ vs original |\n|---|---|---|\n")

    base_hits = sum(
        1 for r in report["results"]["original"]
        if r["hit_any"] is True
    )
    base_total = sum(
        1 for r in report["results"]["original"]
        if r["hit_any"] is not None and not r["skipped"]
    )
    base_recall = base_hits / base_total if base_total else 0.0

    for name, _ in PERTURBATIONS:
        hits = sum(1 for r in report["results"][name] if r["hit_any"] is True)
        total = sum(
            1 for r in report["results"][name]
            if r["hit_any"] is not None and not r["skipped"]
        )
        recall = hits / total if total else 0.0
        delta = recall - base_recall
        sign = "+" if delta >= 0 else ""
        marker = "**" if name == "original" else ""
        out.append(
            f"| {marker}{name}{marker} | {recall:.1%} ({hits}/{total}) | "
            f"{sign}{delta * 100:.1f} |\n"
        )
    out.append("\n")

    # ── Brittle queries: failed under any perturbation ──
    out.append("## Brittle queries (passed `original` but failed ≥1 perturbation)\n\n")
    by_qid: dict[str, dict[str, bool]] = {}
    for name, _ in PERTURBATIONS:
        for r in report["results"][name]:
            qid = r["query"]["id"]
            by_qid.setdefault(qid, {})[name] = r["hit_any"]

    brittle_rows = []
    for qid, perts in sorted(by_qid.items()):
        if perts.get("original") is True:
            failures = [p for p, hit in perts.items()
                        if p != "original" and hit is False]
            if failures:
                brittle_rows.append((qid, failures))

    if not brittle_rows:
        out.append("_None._\n\n")
    else:
        out.append("| Query ID | Original | Failed under |\n|---|---|---|\n")
        for qid, failures in brittle_rows:
            q_obj = next(r["query"] for r in report["results"]["original"]
                         if r["query"]["id"] == qid)
            preview = q_obj["query"][:55] + ("…" if len(q_obj["query"]) > 55 else "")
            out.append(f"| {qid} | `{preview}` | {', '.join(failures)} |\n")
        out.append("\n")

    # ── Robust queries: passed under all perturbations ──
    robust = [
        qid for qid, perts in sorted(by_qid.items())
        if all(v is True for v in perts.values() if v is not None)
    ]
    out.append(f"## Bulletproof queries (passed all perturbations): {len(robust)}\n\n")
    if robust:
        out.append(f"`{', '.join(robust)}`\n\n")

    # ── Adversarial score stability ──
    out.append("## Adversarial top-1 scores by perturbation\n\n")
    out.append("_Should stay low (< ~0.2) under all perturbations._\n\n")
    adv_qids = sorted({r["query"]["id"] for r in report["results"]["original"]
                       if r["query"].get("must_refuse", False)})
    out.append("| Query ID | " + " | ".join(p for p, _ in PERTURBATIONS) + " |\n")
    out.append("|" + "---|" * (len(PERTURBATIONS) + 1) + "\n")
    for qid in adv_qids:
        cells = [qid]
        for name, _ in PERTURBATIONS:
            r = next(rr for rr in report["results"][name]
                     if rr["query"]["id"] == qid)
            cells.append(f"{r['top1_score']:.3f}" if r["top1_score"] is not None else "—")
        out.append("| " + " | ".join(cells) + " |\n")
    out.append("\n")

    # ── Per-perturbation example queries ──
    out.append("## Sample perturbations\n\n")
    out.append("First few queries shown across all perturbation forms:\n\n")
    sample_qids = ["Q001", "Q017", "V001", "V007"]
    for qid in sample_qids:
        if qid not in {r["query"]["id"] for r in report["results"]["original"]}:
            continue
        out.append(f"**{qid}**:\n")
        for name, _ in PERTURBATIONS:
            r = next(rr for rr in report["results"][name]
                     if rr["query"]["id"] == qid)
            mark = "✅" if r["hit_any"] is True else ("❌" if r["hit_any"] is False else "•")
            out.append(f"  - `{name}` {mark} → `{r['perturbed']}`\n")
        out.append("\n")

    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--no-rerank", action="store_true",
                        help="Skip cross-encoder reranker (much faster, dense-only)")
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    print(f"Running stress test (k={args.k}, rerank={not args.no_rerank})…")

    report = stress_test(k=args.k, rerank=not args.no_rerank)
    md = render_report(report)
    suffix = "stress" if not args.no_rerank else "stress_dense"
    out_path = REPORTS / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_{suffix}.md"
    out_path.write_text(md, encoding="utf-8")

    # Print headline numbers to stdout for the impatient.
    print()
    print("=" * 60)
    print("HEADLINE")
    print("=" * 60)
    base_hits = sum(1 for r in report["results"]["original"] if r["hit_any"] is True)
    base_total = sum(1 for r in report["results"]["original"]
                     if r["hit_any"] is not None and not r["skipped"])
    base_recall = base_hits / base_total if base_total else 0.0
    print(f"Original Recall (any) @{args.k}: {base_recall:.1%} ({base_hits}/{base_total})")
    for name, _ in PERTURBATIONS[1:]:
        hits = sum(1 for r in report["results"][name] if r["hit_any"] is True)
        total = sum(1 for r in report["results"][name]
                    if r["hit_any"] is not None and not r["skipped"])
        recall = hits / total if total else 0.0
        delta = recall - base_recall
        sign = "+" if delta >= 0 else ""
        print(f"  {name:24s} {recall:.1%} ({hits}/{total})  Δ {sign}{delta*100:.1f}")
    print(f"\nReport: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

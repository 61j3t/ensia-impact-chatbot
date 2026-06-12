"""Quality A/B: run the same set of golden_set queries against multiple
LLM backends via the deployed bot's /ask endpoint, and side-by-side
compare answer length, tier band, refusal rate, latency, and which
expected sources got cited.

Hits the HF Space sidecar's /ask with an explicit `model` override per
request, so we measure the WHOLE pipeline (retrieval + RAG + Gemini-or-
Groq generation) end-to-end. Bot's memory writes are bypassed by /ask.

Usage:
    python eval/model_compare.py \\
        --models gemini/gemini-2.5-flash gemini/gemini-2.0-flash \\
                 groq/llama-3.3-70b-versatile \\
        --n 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden_set.json"
DEFAULT_TARGET = "https://61j3t-ensia-impact-bot.hf.space"


def _load_queries(n: int | None) -> list[dict]:
    data = json.loads(GOLDEN.read_text())
    qs = data["queries"]
    # Skip adversarial — testing them separately would need different scoring.
    qs = [q for q in qs if q.get("category") != "adversarial"]
    if n:
        qs = qs[:n]
    return qs


async def _ask(
    client: httpx.AsyncClient,
    target: str,
    token: str | None,
    query: str,
    model: str,
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Sync-Token"] = token
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{target.rstrip('/')}/ask",
            json={"query": query, "model": model},
            headers=headers,
            timeout=180.0,
        )
        elapsed = time.monotonic() - t0
        if r.status_code != 200:
            return {"ok": False, "elapsed": elapsed, "status": r.status_code,
                    "error": r.text[:200], "model": model}
        body = r.json()
        return {
            "ok": True,
            "elapsed": elapsed,
            "status": 200,
            "model": model,
            "model_used": body.get("model_used"),
            "refused": bool(body.get("refused")),
            "tier": body.get("tier"),
            "num_sources": body.get("num_sources") or 0,
            "answer": body.get("answer") or "",
            "ctx_tokens": body.get("context_tokens"),
        }
    except Exception as e:
        return {"ok": False, "elapsed": time.monotonic() - t0,
                "status": -1, "error": f"{type(e).__name__}: {e}", "model": model}


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))]


async def _main(args) -> int:
    queries = _load_queries(args.n)
    print(f"Comparing {len(args.models)} models on {len(queries)} queries\n")
    target = args.target.rstrip("/")
    token = args.token or os.environ.get("SYNC_TOKEN")

    # We run each model SEQUENTIALLY (one model fully done before the
    # next) so a burst on one provider can't accidentally throttle
    # others mid-run.
    all_results: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=30.0)) as client:
        for m in args.models:
            print(f"\n=== {m} ===")
            results = []
            for q in queries:
                r = await _ask(client, target, token, q["query"], m)
                results.append({**r, "query_id": q.get("id"), "query": q["query"]})
                marker = "✓" if r["ok"] and not r.get("refused") else (
                    "↺" if r["ok"] and r.get("refused") else "✗"
                )
                tag = r.get("model_used") or "?"
                print(
                    f"  {marker} {r['elapsed']:5.1f}s  "
                    f"used={tag:35s} {q['query'][:60]}",
                    flush=True,
                )
                # Tiny politeness gap — keeps us under per-model RPM caps.
                await asyncio.sleep(0.4)
            all_results[m] = results

    # ─── side-by-side report ─────────────────────────────────────────
    print("\n" + "=" * 80)
    print("MODEL COMPARISON REPORT")
    print("=" * 80)
    print(
        f"{'Model':<40} {'p50 s':>8} {'p95 s':>8} {'refuse':>8} {'used':>8}"
    )
    print("-" * 80)
    for m, results in all_results.items():
        ok = [r for r in results if r["ok"]]
        lat = [r["elapsed"] for r in ok]
        refused = sum(1 for r in ok if r.get("refused"))
        # `used` = how often the SPECIFIC requested model answered
        # (vs fallback). 100% means the model held up; <100% means we
        # fell back at least once.
        used_self = sum(1 for r in ok if r.get("model_used") == m)
        p50 = _quantile(lat, 0.5)
        p95 = _quantile(lat, 0.95)
        refuse_str = f"{refused}/{len(ok)}"
        used_str = f"{used_self}/{len(ok)}"
        print(f"{m:<40} {p50:>8.2f} {p95:>8.2f} {refuse_str:>8} {used_str:>8}")
    print()

    # Per-query answer-length comparison (rough proxy for verbosity).
    print("Answer length (chars), per query:")
    qids = [q.get("id") or q["query"][:30] for q in queries]
    header = "  " + " ".join(f"{m[:18]:<18}" for m in args.models)
    print(f"  {'query':<30} {header}")
    for i, qid in enumerate(qids):
        row = " ".join(
            f"{len(all_results[m][i].get('answer') or ''):>18d}"
            for m in args.models
        )
        print(f"  {qid:<30} {row}")
    print()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--token", default=None)
    ap.add_argument(
        "--models", nargs="+",
        default=["gemini/gemini-2.5-flash", "groq/llama-3.3-70b-versatile"],
    )
    ap.add_argument("--n", type=int, default=10,
                    help="number of golden queries to run (sequentially)")
    args = ap.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()

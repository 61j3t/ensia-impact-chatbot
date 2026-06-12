"""Concurrent-load test for the deployed bot's answer pipeline.

Hits the sidecar's /ask endpoint with configurable concurrency. Bypasses
Telegram (no cooldown, no reactions) so we can measure how the HF Space
behaves under real load. Queries come from the golden set.

Distinct from `eval/stress_test.py`, which is a quality stress test
(query perturbations against the retriever); this one is a performance
test of the deployed service.

Setup:
    pip install httpx  # already in requirements

Usage:
    # Baseline: one user, twenty queries
    python eval/load_test.py --workers 1 --total 20

    # Concurrent: 5 users, 30 queries between them
    python eval/load_test.py --workers 5 --total 30

    # Sustained: 2 workers for 5 min (will probably trip Groq's RPM)
    python eval/load_test.py --workers 2 --duration 300

Target defaults to the HF Space sidecar; override with --target.
Token defaults to the SYNC_TOKEN env var.
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


def _load_queries(path: Path, n: int | None = None) -> list[str]:
    if not path.exists():
        return [
            "What is the CDE at ENSIA?",
            "How do I register a startup under decree 1275?",
            "Which companies has ENSIA partnered with?",
            "What is the V2V incubator?",
            "Comment soumettre un PFE comme projet startup?",
        ]
    with path.open() as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("queries", [])
    queries = [q["query"] for q in items if "query" in q]
    if n is not None:
        queries = queries[:n]
    return queries


async def _one_request(
    client: httpx.AsyncClient,
    target: str,
    token: str | None,
    query: str,
) -> dict:
    """One request. Never raises — failures land in `status`."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Sync-Token"] = token
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{target.rstrip('/')}/ask",
            json={"query": query},
            headers=headers,
            timeout=180.0,
        )
        elapsed = time.monotonic() - t0
        if r.status_code != 200:
            return {
                "ok": False,
                "elapsed": elapsed,
                "status": r.status_code,
                "error": r.text[:200],
                "query": query,
            }
        body = r.json()
        return {
            "ok": True,
            "elapsed": elapsed,
            "status": 200,
            "tier": body.get("tier"),
            "model_used": body.get("model_used"),
            "refused": body.get("refused"),
            "ctx_tokens": body.get("context_tokens"),
            "num_sources": body.get("num_sources"),
            "timings": body.get("timings") or {},
            "query": query,
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed": time.monotonic() - t0,
            "status": -1,
            "error": f"{type(e).__name__}: {e}",
            "query": query,
        }


async def _worker(
    name: int,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    target: str,
    token: str | None,
    results: list,
) -> None:
    while True:
        try:
            query = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        r = await _one_request(client, target, token, query)
        r["worker"] = name
        results.append(r)
        marker = "✓" if r["ok"] else "✗"
        print(
            f"  w{name:02d} {marker} {r['elapsed']:5.2f}s  "
            f"{(r.get('error') or r['query'])[:80]}",
            flush=True,
        )
        queue.task_done()


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


def _report(results: list[dict], wall: float, workers: int) -> None:
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lat = [r["elapsed"] for r in ok]

    print()
    print("=" * 64)
    print("LOAD-TEST REPORT")
    print("=" * 64)
    print(f"  total requests:   {len(results)}")
    print(f"  succeeded:        {len(ok)}  ({len(ok) / max(len(results), 1) * 100:.0f}%)")
    print(f"  failed:           {len(bad)}")
    print(f"  workers:          {workers}")
    print(f"  wall-clock:       {wall:.1f}s")
    if ok:
        print(f"  throughput:       {len(ok) / wall:.2f} req/s")
    print()

    if lat:
        print("  Latency (s)")
        print(f"    min:    {min(lat):6.2f}")
        print(f"    p50:    {_quantile(lat, 0.50):6.2f}")
        print(f"    p95:    {_quantile(lat, 0.95):6.2f}")
        print(f"    p99:    {_quantile(lat, 0.99):6.2f}")
        print(f"    max:    {max(lat):6.2f}")
        print(f"    mean:   {statistics.mean(lat):6.2f}")
        print()

    # Pipeline-stage breakdown — tells us whether latency degraded in
    # retrieval, the LLM call, or somewhere in between.
    stages = ["rewrite", "retrieval", "answer"]
    timed = [r for r in ok if r.get("timings")]
    if timed:
        print("  Per-stage means (s)")
        for s in stages:
            vals = [t["timings"].get(s, 0.0) for t in timed if t["timings"]]
            print(f"    {s:10s} {statistics.mean(vals):.2f}")
        print()

    # Model breakdown — which member of the fallback chain answered
    # each request? Tells us whether the primary held up or we fell
    # back to Gemini-2.0 / Groq.
    from collections import Counter as _Counter
    models = _Counter(r.get("model_used") or "unknown" for r in ok)
    if models:
        print("  Model used")
        for m, n in models.most_common():
            pct = n / len(ok) * 100
            print(f"    {m:40s} {n:4d}  ({pct:5.1f}%)")
        print()

    # Refusal breakdown — when the bot says "model unavailable" or
    # similar, it's a refusal at our layer (not Telegram).
    refused = sum(1 for r in ok if r.get("refused"))
    if refused:
        print(f"  Soft refusals (incl. all-fallbacks-exhausted): {refused}/{len(ok)}")
        print()

    if bad:
        from collections import Counter
        codes = Counter((r["status"], (r.get("error") or "")[:60]) for r in bad)
        print("  Failures:")
        for (code, err), n in codes.most_common(10):
            print(f"    [{code}] x{n}: {err}")
        print()


async def _main(args) -> int:
    queries_pool = _load_queries(GOLDEN)
    if not queries_pool:
        print("✗ no queries available", file=sys.stderr)
        return 2

    queue: asyncio.Queue = asyncio.Queue()
    if args.duration:
        for i in range(10_000):
            queue.put_nowait(queries_pool[i % len(queries_pool)])
    else:
        for i in range(args.total):
            queue.put_nowait(queries_pool[i % len(queries_pool)])

    results: list[dict] = []
    target = args.target.rstrip("/")
    token = args.token or os.environ.get("SYNC_TOKEN")

    print(f"target:    {target}")
    print(f"workers:   {args.workers}")
    if args.duration:
        print(f"duration:  {args.duration}s")
    else:
        print(f"total:     {args.total}")
    print(f"queries:   {len(queries_pool)} unique (golden_set)")
    print()

    t_start = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(180.0, connect=30.0),
        limits=httpx.Limits(max_connections=args.workers * 2),
    ) as client:
        # First /ask after a Space restart takes ~60s for model load —
        # drain it serially before starting the actual test so the
        # cold-start cost doesn't blow up the latency stats.
        if not args.skip_warmup:
            print("Warmup request (drains model load)…")
            r = await _one_request(client, target, token, queries_pool[0])
            print(f"  warmup: {r['elapsed']:.1f}s  status={r['status']}")
            print()

        print("Running…")
        print()
        workers = [
            asyncio.create_task(
                _worker(i, queue, client, target, token, results)
            )
            for i in range(args.workers)
        ]

        if args.duration:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True),
                    timeout=args.duration,
                )
            except asyncio.TimeoutError:
                for w in workers:
                    w.cancel()
        else:
            await asyncio.gather(*workers, return_exceptions=True)

    wall = time.monotonic() - t_start
    _report(results, wall, args.workers)
    return 0 if all(r["ok"] for r in results) else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help="sidecar base URL")
    ap.add_argument("--token", default=None,
                    help="X-Sync-Token (defaults to env SYNC_TOKEN)")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent workers")
    ap.add_argument("--total", type=int, default=20,
                    help="total requests (ignored if --duration set)")
    ap.add_argument("--duration", type=int, default=None,
                    help="run for N seconds instead of N requests")
    ap.add_argument("--skip-warmup", action="store_true",
                    help="skip the cold-start drain request")
    args = ap.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()

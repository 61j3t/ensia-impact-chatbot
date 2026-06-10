"""Tiny FastAPI app the HF Space exposes on port 7860.

Three endpoints:
    GET  /              — health, used by HF's healthcheck
    POST /sync          — spawn scripts/_run_sync.py detached
    GET  /sync/status   — read the on-disk state file written by the wrapper

The same state-file design used in the local dev dashboard
(`dashboard/lib/sync-state.ts`) — the Vercel-hosted dashboard just
proxies these endpoints via SYNC_BACKEND_URL so the Sync button works
identically in production.

We don't bother with auth: the bot itself contains no sensitive
endpoints, and triggering a pipeline run is idempotent + cheap. If you
need to lock this down, add a shared-token check via a header.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.environ.get("SYNC_STATE_FILE", "/tmp/ensia_sync.state.json"))
# Shared secret the dashboard sends in X-Sync-Token. When unset (e.g. local
# dev) we don't gate /sync — it's only the HF Space deployment that
# needs the lock.
SYNC_TOKEN = os.environ.get("SYNC_TOKEN")


def _require_token(token: str | None) -> None:
    if not SYNC_TOKEN:
        return  # auth disabled
    # Constant-time compare to avoid a timing oracle.
    a = (token or "").encode()
    b = SYNC_TOKEN.encode()
    if len(a) != len(b):
        raise HTTPException(status_code=401, detail="invalid token")
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    if diff != 0:
        raise HTTPException(status_code=401, detail="invalid token")

app = FastAPI(title="ENSIA Impact bot sidecar")

# Vercel domain isn't known at build time, so we open CORS to anything.
# It's only HF Space → dashboard reading state; nothing privileged is
# returned.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {"running": False}
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        return {"running": False}
    # If the file says running but the PID is gone (e.g. crash without
    # finally{}), downgrade to failed so the UI doesn't get stuck.
    if s.get("running") and s.get("pid"):
        try:
            os.kill(s["pid"], 0)
        except Exception:
            s["running"] = False
            s.setdefault("exitCode", -1)
            s["error"] = s.get("error") or "process exited without recording an exit code"
            s.setdefault("endedAt", datetime.now(timezone.utc).isoformat())
    return s


@app.get("/")
async def health() -> dict:
    """Simple health probe used by HF's container monitor."""
    return {"ok": True, "service": "ensia-impact-bot/sidecar"}


# ── /ask: stress-test endpoint ─────────────────────────────────────────
#
# Runs the bot's answer pipeline against an arbitrary query without
# going through Telegram (no cooldown, no reactions, no bot-user
# upserts). Used by eval/stress_test.py.
#
# Lazy-loaded: the FIRST request after a container start takes ~60s
# (BGE-M3 + reranker load on CPU). Subsequent calls are ~3-5s on
# cpu-basic. Models stay resident, adding ~4.6 GB to the container's
# RAM usage. Token-protected like every other sidecar endpoint.

_RETRIEVER = None
_RETRIEVER_LOCK = None


def _get_retriever():
    """Lazy singleton — instantiated on first /ask call."""
    global _RETRIEVER
    if _RETRIEVER is None:
        from chatbot.retrieve import Retriever
        _RETRIEVER = Retriever()
        # Warm up the lazy embedder/reranker so the first real query
        # doesn't pay the full cold-start cost.
        _RETRIEVER.search("hello", k=1, rerank=True)
    return _RETRIEVER


@app.post("/ask")
async def ask(
    payload: dict = Body(...),
    x_sync_token: str | None = Header(default=None),
) -> dict:
    """Run the bot's answer pipeline on an arbitrary query.

    Body: `{"query": "...", "rerank": true (optional)}`.

    Returns the answer text, sources, timings, and context-fill metric.
    Bypasses memory writes — the stress test shouldn't pollute Neon
    with synthetic data."""
    _require_token(x_sync_token)
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="missing query")

    import asyncio
    from chatbot.answer import answer

    retriever = await asyncio.to_thread(_get_retriever)
    result = await asyncio.to_thread(answer, query, retriever=retriever, history=[])

    return {
        "answer": result["answer"],
        "refused": result.get("refused"),
        "tier": result.get("tier"),
        "top_score": result.get("top_score"),
        "context_tokens": result.get("context_tokens"),
        "context_max": result.get("context_max"),
        "num_sources": len(result.get("sources") or []),
        "timings": result.get("timings"),
    }


@app.get("/sync/status")
async def sync_status(x_sync_token: str | None = Header(default=None)) -> dict:
    _require_token(x_sync_token)
    return _read_state()


@app.get("/snapshot")
async def snapshot(x_sync_token: str | None = Header(default=None)) -> dict:
    """Expose `data/_status.json` so the Vercel dashboard can read corpus
    stats / freshness / index breakdown without bundling the file. Stage
    8 of the pipeline writes this file."""
    _require_token(x_sync_token)
    snap = ROOT / "data" / "_status.json"
    if not snap.exists():
        raise HTTPException(status_code=404, detail="snapshot not generated yet")
    return json.loads(snap.read_text())


@app.post("/sync")
async def sync_start(x_sync_token: str | None = Header(default=None)) -> dict:
    """Kick off the pipeline as a detached child, return immediately."""
    _require_token(x_sync_token)
    current = _read_state()
    if current.get("running"):
        raise HTTPException(
            status_code=409,
            detail={"error": "a sync is already running", "state": current},
        )

    # Spawn the same wrapper used by the local dashboard. Detached +
    # close fds so the HTTP response can return without holding the
    # subprocess open.
    proc = subprocess.Popen(
        [sys.executable, "scripts/_run_sync.py"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env={
            **os.environ,
            "HF_HUB_OFFLINE": "0",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(ROOT),
        },
    )
    return {"ok": True, "pid": proc.pid}

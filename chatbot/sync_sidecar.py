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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.environ.get("SYNC_STATE_FILE", "/tmp/ensia_sync.state.json"))

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


@app.get("/sync/status")
async def sync_status() -> dict:
    return _read_state()


@app.post("/sync")
async def sync_start() -> dict:
    """Kick off the pipeline as a detached child, return immediately."""
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

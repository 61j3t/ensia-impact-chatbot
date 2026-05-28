"""Robust wrapper around `scripts/run_pipeline.sh` that owns its own
on-disk state file (`/tmp/ensia_sync.state.json`) so the dashboard
doesn't have to track the run via in-memory state — which breaks every
time Next.js hot-reloads in dev.

Spawned by `app/api/sync/route.ts` as a detached, unref'd process. The
dashboard just polls the state file via `/api/sync/status` and reads
`{running, currentStage, lastLine, exitCode}`.

The pipeline output is also tee'd to `/tmp/ensia_sync.log` for human
debugging — not surfaced in the UI.

Run manually if you want:
    .venv/bin/python scripts/_run_sync.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/tmp/ensia_sync.state.json")
LOG = Path("/tmp/ensia_sync.log")
STAGE_RE = re.compile(r"^▶\s+(\d+)/\d+\s")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_state(**updates) -> None:
    """Atomic-ish JSON update. Reads-modifies-writes the state file
    using a temp file + rename so a reader can never see a half-written
    payload."""
    payload: dict = {}
    if STATE.exists():
        try:
            payload = json.loads(STATE.read_text())
        except Exception:
            payload = {}
    payload.update(updates)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(STATE)


def main() -> int:
    write_state(
        running=True,
        startedAt=_now(),
        pid=os.getpid(),
        currentStage=None,
        lastLine="",
        exitCode=None,
        endedAt=None,
    )
    # Truncate the log on each fresh run.
    LOG.write_text("")

    env = {
        **os.environ,
        # HF Hub "check for updates" has flaked locally; weights are cached.
        "HF_HUB_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": ".",
    }

    proc = subprocess.Popen(
        ["bash", "scripts/run_pipeline.sh"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    exit_code = 1
    try:
        assert proc.stdout is not None
        with LOG.open("a", buffering=1) as logf:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                logf.write(line + "\n")
                updates = {"lastLine": line}
                m = STAGE_RE.match(line)
                if m:
                    updates["currentStage"] = int(m.group(1))
                write_state(**updates)
        exit_code = proc.wait()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        write_state(
            running=False,
            exitCode=1,
            endedAt=_now(),
            error=str(e),
        )
        return 1
    write_state(
        running=False,
        exitCode=exit_code,
        endedAt=_now(),
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

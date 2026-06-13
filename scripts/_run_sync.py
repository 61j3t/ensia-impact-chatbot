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

    # ── HF Space persistence shim ───────────────────────────────────────
    # The HF Space filesystem is ephemeral: every container restart wipes
    # whatever the pipeline wrote. To make sync results actually stick we
    # commit + push the data deltas back to the Space's own git repo.
    # The next time the Space starts (immediately, since pushing main
    # triggers a rebuild), the fresh container clones the updated repo.
    #
    # Gated on HF_WRITE_TOKEN — set only on the HF Space deployment. On a
    # local laptop the variable is unset and this is a no-op, so local
    # runs don't accidentally git-push anything.
    if exit_code == 0 and os.environ.get("HF_WRITE_TOKEN"):
        try:
            _hf_commit_back()
        except Exception:
            # Don't fail the whole sync just because we couldn't push —
            # the bot already has the updated data in its container; the
            # data only disappears on the next restart.
            import traceback
            err = traceback.format_exc()
            write_state(error=f"git push failed (sync data is OK in-memory): {err[:300]}")
    return exit_code


def _hf_commit_back() -> None:
    """Commit the pipeline's data deltas + push to the HF Space repo.

    Only the directories that the pipeline writes are staged — code
    files are never touched here, even if the workspace has uncommitted
    edits from some operator poking around.
    """
    import shlex

    token = os.environ["HF_WRITE_TOKEN"]
    repo = os.environ.get("HF_SPACE_REPO", "61j3t/ensia-impact-bot")
    remote = f"https://61j3t:{token}@huggingface.co/spaces/{repo}"

    def run(*cmd: str) -> tuple[int, str]:
        p = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    # `.git` was created by Docker's build process (root) but the
    # runtime user is `user` (UID 1000), so git's "dubious ownership"
    # check rejects every operation. Mark /app as safe before doing
    # anything else.
    run("git", "config", "--global", "--add", "safe.directory", str(ROOT))

    # Identify ourselves so the commits don't show up as "unknown".
    run("git", "config", "user.email", "sync@ensia-impact-bot")
    run("git", "config", "user.name", "ensia-bot-sync")

    # Stage only the data the pipeline produces. Adding the whole repo
    # would also stage `data/.telethon.session` and other accidents.
    paths = [
        "data/result.json",
        "data/messages_enriched.json",
        "data/external_text/",
        "data/extracted_text/",
        "data/ocr_text/",
        "data/_status.json",
        "chatbot/chroma_db/",
    ]
    for p in paths:
        run("git", "add", "-A", "--", p)

    # Bail cleanly if there's nothing to commit (e.g. a no-op re-sync).
    code, _out = run("git", "diff", "--cached", "--quiet")
    if code == 0:
        return  # no changes staged

    msg = f"sync: pipeline run @ {_now()}"
    code, out = run("git", "commit", "-m", msg)
    if code != 0:
        raise RuntimeError(f"commit failed: {out[:500]}")

    # Push via a credential header injected per-command. Passing the
    # credentialed URL as a positional arg to `git push` triggers git's
    # credential-helper lookup ("fatal: unable to get credentials") on
    # the HF container because there's no askpass. The cleaner pattern
    # is to leave the remote URL clean and inject auth via an extra
    # HTTP header for this one invocation.
    #
    # GIT_TERMINAL_PROMPT=0 prevents any interactive fallback.
    push_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    import base64
    basic = base64.b64encode(f"61j3t:{token}".encode()).decode()
    push_args = [
        "git",
        "-c", f"http.extraheader=Authorization: Basic {basic}",
        "push",
        f"https://huggingface.co/spaces/{repo}",
        "HEAD:main",
    ]
    p = subprocess.run(push_args, cwd=str(ROOT), capture_output=True,
                       text=True, timeout=180, env=push_env)
    if p.returncode != 0:
        out_combined = (p.stdout or "") + (p.stderr or "")
        raise RuntimeError(f"push failed: {out_combined[:500]}")


if __name__ == "__main__":
    sys.exit(main())

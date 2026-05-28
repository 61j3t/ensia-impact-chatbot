#!/usr/bin/env bash
# Hugging Face Space entrypoint. Starts (in this order):
#   1. Decode the optional Telethon session secret onto disk
#   2. The sync sidecar (FastAPI on :7860) — required because HF Spaces
#      health-check that port
#   3. The Telegram bot in the background
#
# We don't `exec` the bot directly because HF needs port 7860 alive even
# while the bot is loading models or in a polling slow-spot.

set -euo pipefail

# ── Restore Telethon session from the HF secret ──────────────────────────
# Uploaded as TELETHON_SESSION_B64 — a base64-encoded copy of the
# `data/.telethon.session` SQLite file produced by the first interactive
# login locally. Decode + drop it into place if present.
mkdir -p /tmp/data
if [[ -n "${TELETHON_SESSION_B64:-}" ]]; then
    echo "▶ restoring Telethon session from HF secret"
    echo "$TELETHON_SESSION_B64" | base64 -d > /tmp/data/.telethon.session
    # The bot's session loader reads from data/.telethon.session relative
    # to the repo root; symlink so we don't have to touch the code.
    ln -sf /tmp/data/.telethon.session /app/data/.telethon.session
fi

# ── Sidecar HTTP server ──────────────────────────────────────────────────
# /              health
# /sync          POST → spawn detached pipeline
# /sync/status   GET  → on-disk state
# Wired into the Vercel dashboard's Sync button via SYNC_BACKEND_URL.
echo "▶ starting sync sidecar on :7860"
uvicorn chatbot.sync_sidecar:app --host 0.0.0.0 --port 7860 &
SIDECAR_PID=$!

# Give the sidecar a moment to bind 7860 before the bot starts pulling
# heavy models into RAM.
sleep 1

echo "▶ starting Telegram bot (Telethon / MTProto)"
python -m chatbot.telegram_bot_telethon &
BOT_PID=$!

# Shut everything down on container stop.
trap 'kill $SIDECAR_PID $BOT_PID 2>/dev/null || true' SIGTERM SIGINT
wait -n $SIDECAR_PID $BOT_PID
EXIT=$?
echo "▶ a process exited with $EXIT; tearing down"
kill $SIDECAR_PID $BOT_PID 2>/dev/null || true
exit $EXIT

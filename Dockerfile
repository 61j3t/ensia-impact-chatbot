# ─── ENSIA Impact bot — Hugging Face Space (Docker SDK) ──────────────────
#
# Single-container deploy of the Telegram bot plus a tiny FastAPI sidecar
# that exposes /sync and /sync/status so the Vercel dashboard can drive
# the same pipeline that used to live in `dashboard/app/api/sync`.
#
# HF Spaces require:
#   - The container listens on port 7860.
#   - The image's filesystem is ephemeral; persist via committed code /
#     git LFS / paid Persistent Storage.
#
# All secrets come from HF Space secrets (env vars), never baked in.

FROM python:3.13-slim

# ── system deps ─────────────────────────────────────────────────────────
# tesseract + lang packs for OCR; playwright runtime libs for chromium;
# build-essential for any wheels that need to compile (psycopg, chromadb).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        tesseract-ocr \
        tesseract-ocr-ara \
        tesseract-ocr-fra \
        tesseract-ocr-eng \
        # playwright chromium runtime deps
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxext6 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# HF spaces run as a non-root user with UID 1000; everything under /app
# needs to be writable by it (state files at runtime, etc.).
RUN useradd -m -u 1000 user
WORKDIR /app

# ── python deps ─────────────────────────────────────────────────────────
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir fastapi 'uvicorn[standard]'

# Playwright Chromium browser — separate step so its cache layer is reused.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/user/.cache/ms-playwright
RUN mkdir -p ${PLAYWRIGHT_BROWSERS_PATH} && chown -R user:user /home/user/.cache && \
    su -s /bin/bash user -c "python -m playwright install chromium"

# ── HF cache + state dirs ───────────────────────────────────────────────
# Sentence-Transformers downloads BGE-M3 weights to HF_HOME on first run
# (~2.3 GB). Pre-pulling them at build time would balloon the image and
# fail HF Space's 50 GB image cap, so we let it happen on first start
# instead — first message after a cold restart takes ~30s longer.
ENV HF_HOME=/home/user/.cache/huggingface
ENV TRANSFORMERS_CACHE=/home/user/.cache/huggingface
ENV HF_HUB_OFFLINE=0
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Where the bot will look for its on-disk sync state (the /tmp default
# is fine on Linux containers).
ENV SYNC_STATE_FILE=/tmp/ensia_sync.state.json
ENV SYNC_LOG_FILE=/tmp/ensia_sync.log

# ── application code ────────────────────────────────────────────────────
COPY --chown=user:user . .

# HF Space port.
EXPOSE 7860

USER user
ENTRYPOINT ["bash", "scripts/_space_entrypoint.sh"]

# Build separate images for web and worker.
#
#   --target web    … a lightweight image with neither the Whisper dependencies nor the model weights
#   --target worker … an image that includes stable-ts / torch and the model weights
#
# web does not do forced alignment of subtitles. align_subtitles.py runs that in a
# separate process, and that script is the only thing that imports stable_whisper,
# so the web side needs neither torch nor the model weights. Having the public
# service pull an image of several GB on every cold start is very wasteful.
# The system dependencies and the common Python dependencies are shared in the
# base stage, to avoid maintaining them twice.

FROM python:3.14-slim AS base

# System dependencies: ffmpeg + CJK fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install shared Python dependencies (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user (PoLP); -m creates /home/appuser so libraries that write to $HOME
# (e.g. torch/huggingface cache fallback, Cloud Run gen2 control socket) don't fail
# with "Permission denied: '/home/appuser'" on every boot.
RUN useradd -r -m -u 1001 appuser

ENV PORT=8080
EXPOSE 8080


# --- web ---------------------------------------------------------------------
# It has no model weights. The gunicorn timeout matches the 300 seconds on the Cloud Run side.

FROM base AS web

# For why chown -R is not used, see the ★ in the worker stage.
COPY --chown=appuser:appuser . .
USER appuser

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "300", "app.main:app"]


# --- worker ------------------------------------------------------------------
# Pre-download Whisper model into /app/.cache (XDG_CACHE_HOME) so it survives user switch.
# Avoid load_model here because large-v3 can exhaust Cloud Build memory while loading weights.
#
# The model download is placed before COPY . . so that a code-only change does
# not invalidate this heavy layer.

FROM base AS worker

# The heavy Whisper dependencies are installed only here. They are not included in the web image.
COPY requirements-worker.txt .
RUN pip install --no-cache-dir -r requirements-worker.txt

ARG WHISPER_MODEL=large-v3
ENV XDG_CACHE_HOME=/app/.cache

# ★ Have appuser itself download the model into a directory owned by appuser.
#   If root downloads it and chown -R is run afterwards, overlayfs copies a file
#   up to the upper layer on every ownership change, so the whole 2.84GB model is
#   stacked a second time (the image went from 6GB to 8.9GB, and the same amount
#   was added on every deploy).
#   Do not change this back to chown -R.
RUN install -d -o appuser -g appuser /app/.cache
USER appuser
RUN python3 -c "import os, whisper; whisper._download(whisper._MODELS['${WHISPER_MODEL}'], os.path.join(os.environ['XDG_CACHE_HOME'], 'whisper'), False)"

COPY --chown=appuser:appuser . .

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "1800", "app.main:app"]

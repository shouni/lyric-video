FROM python:3.11-slim

# System dependencies: ffmpeg + CJK fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Whisper model into /app/.cache (XDG_CACHE_HOME) so it survives user switch.
# Avoid load_model here because large-v3 can exhaust Cloud Build memory while loading weights.
ARG WHISPER_MODEL=large-v3
ENV XDG_CACHE_HOME=/app/.cache
RUN python3 -c "import os, whisper; whisper._download(whisper._MODELS['${WHISPER_MODEL}'], os.path.join(os.environ['XDG_CACHE_HOME'], 'whisper'), False)"

# Copy application code after model download so code-only changes do not invalidate the model cache layer.
COPY . .

# Non-root user (PoLP); -m creates /home/appuser so libraries that write to $HOME
# (e.g. torch/huggingface cache fallback, Cloud Run gen2 control socket) don't fail
# with "Permission denied: '/home/appuser'" on every boot.
RUN useradd -r -m -u 1001 appuser && chown -R appuser /app
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "1800", "app.main:app"]

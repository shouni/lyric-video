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

# Copy application code
COPY . .

# Pre-download Whisper model into /app/.cache (XDG_CACHE_HOME) so it survives user switch
ARG WHISPER_MODEL=large-v3
ENV XDG_CACHE_HOME=/app/.cache
RUN python3 -c "import whisper; whisper.load_model('${WHISPER_MODEL}')"

# Non-root user (PoLP); chown after model download so cache is accessible
RUN useradd -r -u 1001 appuser && chown -R appuser /app
USER appuser

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "1800", "app.main:app"]

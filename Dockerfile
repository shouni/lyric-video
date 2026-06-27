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

# Optional: pre-download Whisper model to warm up cold starts.
# Adds ~3 GB to image. Uncomment if you prefer fast task start over small image.
# ARG WHISPER_MODEL=large-v3
# RUN python3 -c "import whisper; whisper.load_model('large-v3')"

ENV PORT=8080
EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

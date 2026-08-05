# web と worker で別のイメージを作る。
#
#   --target web    … Whisper 系の依存もモデル重みも含まない軽量イメージ
#   --target worker … stable-ts / torch とモデル重みを含むイメージ
#
# web は字幕の強制アライメントを行わない。その処理は align_subtitles.py が
# 別プロセスで実行し、stable_whisper を import するのはそのスクリプトだけなので、
# web 側は torch もモデル重みも必要としない。公開サービスがコールドスタートの
# たびに数 GB のイメージを引くのは無駄が大きい。
# システム依存と共通の Python 依存は base ステージで共有し、二重管理を避ける。

FROM python:3.11-slim AS base

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
# モデル重みを持たない。gunicorn の timeout は Cloud Run 側の 300 秒に合わせる。

FROM base AS web

COPY . .
RUN chown -R appuser /app
USER appuser

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "300", "app.main:app"]


# --- worker ------------------------------------------------------------------
# Pre-download Whisper model into /app/.cache (XDG_CACHE_HOME) so it survives user switch.
# Avoid load_model here because large-v3 can exhaust Cloud Build memory while loading weights.
#
# モデル取得を COPY . . より前に置くのは、コードだけの変更でこの重いレイヤーを
# 無効化しないため。

FROM base AS worker

# Whisper 系の重い依存はここでだけ入れる。web のイメージには含めない。
COPY requirements-worker.txt .
RUN pip install --no-cache-dir -r requirements-worker.txt

ARG WHISPER_MODEL=large-v3
ENV XDG_CACHE_HOME=/app/.cache
RUN python3 -c "import os, whisper; whisper._download(whisper._MODELS['${WHISPER_MODEL}'], os.path.join(os.environ['XDG_CACHE_HOME'], 'whisper'), False)"

COPY . .
RUN chown -R appuser /app
USER appuser

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "1800", "app.main:app"]

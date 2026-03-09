# video2text Dockerfile (v0.2.1)
# Supports NVIDIA GPU acceleration with cu121 (Tesla P4 / Pascal / Volta compatible)
# https://github.com/your-repo/video2text

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface \
    MODELSCOPE_CACHE=/app/.cache/modelscope \
    TORCH_HOME=/app/.cache/torch \
    GRADIO_ANALYTICS_ENABLED=false \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY utils ./utils
COPY fastapi_app.py main.py main.sh .env.example ./

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install \
        --index-url https://download.pytorch.org/whl/cu121 \
        torch==2.3.1 \
        torchaudio==2.3.1 \
    && python -m pip install -e .

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/workspace /app/.cache/huggingface /app/.cache/modelscope /app/.cache/torch

EXPOSE 7881

ENTRYPOINT ["/app/docker-entrypoint.sh"]

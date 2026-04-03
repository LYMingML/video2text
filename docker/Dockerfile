# video2text GPU Dockerfile - Ultra Optimized v0.2.3
# 基于 PyTorch 官方 runtime 镜像，单阶段构建
# 预估大小：~6-7GB

FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface \
    HF_ENDPOINT=https://hf-mirror.com \
    MODELSCOPE_CACHE=/app/.cache/modelscope \
    TORCH_HOME=/app/.cache/torch \
    GRADIO_ANALYTICS_ENABLED=false \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video

WORKDIR /app

# 1. 安装系统依赖（变化极少，缓存命中率最高）
RUN sed -i "s/archive.ubuntu.com/mirrors.tuna.tsinghua.edu.cn/g" /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc git ffmpeg \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/*

# 2. 创建缓存目录
RUN mkdir -p /app/.cache/huggingface /app/.cache/modelscope /app/.cache/torch /app/workspace

# 3. Python 依赖安装（COPY pyproject.toml + src，pip install 需要这些包）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple . && \
    find /opt/conda -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /root/.cache/pip /tmp/*

# 4. 下载 paraformer-zh 核心模型（~1GB，依赖 pip install 的 funasr/modelscope）
COPY scripts/download_models.py /tmp/
RUN python /tmp/download_models.py && rm -rf /tmp/download_models.py

# 5. 应用代码（变更最频繁，放在最后以最大化缓存利用）
COPY fastapi_app.py main.py .env.example ./
COPY scripts/main.sh /app/main.sh
COPY scripts/video2text.service /app/video2text.service
COPY docker/docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh /app/main.sh

EXPOSE 7881

ENTRYPOINT ["/app/docker-entrypoint.sh"]

#!/usr/bin/env bash
# video2text - 构建并推送到 Docker Hub
set -euo pipefail

USERNAME="${DOCKER_HUB_USER:-lym}"
VERSION="${1:-0.2.1}"

echo "=========================================="
echo "  video2text Docker 镜像构建与推送"
echo "  用户名: $USERNAME"
echo "  版本: $VERSION"
echo "=========================================="

# 检查登录状态
echo ""
echo "[1/5] 检查 Docker Hub 登录状态..."
if ! docker login --username "$USERNAME" 2>/dev/null; then
    echo "请先登录 Docker Hub:"
    echo "  docker login"
    exit 1
fi

# 构建 CPU 版
echo ""
echo "[2/5] 构建 CPU 版镜像..."
docker build -f Dockerfile.cpu \
    -t "$USERNAME/video2text:cpu" \
    -t "$USERNAME/video2text:$VERSION-cpu" \
    -t "$USERNAME/video2text:latest" \
    .

# 构建 GPU 版
echo ""
echo "[3/5] 构建 GPU 版镜像..."
docker build -f Dockerfile \
    -t "$USERNAME/video2text:cu121" \
    -t "$USERNAME/video2text:$VERSION-cu121" \
    .

# 推送镜像
echo ""
echo "[4/5] 推送镜像到 Docker Hub..."
docker push "$USERNAME/video2text:cpu"
docker push "$USERNAME/video2text:$VERSION-cpu"
docker push "$USERNAME/video2text:cu121"
docker push "$USERNAME/video2text:$VERSION-cu121"
docker push "$USERNAME/video2text:latest"

echo ""
echo "[5/5] 完成!"
echo ""
echo "镜像已推送:"
echo "  - $USERNAME/video2text:cpu"
echo "  - $USERNAME/video2text:cu121"
echo "  - $USERNAME/video2text:latest"
echo ""
echo "拉取命令:"
echo "  docker pull $USERNAME/video2text:cpu      # CPU 版"
echo "  docker pull $USERNAME/video2text:cu121    # GPU 版"

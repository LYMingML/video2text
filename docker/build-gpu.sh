#!/usr/bin/env bash
# video2text - 构建 GPU 版 Docker 镜像
set -euo pipefail

USERNAME="${DOCKER_HUB_USER:-adolyming}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "构建 video2text GPU 镜像..."
cd "$PROJECT_ROOT"
docker build -f docker/Dockerfile -t "$USERNAME/video2text:latest" "$PROJECT_ROOT"

echo ""
echo "完成! 推送命令:"
echo "  docker login"
echo "  docker push $USERNAME/video2text:latest"

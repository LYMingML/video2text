#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/app"
cd "$PROJECT_DIR"

read_env_value() {
  local key="$1"
  local env_file="$PROJECT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi
  grep -E "^${key}=" "$env_file" | tail -n 1 | cut -d'=' -f2-
}

HOST="${HOST:-0.0.0.0}"
ENV_PORT="$(read_env_value APP_PORT || true)"
PORT="${PORT:-${APP_PORT:-${ENV_PORT:-7881}}}"
CERT_FILE="${CERT_FILE:-/app/video2text.pem}"
KEY_FILE="${KEY_FILE:-/app/video2text-key.pem}"
SSL_MODE="${SSL_MODE:-auto}"

mkdir -p /app/workspace /app/.cache/huggingface /app/.cache/modelscope /app/.cache/torch

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "[GPU] 检测到 NVIDIA GPU："
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || true
elif [[ -e /dev/nvidiactl || -d /proc/driver/nvidia ]]; then
  echo "[GPU] 检测到 NVIDIA 设备节点，但 nvidia-smi 不可用"
else
  echo "[GPU] 未检测到 NVIDIA GPU，容器将按 CPU 环境运行"
fi

launch_http() {
  echo "[RUN] Docker HTTP 模式: http://${HOST}:${PORT}"
  exec python fastapi_app.py --host "$HOST" --port "$PORT"
}

launch_https() {
  echo "[RUN] Docker HTTPS 模式: https://${HOST}:${PORT}"
  exec python fastapi_app.py \
    --host "$HOST" \
    --port "$PORT" \
    --ssl-certfile "$CERT_FILE" \
    --ssl-keyfile "$KEY_FILE"
}

case "$SSL_MODE" in
  https)
    if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
      echo "[ERROR] HTTPS 证书不存在: $CERT_FILE / $KEY_FILE"
      exit 1
    fi
    launch_https
    ;;
  http)
    launch_http
    ;;
  auto)
    if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
      launch_https
    else
      launch_http
    fi
    ;;
  *)
    echo "[ERROR] SSL_MODE 仅支持 auto|http|https，当前为: $SSL_MODE"
    exit 1
    ;;
esac


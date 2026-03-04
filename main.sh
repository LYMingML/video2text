#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-7880}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
CERT_FILE="${CERT_FILE:-video2text.pem}"
KEY_FILE="${KEY_FILE:-video2text-key.pem}"
MODE="${1:-auto}"  # auto | http | https

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[ERROR] Python 不存在或不可执行: $PYTHON_BIN"
  echo "请先创建虚拟环境并安装依赖。"
  exit 1
fi

echo "[STEP] 停止旧进程..."
pkill -f "main.py" 2>/dev/null || true

NO_PROXY_LIST="127.0.0.1,localhost,0.0.0.0,192.168.1.2"
export NO_PROXY="${NO_PROXY:-$NO_PROXY_LIST}"
export no_proxy="${no_proxy:-$NO_PROXY}"
export GRADIO_ANALYTICS_ENABLED="${GRADIO_ANALYTICS_ENABLED:-False}"

launch_http() {
  echo "[RUN] HTTP 模式: http://$HOST:$PORT"
  exec "$PYTHON_BIN" main.py --host "$HOST" --port "$PORT"
}

launch_https() {
  echo "[RUN] HTTPS 模式: https://$HOST:$PORT"
  exec "$PYTHON_BIN" main.py \
    --host "$HOST" \
    --port "$PORT" \
    --ssl-certfile "$CERT_FILE" \
    --ssl-keyfile "$KEY_FILE"
}

case "$MODE" in
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
    echo "用法: ./main.sh [auto|http|https]"
    exit 1
    ;;
esac

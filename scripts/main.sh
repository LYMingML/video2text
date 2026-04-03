#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PORT="${PORT:-7881}"
HTTP_PORT="${HTTP_PORT:-7880}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
CERT_FILE="$PROJECT_ROOT/certs/video2text.pem"
KEY_FILE="$PROJECT_ROOT/certs/video2text-key.pem"

# 杀掉旧进程
pkill -f "fastapi_app.py" 2>/dev/null || true
sleep 0.3

# 环境变量
export GRADIO_ANALYTICS_ENABLED=False
export NO_PROXY="127.0.0.1,localhost,0.0.0.0"
export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT"

cleanup() {
    echo "停止服务..."
    pkill -f "fastapi_app.py" 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

# 同时启动 HTTP 和 HTTPS
if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    echo "启动 HTTP: http://0.0.0.0:$HTTP_PORT"
    "$PYTHON_BIN" "$PROJECT_ROOT/fastapi_app.py" --host 0.0.0.0 --port "$HTTP_PORT" &
    HTTP_PID=$!

    echo "启动 HTTPS: https://0.0.0.0:$PORT"
    "$PYTHON_BIN" "$PROJECT_ROOT/fastapi_app.py" --host 0.0.0.0 --port "$PORT" \
        --ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE" &
    HTTPS_PID=$!

    wait $HTTPS_PID $HTTP_PID
else
    echo "启动 HTTP 模式: http://0.0.0.0:$PORT"
    exec "$PYTHON_BIN" "$PROJECT_ROOT/fastapi_app.py" --host 0.0.0.0 --port "$PORT"
fi

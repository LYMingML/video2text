#!/usr/bin/env bash
cd "$(dirname "$0")"
PORT="${PORT:-7881}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
CERT_FILE="video2text.pem"
KEY_FILE="video2text-key.pem"

# 杀掉旧进程
pkill -f "fastapi_app.py --port $PORT" 2>/dev/null || true
sleep 0.3

# 环境变量
export GRADIO_ANALYTICS_ENABLED=False
export NO_PROXY="127.0.0.1,localhost,0.0.0.0"

# 检查证书，自动选择 HTTP/HTTPS
if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    echo "启动 HTTPS 模式: https://0.0.0.0:$PORT"
    exec "$PYTHON_BIN" fastapi_app.py --host 0.0.0.0 --port "$PORT" \
        --ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE"
else
    echo "启动 HTTP 模式: http://0.0.0.0:$PORT"
    exec "$PYTHON_BIN" fastapi_app.py --host 0.0.0.0 --port "$PORT"
fi

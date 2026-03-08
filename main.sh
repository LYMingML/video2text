#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
PORT="${PORT:-${ENV_PORT:-7881}}"
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

kill_pid_gracefully() {
  local pid="$1"
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  echo "[KILL] 发送 TERM 到 PID=$pid"
  kill "$pid" 2>/dev/null || true

  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done

  echo "[KILL] PID=$pid 未在超时内退出，发送 KILL"
  kill -9 "$pid" 2>/dev/null || true
}

collect_port_pids() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null \
      | awk -v p=":$port" '$4 ~ p {print $NF}' \
      | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' \
      | sort -u
    return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
    return 0
  fi
  return 0
}

cleanup_old_processes() {
  local port="$1"
  local killed_any=0

  # 1) 先清理占用端口的进程（最精准）
  mapfile -t port_pids < <(collect_port_pids "$port")
  if [[ ${#port_pids[@]} -gt 0 ]]; then
    echo "[KILL] 检测到端口 $port 被占用，PID: ${port_pids[*]}"
    for pid in "${port_pids[@]}"; do
      kill_pid_gracefully "$pid"
      killed_any=1
    done
  fi

  # 2) 再按命令行兜底清理当前项目进程
  mapfile -t main_pids < <(pgrep -f "${PROJECT_DIR}/main.py|${PROJECT_DIR}/fastapi_app.py|fastapi_app.py --host|fastapi_app.py --port" 2>/dev/null || true)
  if [[ ${#main_pids[@]} -gt 0 ]]; then
    echo "[KILL] 兜底清理 main.py 进程，PID: ${main_pids[*]}"
    for pid in "${main_pids[@]}"; do
      kill_pid_gracefully "$pid"
      killed_any=1
    done
  fi

  # 3) 最终确认端口释放
  mapfile -t remain_pids < <(collect_port_pids "$port")
  if [[ ${#remain_pids[@]} -gt 0 ]]; then
    echo "[WARN] 端口 $port 仍被占用，PID: ${remain_pids[*]}"
  elif [[ "$killed_any" -eq 1 ]]; then
    echo "[STEP] 旧进程已清理，端口 $port 已释放"
  else
    echo "[STEP] 未检测到旧进程"
  fi
}

cleanup_old_processes "$PORT"

NO_PROXY_LIST="127.0.0.1,localhost,0.0.0.0,192.168.1.2"
export NO_PROXY="${NO_PROXY:-$NO_PROXY_LIST}"
export no_proxy="${no_proxy:-$NO_PROXY}"
export GRADIO_ANALYTICS_ENABLED="${GRADIO_ANALYTICS_ENABLED:-False}"

launch_http() {
  echo "[RUN] HTTP 模式: http://$HOST:$PORT"
  exec "$PYTHON_BIN" fastapi_app.py --host "$HOST" --port "$PORT" < /dev/null
}

launch_https() {
  echo "[RUN] HTTPS 模式: https://$HOST:$PORT"
  exec "$PYTHON_BIN" fastapi_app.py \
    --host "$HOST" \
    --port "$PORT" \
    --ssl-certfile "$CERT_FILE" \
    --ssl-keyfile "$KEY_FILE" \
    < /dev/null
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

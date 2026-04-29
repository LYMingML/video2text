#!/usr/bin/env bash
# ==============================================================================
# video2text — 启动脚本 v0.5.1
# ==============================================================================
#
# 功能：
#   自动检测 SSL 证书，启动 HTTP + HTTPS 双端口服务
#   同一进程共享 workspace / Pipeline / 任务状态
#
# 用法：
#   bash run.sh                          # 默认: HTTP 7880 + HTTPS 7881
#   bash run.sh --http-only              # 仅 HTTP (7880)
#   bash run.sh --port 9000              # 自定义 HTTPS 端口
#   bash run.sh --http-port 8080 --port 8443  # 自定义双端口
#   bash run.sh --no-kill                # 不杀旧进程（多实例部署）
#   bash run.sh --python /usr/bin/python3     # 指定 Python 路径
#   bash run.sh --background             # 后台运行（nohup）
#   bash run.sh --status                 # 查看运行状态
#   bash run.sh --stop                   # 停止所有实例
#   bash run.sh --log                    # 实时查看日志
#   bash run.sh --help                   # 显示帮助
#
# 环境变量（优先级低于命令行参数）：
#   HTTP_PORT=7880      HTTP 端口
#   PORT=7881           HTTPS 端口（无证书时退化为 HTTP）
#   PYTHON_BIN=path     Python 解释器路径
#   PYTHONPATH=path     Python 模块搜索路径（默认自动设置）
#
# ==============================================================================

set -euo pipefail

# ──────────────────────────── 颜色输出 ─────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${CYAN}[→]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

# ──────────────────────────── 基本变量 ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
cd "$PROJECT_ROOT"

LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/nohup.out"

# 默认端口
HTTP_PORT="${HTTP_PORT:-7880}"
HTTPS_PORT="${PORT:-7881}"

# SSL 证书
CERT_FILE="$PROJECT_ROOT/certs/video2text.pem"
KEY_FILE="$PROJECT_ROOT/certs/video2text-key.pem"

# Python 解释器：优先 venv，其次 uv，最后系统 python3
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    if [[ -x "$PROJECT_ROOT/.venv/bin/python3" ]]; then
        PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python3"
    elif command -v uv &>/dev/null; then
        PYTHON_BIN="uv-run"  # 标记使用 uv run
    elif command -v python3 &>/dev/null; then
        PYTHON_BIN="$(which python3)"
    else
        err "找不到 Python3，请先运行 bash install.sh 或设置 PYTHON_BIN"
        exit 1
    fi
fi

# 解析后的标志
NO_KILL=false
BACKGROUND=false
HTTP_ONLY=false
ACTION=""

# ──────────────────────────── 参数解析 ─────────────────────────────────────
usage() {
    cat <<'USAGE'
video2text 启动脚本 v0.5.1

用法: bash run.sh [选项]

启动模式:
  (无参数)                启动 HTTP + HTTPS 双端口服务
  --http-only             仅启动 HTTP 模式（不检测 SSL 证书）
  --background, -d        后台运行（nohup + 日志输出到 logs/）
  --no-kill               不杀旧进程（用于多实例部署不同端口）

端口配置:
  --http-port PORT        HTTP 端口 (默认: 7880)
  --port PORT             HTTPS 端口 (默认: 7881)
  --host HOST             监听地址 (默认: 0.0.0.0)

Python 配置:
  --python PATH           Python 解释器路径
                            支持: /path/to/python3, uv run python
                            默认: .venv/bin/python3 > uv > 系统 python3

SSL 证书:
  --cert FILE             SSL 证书路径 (默认: certs/video2text.pem)
  --key FILE              SSL 私钥路径 (默认: certs/video2text-key.pem)

管理命令:
  --status                查看运行状态
  --stop                  停止所有 video2text 实例
  --log                   实时查看日志 (tail -f)
  --restart               重启服务 (等价于 --stop 后启动)

其他:
  --help, -h              显示此帮助信息

环境变量:
  HTTP_PORT               HTTP 端口 (默认: 7880)
  PORT                    HTTPS 端口 (默认: 7881)
  PYTHON_BIN              Python 解释器路径
  PYTHONPATH              Python 模块搜索路径 (默认: src/)

示例:
  bash run.sh                           # HTTP:7880 + HTTPS:7881
  bash run.sh --http-only --port 9000   # 仅 HTTP，端口 9000
  bash run.sh --background              # 后台运行
  bash run.sh --stop                    # 停止服务
  bash run.sh --status                  # 查看状态
  bash run.sh --python "uv run python"  # 使用 uv 运行
USAGE
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --http-only)     HTTP_ONLY=true; shift ;;
        --no-kill)       NO_KILL=true; shift ;;
        --background|-d) BACKGROUND=true; shift ;;
        --http-port)     HTTP_PORT="${2:-}"; shift 2 ;;
        --port)          HTTPS_PORT="${2:-}"; shift 2 ;;
        --host)          HOST="${2:-0.0.0.0}"; shift 2 ;;
        --python)        PYTHON_BIN="${2:-}"; shift 2 ;;
        --cert)          CERT_FILE="${2:-}"; shift 2 ;;
        --key)           KEY_FILE="${2:-}"; shift 2 ;;
        --status)        ACTION="status"; shift ;;
        --stop)          ACTION="stop"; shift ;;
        --restart)       ACTION="restart"; shift ;;
        --log)           ACTION="log"; shift ;;
        --help|-h)       usage ;;
        *)               err "未知参数: $1"; echo "使用 --help 查看帮助"; exit 1 ;;
    esac
done

HOST="${HOST:-0.0.0.0}"

# ──────────────────────────── 管理命令 ─────────────────────────────────────

# 查找 video2text 进程
find_pids() {
    pgrep -f "fastapi_app.py" 2>/dev/null || true
}

# 状态检查
do_status() {
    echo -e "${BOLD}video2text 运行状态${NC}"
    echo "─────────────────────────────────────"

    PIDS=$(find_pids)
    if [[ -z "$PIDS" ]]; then
        warn "未检测到运行中的实例"
        return 0
    fi

    for pid in $PIDS; do
        CMD=$(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ' || echo "N/A")
        # 提取端口
        PORT_ARG=$(echo "$CMD" | grep -oP '(?<=--port )\d+' || echo "?")
        # 提取 SSL
        if echo "$CMD" | grep -q "ssl-certfile"; then
            MODE="HTTPS"
        else
            MODE="HTTP"
        fi
        # 内存和 CPU
        RSS=$(ps -p $pid -o rss= 2>/dev/null | awk '{printf "%.0fMB", $1/1024}' || echo "?")
        CPU=$(ps -p $pid -o %cpu= 2>/dev/null | tr -d ' ' || echo "?")
        UPTIME=$(ps -p $pid -o etime= 2>/dev/null | tr -d ' ' || echo "?")

        echo -e "  PID: ${BOLD}$pid${NC}  端口: ${BOLD}$PORT_ARG${NC}  模式: ${BOLD}$MODE${NC}"
        echo -e "  运行时间: $UPTIME  CPU: ${CPU}%  内存: ${RSS}"
    done

    echo ""
    echo "端口监听:"
    for port in 7880 7881; do
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            echo -e "  :${port}  ${GREEN}● 监听中${NC}"
        else
            echo -e "  :${port}  ${DIM}○ 未监听${NC}"
        fi
    done

    # GPU 状态
    if command -v nvidia-smi &>/dev/null; then
        echo ""
        echo "GPU:"
        nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null | \
            while IFS=, read -r name mem_used mem_total util; do
                echo -e "  ${name}  显存: ${mem_used}/${mem_total}  利用率: ${util}"
            done || true
    fi
}

# 停止
do_stop() {
    PIDS=$(find_pids)
    if [[ -z "$PIDS" ]]; then
        warn "没有运行中的实例"
        return 0
    fi

    info "停止 video2text (PID: $(echo $PIDS | tr '\n' ' '))..."
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null || true
    done

    # 等待最多 5 秒
    for i in $(seq 1 10); do
        PIDS=$(find_pids)
        [[ -z "$PIDS" ]] && break
        sleep 0.5
    done

    # 强制杀
    PIDS=$(find_pids)
    if [[ -n "$PIDS" ]]; then
        warn "进程未响应 SIGTERM，发送 SIGKILL..."
        for pid in $PIDS; do
            kill -9 "$pid" 2>/dev/null || true
        done
    fi

    log "已停止"
}

# 日志
do_log() {
    if [[ -f "$LOG_FILE" ]]; then
        tail -f "$LOG_FILE"
    else
        warn "日志文件不存在: $LOG_FILE"
    fi
}

# 执行管理命令
case "$ACTION" in
    status)  do_status; exit 0 ;;
    stop)    do_stop; exit 0 ;;
    restart) do_stop ;;
    log)     do_log; exit 0 ;;
esac

# ──────────────────────────── 前置检查 ─────────────────────────────────────

# 创建日志目录
mkdir -p "$LOG_DIR"

# 检查 Python 可用性
if [[ "$PYTHON_BIN" == "uv-run" ]]; then
    if ! command -v uv &>/dev/null; then
        err "uv 未安装，请运行: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    RUN_CMD="uv run python"
elif [[ ! -x "$PYTHON_BIN" ]]; then
    err "Python 解释器不存在或不可执行: $PYTHON_BIN"
    err "请运行 bash install.sh 或指定 --python 参数"
    exit 1
else
    RUN_CMD="$PYTHON_BIN"
fi

# 检查 fastapi_app.py 存在
if [[ ! -f "$PROJECT_ROOT/fastapi_app.py" ]]; then
    err "找不到 fastapi_app.py，请在项目根目录运行此脚本"
    exit 1
fi

# ──────────────────────────── 杀旧进程 ─────────────────────────────────────
if [[ "$NO_KILL" == "false" ]]; then
    OLD_PIDS=$(find_pids)
    if [[ -n "$OLD_PIDS" ]]; then
        info "停止旧进程..."
        do_stop
        sleep 0.5
    fi
fi

# ──────────────────────────── 设置环境 ─────────────────────────────────────
export GRADIO_ANALYTICS_ENABLED=False
export NO_PROXY="127.0.0.1,localhost,0.0.0.0"
export PYTHONPATH="$PROJECT_ROOT/src:$PROJECT_ROOT"

# 从 .env 读取端口（仅当用户未显式指定时）
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    ENV_APP_PORT=$(grep -E "^APP_PORT=" "$PROJECT_ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    if [[ -n "$ENV_APP_PORT" ]] && [[ "$HTTPS_PORT" == "7881" ]]; then
        HTTPS_PORT="$ENV_APP_PORT"
    fi
fi

# ──────────────────────────── 构建启动命令 ─────────────────────────────────

# 启动单个实例的函数
start_instance() {
    local port=$1
    local proto=$2
    local extra_args="${3:-}"

    if [[ "$BACKGROUND" == "true" ]]; then
        local logfile="$LOG_DIR/nohup_${port}.out"
        $RUN_CMD "$PROJECT_ROOT/fastapi_app.py" --host "$HOST" --port "$port" $extra_args >> "$logfile" 2>&1 &
    else
        $RUN_CMD "$PROJECT_ROOT/fastapi_app.py" --host "$HOST" --port "$port" $extra_args &
    fi
    echo $!
}

# 信号处理
cleanup() {
    echo ""
    info "收到停止信号，正在关闭..."
    if [[ -n "${HTTP_PID:-}" ]]; then kill "$HTTP_PID" 2>/dev/null || true; fi
    if [[ -n "${HTTPS_PID:-}" ]]; then kill "$HTTPS_PID" 2>/dev/null || true; fi
    wait 2>/dev/null
    log "已停止"
    exit 0
}
trap cleanup SIGTERM SIGINT

# ──────────────────────────── 启动服务 ─────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║       video2text  v0.5.1             ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""

# 检测 GPU
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "N/A")
    log "GPU: $GPU_NAME"
else
    warn "未检测到 NVIDIA GPU，将使用 CPU 模式"
fi

# 检测 Python 版本
if [[ "$PYTHON_BIN" == "uv-run" ]]; then
    PY_VER=$($RUN_CMD -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "?")
    log "Python: $PY_VER (via uv)"
else
    PY_VER=$($RUN_CMD -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "?")
    log "Python: $PY_VER ($PYTHON_BIN)"
fi

# 检测 workspace
if [[ -d "$PROJECT_ROOT/workspace" ]]; then
    TASK_COUNT=$(find "$PROJECT_ROOT/workspace" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    log "Workspace: $PROJECT_ROOT/workspace ($TASK_COUNT 个任务目录)"
fi

echo ""

# 判断启动模式
HAS_SSL=false
if [[ "$HTTP_ONLY" == "false" ]] && [[ -f "$CERT_FILE" ]] && [[ -f "$KEY_FILE" ]]; then
    HAS_SSL=true
fi

if [[ "$HAS_SSL" == "true" ]]; then
    # ── 双端口模式：HTTP + HTTPS ──
    info "启动 HTTP:  http://${HOST}:${HTTP_PORT}"
    HTTP_PID=$(start_instance "$HTTP_PORT" "HTTP")
    log "HTTP 已启动 (PID: $HTTP_PID)"

    info "启动 HTTPS: https://${HOST}:${HTTPS_PORT}"
    HTTPS_PID=$(start_instance "$HTTPS_PORT" "HTTPS" "--ssl-certfile $CERT_FILE --ssl-keyfile $KEY_FILE")
    log "HTTPS 已启动 (PID: $HTTPS_PID)"

    echo ""
    echo -e "  ${GREEN}●${NC} HTTP:  http://localhost:${HTTP_PORT}"
    echo -e "  ${GREEN}●${NC} HTTPS: https://localhost:${HTTPS_PORT}"
    echo ""
    echo -e "  局域网访问:"
    # 获取本机 IP
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<IP>")
    echo -e "  ${DIM}http://${LOCAL_IP}:${HTTP_PORT}${NC}"
    echo -e "  ${DIM}https://${LOCAL_IP}:${HTTPS_PORT}${NC}"

    if [[ "$BACKGROUND" == "true" ]]; then
        echo ""
        log "后台运行中，日志: $LOG_DIR/"
        echo "  查看: bash run.sh --log"
        echo "  停止: bash run.sh --stop"
        echo "  状态: bash run.sh --status"
    else
        echo ""
        info "按 Ctrl+C 停止服务"
        wait $HTTPS_PID $HTTP_PID
    fi
else
    # ── 单端口模式：仅 HTTP ──
    if [[ "$HTTP_ONLY" == "false" ]]; then
        warn "SSL 证书不存在 ($CERT_FILE)，使用 HTTP 模式"
        warn "如需 HTTPS，请运行: bash install.sh --setup-https"
        echo ""
    fi

    info "启动 HTTP: http://${HOST}:${HTTP_PORT}"
    HTTP_PID=$(start_instance "$HTTP_PORT" "HTTP")
    log "HTTP 已启动 (PID: $HTTP_PID)"

    echo ""
    echo -e "  ${GREEN}●${NC} HTTP: http://localhost:${HTTP_PORT}"
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<IP>")
    echo -e "  ${DIM}http://${LOCAL_IP}:${HTTP_PORT}${NC}"

    if [[ "$BACKGROUND" == "true" ]]; then
        echo ""
        log "后台运行中，日志: $LOG_DIR/"
        echo "  查看: bash run.sh --log"
        echo "  停止: bash run.sh --stop"
        echo "  状态: bash run.sh --status"
    else
        echo ""
        info "按 Ctrl+C 停止服务"
        wait $HTTP_PID
    fi
fi

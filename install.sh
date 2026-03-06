#!/usr/bin/env bash
# video2text — 一键安装脚本
# 适用于 Ubuntu 20.04 / 22.04 / 24.04（需要 sudo 权限）
#
# 用法：
#   bash install.sh               # 基础安装
#   SETUP_HTTPS=1 bash install.sh # 额外生成 mkcert HTTPS 证书
#   SETUP_SYSTEMD=1 bash install.sh  # 额外注册并启动 systemd 服务
#   SETUP_HTTPS=1 SETUP_SYSTEMD=1 bash install.sh  # 完整安装
#
# 可选环境变量：
#   PYTHON_VERSION=3.12     # Python 版本，默认 3.12
#   LOCAL_IP=192.168.1.x    # 局域网 IP（用于 HTTPS 证书），默认自动检测
set -euo pipefail

# ──────────────────────────── 颜色输出 ─────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
info() { echo -e "${CYAN}[→]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ──────────────────────────── 基本变量 ─────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
SETUP_HTTPS="${SETUP_HTTPS:-0}"
SETUP_SYSTEMD="${SETUP_SYSTEMD:-0}"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   video2text 安装脚本                ║"
echo "║   项目路径: $PROJECT_DIR"
echo "╚══════════════════════════════════════╝"
echo ""

# ══════════════════════════════════════════════════════════════════
# 步骤 1：检查 OS
# ══════════════════════════════════════════════════════════════════
if [[ "$(uname)" != "Linux" ]]; then
    err "本脚本仅支持 Linux（推荐 Ubuntu 20.04/22.04/24.04）"
fi

info "系统：$(lsb_release -ds 2>/dev/null || uname -r)"

# ══════════════════════════════════════════════════════════════════
# 步骤 2：安装系统依赖
# ══════════════════════════════════════════════════════════════════
info "安装系统依赖（apt）..."
sudo apt-get update -qq

# Python 3.12（Ubuntu < 24.04 需要 deadsnakes PPA）
if ! command -v "python${PYTHON_VERSION}" &>/dev/null; then
    if ! apt-cache show "python${PYTHON_VERSION}" &>/dev/null 2>&1; then
        warn "系统源中无 python${PYTHON_VERSION}，添加 deadsnakes PPA..."
        sudo apt-get install -y --no-install-recommends software-properties-common
        sudo add-apt-repository -y ppa:deadsnakes/ppa
        sudo apt-get update -qq
    fi
    sudo apt-get install -y --no-install-recommends \
        "python${PYTHON_VERSION}" \
        "python${PYTHON_VERSION}-venv" \
        "python${PYTHON_VERSION}-dev"
fi

sudo apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    wget \
    ca-certificates \
    build-essential

log "系统依赖已安装"
log "  Python : $(python${PYTHON_VERSION} --version)"
log "  ffmpeg : $(ffmpeg -version 2>&1 | head -1)"

# ══════════════════════════════════════════════════════════════════
# 步骤 3：安装 uv
# ══════════════════════════════════════════════════════════════════
if ! command -v uv &>/dev/null; then
    info "安装 uv（Python 包管理器）..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # 让 uv 在本次会话中可用
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
fi

UV_BIN="$(command -v uv)"
log "uv : $UV_BIN ($(uv --version))"

# ══════════════════════════════════════════════════════════════════
# 步骤 4：创建 Python 虚拟环境
# ══════════════════════════════════════════════════════════════════
cd "$PROJECT_DIR"

info "创建虚拟环境 .venv（Python $PYTHON_VERSION）..."
"$UV_BIN" venv .venv --python "$PYTHON_VERSION"
log "虚拟环境已创建：$PROJECT_DIR/.venv"

# ══════════════════════════════════════════════════════════════════
# 步骤 5：安装 Python 依赖
# ══════════════════════════════════════════════════════════════════
info "安装 Python 依赖（pyproject.toml）..."
"$UV_BIN" pip install -e .
log "Python 依赖已安装"

# ══════════════════════════════════════════════════════════════════
# 步骤 6：安装 PyTorch（按 GPU 型号选版本）
# ══════════════════════════════════════════════════════════════════
info "检测 GPU，选择 PyTorch 版本..."

TORCH_INSTALLED=0
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo '')"
    COMPUTE="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 || echo '')"
    MAJOR="${COMPUTE%%.*}"

    log "GPU 型号：${GPU_NAME:-未知}  计算能力：${COMPUTE:-未知}"

    if [[ -n "$MAJOR" && "$MAJOR" -lt 7 ]]; then
        # Pascal / Maxwell / Kepler — sm_61 最高兼容 CUDA 12.1 + torch 2.3.1
        warn "Pascal 或更旧架构（sm_${MAJOR}x），安装 torch 2.3.1+cu121（最高兼容版本）..."
        warn "PyTorch >= 2.4 最低要求 sm_70 (Volta)，超出后请勿升级 torch。"
        "$UV_BIN" pip install \
            "torch==2.3.1+cu121" \
            "torchaudio==2.3.1+cu121" \
            --index-url https://download.pytorch.org/whl/cu121
        TORCH_INSTALLED=1
    else
        # Volta / Turing / Ampere / Ada / Hopper — 用最新 CUDA 12.4
        info "现代 GPU（sm_7x+），安装 torch（最新 CUDA 12.4）..."
        "$UV_BIN" pip install torch torchaudio \
            --index-url https://download.pytorch.org/whl/cu124
        TORCH_INSTALLED=1
    fi
else
    warn "未检测到 NVIDIA GPU，安装 CPU 版 torch..."
    warn "若需要 GPU 加速，请手动安装对应 CUDA 版本的 torch。"
    "$UV_BIN" pip install torch torchaudio \
        --index-url https://download.pytorch.org/whl/cpu
    TORCH_INSTALLED=1
fi

[[ "$TORCH_INSTALLED" -eq 1 ]] && log "PyTorch 已安装"

# ══════════════════════════════════════════════════════════════════
# 步骤 7：安装 yt-dlp
# ══════════════════════════════════════════════════════════════════
YTDLP_TARGET="$HOME/.local/bin/yt-dlp"
info "安装/更新 yt-dlp → $YTDLP_TARGET"
mkdir -p "$HOME/.local/bin"
curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" \
    -o "$YTDLP_TARGET"
chmod +x "$YTDLP_TARGET"
export PATH="$HOME/.local/bin:$PATH"
log "yt-dlp : $("$YTDLP_TARGET" --version)"

# ──────── 检查 PATH 是否包含 ~/.local/bin（shell 持久化）────────
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    warn "~/.local/bin 不在 PATH 中，建议在 ~/.bashrc 中添加："
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ══════════════════════════════════════════════════════════════════
# 步骤 8：创建 workspace 目录
# ══════════════════════════════════════════════════════════════════
mkdir -p "$PROJECT_DIR/workspace"
log "workspace 目录：$PROJECT_DIR/workspace"

# ══════════════════════════════════════════════════════════════════
# 步骤 9（可选）：mkcert HTTPS 证书
# ══════════════════════════════════════════════════════════════════
if [[ "$SETUP_HTTPS" == "1" ]]; then
    info "设置 mkcert HTTPS 证书..."

    if ! command -v mkcert &>/dev/null; then
        sudo apt-get install -y --no-install-recommends libnss3-tools
        MKCERT_VER="$(curl -fsSL https://api.github.com/repos/FiloSottile/mkcert/releases/latest \
            | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/')"
        curl -fsSL \
            "https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v${MKCERT_VER}-linux-amd64" \
            -o /tmp/mkcert
        chmod +x /tmp/mkcert
        sudo mv /tmp/mkcert /usr/local/bin/mkcert
        log "mkcert 已安装：$(mkcert --version)"
    fi

    mkcert -install

    # 自动检测局域网 IP
    if [[ -z "${LOCAL_IP:-}" ]]; then
        LOCAL_IP="$(hostname -I | awk '{print $1}')"
    fi
    CERT_FILE="$PROJECT_DIR/video2text.pem"
    KEY_FILE="$PROJECT_DIR/video2text-key.pem"
    mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" "$LOCAL_IP" localhost 127.0.0.1
    log "HTTPS 证书已生成："
    log "  证书：$CERT_FILE"
    log "  私钥：$KEY_FILE"
    log "  访问：https://${LOCAL_IP}:7881"
fi

# ══════════════════════════════════════════════════════════════════
# 步骤 10（可选）：systemd 服务
# ══════════════════════════════════════════════════════════════════
if [[ "$SETUP_SYSTEMD" == "1" ]]; then
    info "注册 systemd 服务..."
    SERVICE_TEMPLATE="$PROJECT_DIR/video2text.service"
    if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
        err "找不到 video2text.service 模板文件：$SERVICE_TEMPLATE"
    fi

    # 将模板中的路径替换为当前用户与当前项目路径
    sed \
        -e "s|User=lym|User=$USER|g" \
        -e "s|Group=lym|Group=$USER|g" \
        -e "s|/home/lym/projects/video2text|$PROJECT_DIR|g" \
        "$SERVICE_TEMPLATE" > /tmp/video2text.service

    sudo cp /tmp/video2text.service /etc/systemd/system/video2text.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now video2text.service

    log "systemd 服务已注册并启动"
    systemctl status video2text.service --no-pager -l 2>/dev/null || true
fi

# ══════════════════════════════════════════════════════════════════
# 完成
# ══════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║       安装完成                       ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo -e "  启动服务："
echo -e "    ${CYAN}cd $PROJECT_DIR && chmod +x main.sh && ./main.sh auto${NC}"
echo ""
echo -e "  HTTP 访问（同局域网）："
echo -e "    ${CYAN}http://$(hostname -I | awk '{print $1}'):7881${NC}"
echo ""
if [[ "$SETUP_HTTPS" != "1" ]]; then
    echo -e "  HTTPS 证书（可选）："
    echo -e "    ${CYAN}SETUP_HTTPS=1 bash $SCRIPT_DIR/install.sh${NC}"
    echo ""
fi
if [[ "$SETUP_SYSTEMD" != "1" ]]; then
    echo -e "  注册为 systemd 服务（可选）："
    echo -e "    ${CYAN}SETUP_SYSTEMD=1 bash $SCRIPT_DIR/install.sh${NC}"
    echo ""
fi
echo -e "  重启服务："
echo -e "    ${CYAN}sudo systemctl restart video2text${NC}"
echo ""

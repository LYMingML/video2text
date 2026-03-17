#!/usr/bin/env bash
# video2text — Docker 一键安装脚本
# 适用于已安装 Docker 的 Linux 服务器
#
# 用法：
#   bash docker-install.sh              # 拉取并安装
#   bash docker-install.sh build        # 本地构建镜像（需在项目目录下）
#   bash docker-install.sh help         # 显示帮助
#
# 环境变量：
#   IMAGE_REGISTRY=ghcr.io/xxx  # 指定镜像仓库（可选）
#   DATA_DIR=/data/video2text   # 数据目录（默认 ./data）
set -euo pipefail

# ──────────────────────────── 配置 ─────────────────────────────
IMAGE_NAME="${IMAGE_REGISTRY:-adolyming/video2text}"
VERSION="0.2.3"
PORT="${APP_PORT:-7881}"
DATA_DIR="${DATA_DIR:-$(pwd)/data}"

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

# ──────────────────────────── 帮助信息 ─────────────────────────────
show_help() {
    cat << 'EOF'
video2text Docker 安装脚本

用法:
  bash docker-install.sh [命令]

命令:
  build     本地构建镜像（需在项目目录下）
  help      显示此帮助信息

示例:
  bash docker-install.sh              # 拉取镜像并安装
  bash docker-install.sh build        # 本地构建镜像

环境变量:
  IMAGE_REGISTRY  镜像仓库地址（如 ghcr.io/user/）
  DATA_DIR        数据存储目录（默认 ./data）
  APP_PORT        服务端口（默认 7881）

注意:
  - 镜像同时支持 GPU 和 CPU，无 GPU 时自动回退 CPU
  - 有 GPU 时需安装 NVIDIA Container Toolkit

EOF
    exit 0
}

# ──────────────────────────── 检查 Docker ─────────────────────────────
check_docker() {
    if ! command -v docker &>/dev/null; then
        err "Docker 未安装，请先安装 Docker: https://docs.docker.com/engine/install/"
    fi

    if ! docker info &>/dev/null; then
        err "Docker 未运行或当前用户无权限。请启动 Docker 或将用户加入 docker 组。"
    fi

    log "Docker: $(docker --version)"
}

# ──────────────────────────── 检查 GPU ─────────────────────────────
check_gpu() {
    if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
        GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo '未知')"
        log "检测到 GPU: $GPU_NAME"
        return 0
    else
        info "未检测到 NVIDIA GPU，将使用 CPU 模式"
        return 1
    fi
}

# ──────────────────────────── 创建配置文件 ─────────────────────────────
create_env_file() {
    local env_file="$DATA_DIR/.env"
    if [[ -f "$env_file" ]]; then
        info "配置文件已存在: $env_file"
        return
    fi

    info "创建配置文件: $env_file"
    mkdir -p "$DATA_DIR"
    cat > "$env_file" << 'EOF'
# video2text 配置文件
APP_PORT=7881
BROWSER_DEBUG_PORT=9222
DEFAULT_BACKEND=FunASR（Paraformer）
DEFAULT_FUNASR_MODEL=paraformer-zh ⭐ 普通话精度推荐
DEFAULT_WHISPER_MODEL=medium
AUTO_SUBTITLE_LANG=zh

# 在线翻译模型配置（可选）
ONLINE_MODEL_ACTIVE_PROFILE=default
ONLINE_MODEL_PROFILE_COUNT=1
ONLINE_MODEL_PROFILE_1_NAME=default
ONLINE_MODEL_PROFILE_1_BASE_URL=https://api.siliconflow.cn/v1
ONLINE_MODEL_PROFILE_1_API_KEY=
ONLINE_MODEL_PROFILE_1_DEFAULT_MODEL=tencent/Hunyuan-MT-7B
ONLINE_MODEL_PROFILE_1_MODEL_LIST_JSON=["tencent/Hunyuan-MT-7B"]

# FFmpeg 线程数
FFMPEG_THREADS=4
# FunASR 每批处理秒数
FUNASR_BATCH_SIZE_S=300
EOF
    log "配置文件已创建，请按需编辑: $env_file"
}

# ──────────────────────────── 构建镜像 ─────────────────────────────
build_image() {
    local tag="${IMAGE_NAME}:latest"

    if [[ ! -f "Dockerfile" ]]; then
        err "找不到 Dockerfile，请在项目目录下运行此脚本"
    fi

    info "构建镜像: $tag"
    docker build -t "$tag" .
    log "镜像构建完成: $tag"
}

# ──────────────────────────── 拉取镜像 ─────────────────────────────
pull_image() {
    local tag="${IMAGE_NAME}:latest"

    info "拉取镜像: $tag"

    if docker pull "$tag"; then
        log "镜像拉取成功: $tag"
        return 0
    else
        warn "镜像拉取失败，尝试本地构建..."
        build_image
        return $?
    fi
}

# ──────────────────────────── 生成启动命令 ─────────────────────────────
print_run_commands() {
    local has_gpu="$1"
    local container_name="video2text"
    local image_tag="${IMAGE_NAME}:latest"

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║       安装完成                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}配置文件:${NC} $DATA_DIR/.env"
    echo -e "  ${CYAN}数据目录:${NC} $DATA_DIR/workspace"
    echo -e "  ${CYAN}模型缓存:${NC} $DATA_DIR/cache"
    echo ""
    echo -e "  ${YELLOW}启动命令:${NC}"
    echo ""

    if [[ "$has_gpu" == "true" ]]; then
        cat << EOF
  docker run -d \\
    --name $container_name \\
    --gpus all \\
    --restart unless-stopped \\
    -p $PORT:7881 \\
    -e HOST=0.0.0.0 \\
    -e PORT=7881 \\
    -e SSL_MODE=http \\
    -v $DATA_DIR/workspace:/app/workspace \\
    -v $DATA_DIR/.env:/app/.env \\
    -v $DATA_DIR/cache/huggingface:/app/.cache/huggingface \\
    -v $DATA_DIR/cache/modelscope:/app/.cache/modelscope \\
    -v $DATA_DIR/cache/torch:/app/.cache/torch \\
    $image_tag

EOF
    else
        cat << EOF
  docker run -d \\
    --name $container_name \\
    --restart unless-stopped \\
    -p $PORT:7881 \\
    -e HOST=0.0.0.0 \\
    -e PORT=7881 \\
    -e SSL_MODE=http \\
    -v $DATA_DIR/workspace:/app/workspace \\
    -v $DATA_DIR/.env:/app/.env \\
    -v $DATA_DIR/cache/huggingface:/app/.cache/huggingface \\
    -v $DATA_DIR/cache/modelscope:/app/.cache/modelscope \\
    -v $DATA_DIR/cache/torch:/app/.cache/torch \\
    $image_tag

EOF
    fi

    echo -e "  ${YELLOW}管理命令:${NC}"
    echo -e "    查看日志: ${CYAN}docker logs -f $container_name${NC}"
    echo -e "    停止服务: ${CYAN}docker stop $container_name${NC}"
    echo -e "    启动服务: ${CYAN}docker start $container_name${NC}"
    echo -e "    删除容器: ${CYAN}docker rm -f $container_name${NC}"
    echo ""
    echo -e "  ${YELLOW}访问地址:${NC}"
    echo -e "    ${CYAN}http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):$PORT${NC}"
    echo ""
}

# ──────────────────────────── 主流程 ─────────────────────────────
main() {
    local action="${1:-}"

    # 显示帮助
    if [[ "$action" == "help" || "$action" == "-h" || "$action" == "--help" ]]; then
        show_help
    fi

    echo ""
    echo "╔══════════════════════════════════════╗"
    echo "║   video2text Docker 安装脚本         ║"
    echo "║   版本: $VERSION"
    echo "╚══════════════════════════════════════╝"
    echo ""

    check_docker

    local has_gpu="false"
    if check_gpu; then
        has_gpu="true"
    fi

    create_env_file

    # 解析参数
    if [[ "$action" == "build" ]]; then
        build_image
    elif [[ -f "Dockerfile" ]]; then
        info "检测到项目源码，使用本地构建..."
        build_image
    else
        pull_image
    fi

    print_run_commands "$has_gpu"
}

main "$@"

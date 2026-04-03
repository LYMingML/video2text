#!/bin/bash
# 启动 XHS-Downloader API 服务
# 用于下载小红书视频

set -e

# XHS-Downloader 项目路径
XHS_PATH="${XHS_DOWNLOADER_PATH:-/home/lym/projects/xhs-test/XHS-Downloader}"

# 检查是否存在
if [ ! -d "$XHS_PATH" ]; then
    echo "❌ XHS-Downloader 未找到"
    echo "请先克隆项目："
    echo "  cd /home/lym/projects/xhs-test"
    echo "  git clone https://github.com/JoeanAmier/XHS-Downloader.git"
    echo ""
    echo "或设置环境变量："
    echo "  export XHS_DOWNLOADER_PATH=/path/to/XHS-Downloader"
    exit 1
fi

# 检查 main.py 是否存在
if [ ! -f "$XHS_PATH/main.py" ]; then
    echo "❌ 无效的 XHS-Downloader 目录"
    exit 1
fi

cd "$XHS_PATH"

# 检查是否已经在运行
if curl -s http://127.0.0.1:5556/docs > /dev/null 2>&1; then
    echo "✅ XHS-Downloader API 服务已在运行"
    echo "   API 文档: http://127.0.0.1:5556/docs"
    exit 0
fi

echo "🚀 启动 XHS-Downloader API 服务..."
echo "   项目路径: $XHS_PATH"
echo "   API 地址: http://127.0.0.1:5556"
echo "   API 文档: http://127.0.0.1:5556/docs"
echo ""

# 启动服务
python main.py api

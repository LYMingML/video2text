#!/usr/bin/env bash
# 安装 systemd 服务，实现开机自动后台运行

cd "$(dirname "$0")"
SERVICE_NAME="video2text"
SERVICE_FILE="$PWD/video2text.service"

# 检查 systemd
if ! command -v systemctl &>/dev/null; then
    echo "错误: 不支持 systemd 的系统"
    exit 1
fi

# 复制服务文件
mkdir -p ~/.config/systemd/user
cp "$SERVICE_FILE" ~/.config/systemd/user/

# 重新加载并启动
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "服务已安装并启动"
echo "查看状态: systemctl --user status $SERVICE_NAME"
echo "查看日志: journalctl --user -u $SERVICE_NAME -f"

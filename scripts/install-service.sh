#!/usr/bin/env bash
# video2text systemd 守护进程安装脚本
# 功能：安装 systemd 服务（自动重启 + 开机自启）+ cron 健康检查
# 用法：sudo bash scripts/install-service.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
CERT_FILE="$PROJECT_ROOT/certs/video2text.pem"
KEY_FILE="$PROJECT_ROOT/certs/video2text-key.pem"
LOG_DIR="$PROJECT_ROOT/logs"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
CURRENT_UID=$(id -u "$CURRENT_USER")
CURRENT_GID=$(id -g "$CURRENT_USER")
HEALTHCHECK="$SCRIPT_DIR/healthcheck.sh"
CRON_LINE="0 8 * * * $HEALTHCHECK >> $LOG_DIR/healthcheck.log 2>&1"

# ---------- 前置检查 ----------
if [ "$(id -u)" -ne 0 ]; then
    echo "错误: 请使用 sudo 运行此脚本" >&2
    echo "  sudo bash $0" >&2
    exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "错误: 找不到 Python 解释器: $PYTHON_BIN" >&2
    exit 1
fi

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "警告: SSL 证书不存在，将只安装 HTTP (7880) 服务"
    INSTALL_HTTPS=false
else
    INSTALL_HTTPS=true
fi

mkdir -p "$LOG_DIR"
chown "$CURRENT_USER:$CURRENT_USER" "$LOG_DIR"

# ---------- 生成 systemd service 文件 ----------
# Restart=on-failure: 进程异常退出时自动重启
# RestartSec=10: 重启间隔 10 秒
# StartLimitBurst/StartLimitIntervalSec: 5 分钟内最多重启 5 次，防止循环

HTTP_SERVICE=$(cat <<EOF
[Unit]
Description=video2text HTTP (port 7880)
After=network.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON_BIN $PROJECT_ROOT/fastapi_app.py --host 0.0.0.0 --port 7880
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=GRADIO_ANALYTICS_ENABLED=false
Environment=NO_PROXY=127.0.0.1,localhost,0.0.0.0
Environment=PYTHONPATH=$PROJECT_ROOT/src:$PROJECT_ROOT
StandardOutput=journal
StandardError=journal
SyslogIdentifier=video2text-http

[Install]
WantedBy=multi-user.target
EOF
)

HTTPS_SERVICE=$(cat <<EOF
[Unit]
Description=video2text HTTPS (port 7881)
After=network.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON_BIN $PROJECT_ROOT/fastapi_app.py --host 0.0.0.0 --port 7881 --ssl-certfile $CERT_FILE --ssl-keyfile $KEY_FILE
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=GRADIO_ANALYTICS_ENABLED=false
Environment=NO_PROXY=127.0.0.1,localhost,0.0.0.0
Environment=PYTHONPATH=$PROJECT_ROOT/src:$PROJECT_ROOT
StandardOutput=journal
StandardError=journal
SyslogIdentifier=video2text-https

[Install]
WantedBy=multi-user.target
EOF
)

# ---------- 安装服务 ----------
echo ">>> 安装 video2text-http.service"
echo "$HTTP_SERVICE" | tee /etc/systemd/system/video2text-http.service > /dev/null

if $INSTALL_HTTPS; then
    echo ">>> 安装 video2text-https.service"
    echo "$HTTPS_SERVICE" | tee /etc/systemd/system/video2text-https.service > /dev/null
else
    echo ">>> 跳过 HTTPS 服务（无 SSL 证书）"
    rm -f /etc/systemd/system/video2text-https.service
fi

systemctl daemon-reload

# ---------- 停止旧进程 ----------
echo ">>> 清理旧进程..."
# 用精确匹配清理，避免影响其他 python 进程
su - "$CURRENT_USER" -c "pgrep -f 'fastapi_app.py' | xargs -r kill 2>/dev/null" || true
sleep 2

# ---------- 启用并启动 ----------
echo ">>> 启用开机自启 + 启动服务"
systemctl enable video2text-http
systemctl start video2text-http

if $INSTALL_HTTPS; then
    systemctl enable video2text-https
    systemctl start video2text-https
fi

# ---------- 安装 cron 健康检查（每 10 分钟） ----------
echo ">>> 配置 cron 健康检查（每 10 分钟）"
chmod +x "$HEALTHCHECK"
chown "$CURRENT_USER:$CURRENT_USER" "$HEALTHCHECK"

su - "$CURRENT_USER" -c "crontab -l 2>/dev/null | grep -v 'video2text/scripts/healthcheck.sh'; echo '$CRON_LINE'" | su - "$CURRENT_USER" -c "crontab -"

# ---------- 等待并显示状态 ----------
echo ""
echo ">>> 等待服务启动..."
sleep 5

echo ""
echo "===== 安装完成 ====="
echo "HTTP:  http://0.0.0.0:7880"
$INSTALL_HTTPS && echo "HTTPS: https://0.0.0.0:7881"
echo ""
systemctl status video2text-http --no-pager -l 2>/dev/null || true
$INSTALL_HTTPS && systemctl status video2text-https --no-pager -l 2>/dev/null || true
echo ""
echo "常用命令:"
echo "  查看状态: sudo systemctl status video2text-http video2text-https"
echo "  查看日志: sudo journalctl -u video2text-http -f"
echo "  重启服务: sudo systemctl restart video2text-http video2text-https"
echo "  停止服务: sudo systemctl stop video2text-http video2text-https"
echo "  健康日志: cat $LOG_DIR/healthcheck.log"

#!/usr/bin/env bash
# video2text 一键安装脚本
# 功能：安装 systemd 服务 + 健康检查 cron，实现开机自启与守护

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
CERT_FILE="$PROJECT_ROOT/certs/video2text.pem"
KEY_FILE="$PROJECT_ROOT/certs/video2text-key.pem"
LOG_DIR="$PROJECT_ROOT/logs"
CURRENT_USER="$(whoami)"
HEALTHCHECK="$SCRIPT_DIR/healthcheck.sh"
CRON_LINE="0 8,20 * * * $HEALTHCHECK >> $LOG_DIR/healthcheck.log 2>&1"

# ---------- 前置检查 ----------
if ! command -v systemctl &>/dev/null; then
    echo "错误: 不支持 systemd 的系统" >&2; exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "错误: 找不到 Python 解释器: $PYTHON_BIN" >&2; exit 1
fi

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "警告: SSL 证书不存在，将只安装 HTTP (7880) 服务"
    INSTALL_HTTPS=false
else
    INSTALL_HTTPS=true
fi

mkdir -p "$LOG_DIR"

# ---------- 生成 systemd service 文件 ----------
HTTP_SERVICE=$(cat <<EOF
[Unit]
Description=video2text HTTP (port 7880)
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_ROOT
ExecStartPre=/bin/bash -c 'PID=\$(pgrep -u $CURRENT_USER -f "fastapi_app.py.*--port 7880"); [ -n "\$PID" ] && kill \$PID && sleep 1 || true'
ExecStart=$PYTHON_BIN $PROJECT_ROOT/fastapi_app.py --host 0.0.0.0 --port 7880
Restart=no
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

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_ROOT
ExecStartPre=/bin/bash -c 'PID=\$(pgrep -u $CURRENT_USER -f "fastapi_app.py.*--port 7881"); [ -n "\$PID" ] && kill \$PID && sleep 1 || true'
ExecStart=$PYTHON_BIN $PROJECT_ROOT/fastapi_app.py --host 0.0.0.0 --port 7881 --ssl-certfile $CERT_FILE --ssl-keyfile $KEY_FILE
Restart=no
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
echo "$HTTP_SERVICE" | sudo tee /etc/systemd/system/video2text-http.service > /dev/null

if $INSTALL_HTTPS; then
    echo ">>> 安装 video2text-https.service"
    echo "$HTTPS_SERVICE" | sudo tee /etc/systemd/system/video2text-https.service > /dev/null
else
    echo ">>> 跳过 HTTPS 服务（无 SSL 证书）"
    sudo rm -f /etc/systemd/system/video2text-https.service
fi

sudo systemctl daemon-reload

# ---------- 停止旧进程（nohup 方式启动的） ----------
pkill -u "$CURRENT_USER" -f "fastapi_app.py" 2>/dev/null || true
sleep 1

# ---------- 启用并启动 ----------
echo ">>> 启用开机自启 + 启动服务"
sudo systemctl enable video2text-http
sudo systemctl start video2text-http

if $INSTALL_HTTPS; then
    sudo systemctl enable video2text-https
    sudo systemctl start video2text-https
fi

# ---------- 安装 cron 健康检查 ----------
echo ">>> 配置 cron 健康检查（每天 8:00 / 20:00）"
chmod +x "$HEALTHCHECK"

# 移除旧的 video2text cron 条目（如果有），再添加新的
(crontab -l 2>/dev/null | grep -v "video2text/scripts/healthcheck.sh"; echo "$CRON_LINE") | crontab -

# ---------- 完成 ----------
echo ""
echo "===== 安装完成 ====="
echo "HTTP:  http://0.0.0.0:7880"
$INSTALL_HTTPS && echo "HTTPS: https://0.0.0.0:7881"
echo ""
echo "常用命令:"
echo "  查看状态: sudo systemctl status video2text-http video2text-https"
echo "  查看日志: journalctl -u video2text-http -f"
echo "  重启服务: sudo systemctl restart video2text-http video2text-https"
echo "  健康日志: cat $LOG_DIR/healthcheck.log"

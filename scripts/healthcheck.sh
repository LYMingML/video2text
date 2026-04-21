#!/usr/bin/env bash
# video2text 健康检查脚本 — 每天 8:00 和 20:00 由 cron 调用
# 检测服务是否存活，挂掉则通过 systemctl 重启

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

SERVICES="video2text-http"
[ -f "$PROJECT_ROOT/certs/video2text.pem" ] && SERVICES="$SERVICES video2text-https"

RESTARTED=""

for svc in $SERVICES; do
    if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "[$(date '+%F %T')] $svc 已停止，正在重启..."
        sudo systemctl restart "$svc" && RESTARTED="$RESTARTED $svc"
    fi
done

if [ -n "$RESTARTED" ]; then
    echo "[$(date '+%F %T')] 已重启:$RESTARTED"
fi

#!/usr/bin/env bash
# OpenClaw 数据备份 — Linux cron 入口
#
# cron 示例（每天凌晨 3 点备份，保留 14 份）:
#   0 3 * * * /opt/openclaw/scripts/backup.sh >> /var/log/openclaw-backup.log 2>&1
#
# 用法: scripts/backup.sh [--dir DIR] [--data DIR] [--keep N]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 优先使用项目虚拟环境，否则回退到系统 python3
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"
else
    PYTHON="python3"
fi

exec "$PYTHON" "$SCRIPT_DIR/backup.py" "$@"

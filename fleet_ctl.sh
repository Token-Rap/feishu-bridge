#!/usr/bin/env bash
# fleet_ctl.sh — 管理 fleet_bridge 多实例 bot (start/stop/restart/status).
#
# 每个 bot = 一个配置目录 (home), 含 .env (app 凭证 + FEISHU_BRIDGE_REPO) + bridge.log.
# home 作为命令行参数传给 bridge, 故 ps/pgrep 能区分实例 (env var 不行, 易留孤儿).
#
# 用法:
#   fleet_ctl.sh start   <home>
#   fleet_ctl.sh stop    <home>
#   fleet_ctl.sh restart <home>
#   fleet_ctl.sh status  <home>
# 例:
#   fleet_ctl.sh restart ~/.fleet-claude-bridge      # quant-trader bot
#   fleet_ctl.sh restart ~/.fleet-orbitos-bridge     # OrbitOS bot
set -uo pipefail

# venv 仍复用 quant-trader bot home 下那套 (含 lark_oapi/claude_agent_sdk);
# 代码位置参数化 (FEISHU_BRIDGE_CODE), 默认 = 本独立 repo 所在目录 (本脚本所在目录),
# 不再硬编码 quant-trader. 想换 venv 设 FEISHU_BRIDGE_VENV.
VENV="${FEISHU_BRIDGE_VENV:-$HOME/.fleet-claude-bridge/venv/bin/python}"
SCRIPT="${FEISHU_BRIDGE_CODE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fleet_bridge.py}"

cmd="${1:-}"; home="${2:-}"
if [ -z "$cmd" ] || [ -z "$home" ]; then
  echo "用法: $0 {start|stop|restart|status} <home_dir>"; exit 1
fi
home="${home/#\~/$HOME}"   # 展开 ~
home="${home%/}"           # 去尾斜杠
logf="$home/bridge.log"
pat="fleet_bridge.py $home"   # ps 匹配该实例 (home 作 argv)

_pids() { pgrep -f "$pat" 2>/dev/null || true; }

case "$cmd" in
  start)
    if [ -n "$(_pids)" ]; then echo "已在跑: $(_pids)"; exit 0; fi
    mkdir -p "$home"; : > "$logf"
    nohup "$VENV" -u "$SCRIPT" "$home" > "$logf" 2>&1 &
    sleep 4
    if [ -n "$(_pids)" ]; then echo "✅ 启动 PID $(_pids)"; grep -m1 "bridge 启动" "$logf" 2>/dev/null
    else echo "❌ 启动失败:"; tail -5 "$logf" 2>/dev/null; exit 1; fi
    ;;
  stop)
    p="$(_pids)"
    if [ -n "$p" ]; then echo "$p" | xargs kill 2>/dev/null; sleep 1
      echo "$p" | xargs kill -9 2>/dev/null || true; echo "已停: $p"
    else echo "未在跑"; fi
    ;;
  restart)
    "$0" stop "$home"; sleep 1; "$0" start "$home"
    ;;
  status)
    p="$(_pids)"
    if [ -n "$p" ]; then echo "running PID $p | $(grep -m1 -o 'repo=[^ ]*' "$logf" 2>/dev/null)"
    else echo "stopped"; fi
    ;;
  *) echo "未知命令: $cmd (start|stop|restart|status)"; exit 1 ;;
esac

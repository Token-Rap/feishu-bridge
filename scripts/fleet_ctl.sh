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
# 默认运行已安装的包。FEISHU_BRIDGE_CODE 可指定兼容的 Python 入口文件；
# 想换 venv 设 FEISHU_BRIDGE_VENV.
VENV="${FEISHU_BRIDGE_VENV:-$HOME/.fleet-claude-bridge/venv/bin/python}"
ENTRY="${FEISHU_BRIDGE_CODE:-feishu_bridge.fleet_bridge}"
if [ -n "${FEISHU_BRIDGE_CODE:-}" ]; then
  launch_args=("$ENTRY")
else
  launch_args=(-m "$ENTRY")
fi

cmd="${1:-}"; home="${2:-}"
if [ -z "$cmd" ] || [ -z "$home" ]; then
  echo "用法: $0 {start|stop|restart|status} <home_dir>"; exit 1
fi
home="${home/#\~/$HOME}"   # 展开 ~
home="${home%/}"           # 去尾斜杠
logf="$home/bridge.log"
pat="${ENTRY} $home"   # ps 匹配该实例 (home 作 argv)

_pids() { pgrep -f "$pat" 2>/dev/null || true; }

case "$cmd" in
  start)
    if [ -n "$(_pids)" ]; then echo "已在跑: $(_pids)"; exit 0; fi
    mkdir -p "$home"; : > "$logf"
    nohup "$VENV" -u "${launch_args[@]}" "$home" > "$logf" 2>&1 &
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

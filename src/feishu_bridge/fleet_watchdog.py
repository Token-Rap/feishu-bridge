#!/usr/bin/env python3
"""feishu bridge watchdog — 心跳陈旧则 launchctl kickstart 重启对应 bot。

分工:
  - launchd KeepAlive + RunAtLoad: 兜「进程死 / 开机」→ 自动拉起 (不用本脚本)
  - 本 watchdog (launchd StartInterval 每 5min): 兜「进程活着但 asyncio loop 卡死 / lark ws
    线程死」—— bridge 心跳 (<home>/heartbeat, 每 30s 写 epoch) 陈旧 → launchctl kickstart -k 强重启

读注册表取得 name→home；通过探测确定 launchd label，避免向不存在的服务发送重启请求。
只对「心跳文件存在且陈旧」动手 (避免误重启刚启动还没写心跳的 bot; 从没写过的由 KeepAlive 兜)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from feishu_bridge.fleet_env import env_any, fleet_registry_path   # 新名优先, 回落旧名 (改名迁移)

FLEET = fleet_registry_path()   # 只此一份, 见 fleet_env
STALE_S = int(env_any("FLEET_HEARTBEAT_STALE_S", "FEISHU_HEARTBEAT_STALE_S", default="180"))   # 心跳 30s 一次, 180s=6 次没写=卡死
# 回合静默检测应保守：长时间运行的工具可能没有中间日志。
# 心跳检测负责发现进程或事件循环故障；等待用户回答的回合也会被豁免。
STUCK_S = int(env_any("FLEET_STUCK_TURN_S", "FEISHU_STUCK_TURN_S", default="1800"))
UID = os.getuid()

_TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00)\]\s+(?:\[[^\]]+\]\s+)?(.*)")


# 活动标记按包含匹配：重试或客户端前缀可能出现在标记之前。
# 漏认活动可能中断正常回合，因此该判据应保守。
_ACTIVITY_MARKS = ("工具:", "💬 消息", "收到", "bridge 启动")


def _last_log_event(home: Path) -> tuple[float, str]:
    """扫 bridge.log 尾部, 返回 (最后一条业务事件的 epoch, 事件描述)。无则 (0, "")。

    只认 bridge 自己打的行 (收到文本/图片/卡片 · 工具: X · bridge 启动), 忽略 lark 噪音。
    """
    logf = home / "bridge.log"
    if not logf.exists():
        return 0.0, ""
    try:
        tail = subprocess.run(["tail", "-300", str(logf)],
                              capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:  # noqa: BLE001
        # 方向是安全的 (读不到 → 不认为卡住 → 不重启), 但**要留痕**: 若这里持续失败,
        # watchdog 就静默退化成一个永远不救人的空转进程, 而且外面看不出区别
        print(f"⚠ 读 {logf} 失败 ({exc}) —— 本轮跳过卡死判定", flush=True)
        return 0.0, ""
    last_ts, last_what = 0.0, ""
    for line in tail.splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        what = m.group(2)
        if not any(k in what for k in _ACTIVITY_MARKS):
            continue
        try:
            ts = datetime.fromisoformat(m.group(1)).timestamp()
        except Exception:  # noqa: BLE001
            continue
        if ts >= last_ts:
            last_ts, last_what = ts, what
    return last_ts, last_what


def _status_ts(home: Path) -> float:
    """读取最近完成时间。文件不存在返回 0；读取失败返回当前时间，避免把未知状态误判为卡死并重启。"""
    f = home / "status.json"
    if not f.exists():
        return 0.0
    try:
        s = json.loads(f.read_text(encoding="utf-8")).get("ts", "")
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ 读 {f} 失败 ({exc}) —— 本轮按「刚收尾」跳过卡死判定, 不重启", flush=True)
        return datetime.now(timezone.utc).timestamp()


def _stuck_reason(home: Path, now: float) -> str | None:
    """判定回合是否挂死; 是则返回原因串, 否则 None。"""
    last_ts, what = _last_log_event(home)
    if not last_ts or "bridge 启动" in what:
        return None                       # 没跑过回合 / 刚启动
    if last_ts <= _status_ts(home):
        return None                       # 回合已收尾, 不在跑
    if "AskUserQuestion" in what:
        return None                       # ⚠ 合法等待用户回答 (可能几小时), 绝不能杀
    silence = now - last_ts
    if silence <= STUCK_S:
        return None
    return f"回合在跑但静默 {int(silence)}s (>{STUCK_S}), 末事件「{what[:30]}」"


# ── 崩溃检测 ─────────────────────────────────────────────────
# launchd 的 runs 计数每次拉起 +1。突增 = 崩过 (KeepAlive 自动拉起)。
# 但主动 kickstart 也会 +1 → safe_restart/本 watchdog 重启前写「计划内」标记, 见 _planned。
RUNS_STATE = Path.home() / ".fleet-ops" / "watchdog_runs.json"
PLANNED_DIR = Path.home() / ".fleet-ops"
PLANNED_TTL = 600          # 标记 10min 内有效


def _bot_label(name: str) -> str:
    """探测 launchd label，新名称优先、旧名称回落；均不可用时返回新名称，让错误指向目标服务。"""
    for lb in (f"com.fleet-bridge.{name}", f"com.feishu-bridge.{name}"):
        try:
            if subprocess.run(["launchctl", "print", f"gui/{UID}/{lb}"],
                              capture_output=True, timeout=10).returncode == 0:
                return lb
        except Exception:  # noqa: BLE001  (探测失败不该让整轮救援停摆)
            continue
    return f"com.fleet-bridge.{name}"


def _runs(label: str) -> int:
    """launchd 记录的启动次数; 取不到返回 -1。"""
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"runs = (\d+)", out)
        return int(m.group(1)) if m else -1
    except Exception:  # noqa: BLE001
        return -1


def _planned(name: str) -> bool:
    """有「计划内重启」标记且未过期 → 消费掉并返回 True (不当作崩溃)。"""
    f = PLANNED_DIR / f".planned_restart_{name}"
    if not f.exists():
        return False
    try:
        fresh = (time.time() - f.stat().st_mtime) < PLANNED_TTL
    except Exception:  # noqa: BLE001
        fresh = False
    f.unlink(missing_ok=True)
    return fresh


def mark_planned(name: str) -> None:
    """主动重启前调用, 免得被误报成崩溃 (safe_restart 也会写这个标记)。"""
    (PLANNED_DIR / f".planned_restart_{name}").touch()


def _notify_crash(home: Path, name: str, delta: int) -> None:
    """崩溃后主动告诉用户 —— 关键是让他知道『上一条回复可能丢了, 该重发』。"""
    hook = os.environ.get("FLEET_NOTIFY_HOOK", "").strip()
    if not hook:
        return
    try:
        subprocess.run(["python3", hook,
                        "--home", str(home),
                        "--title", "⚠ 我刚崩溃重启过",
                        "--text", f"进程异常退出 {delta} 次并被自动拉起。\n\n"
                                  f"**如果你刚才发过消息没等到回复, 请重发一次** —— "
                                  f"崩溃时正在跑的回合会被中断, 那条回复发不出来。\n\n"
                                  f"_(已落盘的文件改动不受影响。)_"],
                       capture_output=True, timeout=25)
    except Exception as exc:  # noqa: BLE001
        print(f"  崩溃通知失败: {exc}")


def check_crashes(fleet: dict) -> None:
    try:
        prev = json.loads(RUNS_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {}
    cur = {}
    for name, e in fleet.items():
        label = _bot_label(name)
        n = _runs(label)
        cur[name] = n
        old = prev.get(name, -1)
        if n < 0 or old < 0 or n <= old:
            continue
        if _planned(name):
            print(f"· {name}: runs {old}→{n} (计划内重启, 不报警)")
            continue
        delta = n - old
        print(f"⚠ {name}: runs {old}→{n} 且无计划标记 → 判定崩溃, 通知用户")
        _notify_crash(Path(os.path.expanduser(e["home"])), name, delta)
    try:
        RUNS_STATE.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  写 runs state 失败: {exc}")


# ── 救不活要报警 ────────────────────────────────────────────────────
# 计划内重启可能掩盖反复自愈失败，因此另行检查重启后心跳是否恢复。
RESCUE_STATE = Path.home() / ".fleet-ops" / "watchdog_rescue.json"
RESCUE_ALERT_AT = 2        # 连续 2 次 kickstart 完心跳还是陈旧 (≈10min) = 救不活


def _rescue_all() -> dict:
    try:
        d = json.loads(RESCUE_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}          # 文件坏了/不存在都从头数 —— watchdog 自己崩比不报警更糟


def _rescue_save(d: dict) -> None:
    try:
        RESCUE_STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  写 rescue state 失败: {exc}")


def note_rescue(name: str) -> int:
    """记一次「救完仍没活」, 返回连续次数。watchdog 每轮是新进程, 不落盘就永远数不到 2。"""
    d = _rescue_all()
    n = int(d.get(name, 0) or 0) + 1
    d[name] = n
    _rescue_save(d)
    return n


def clear_rescue(name: str) -> None:
    """心跳恢复 = 救活了, 清零 —— 下次再坏还要能再报。"""
    d = _rescue_all()
    if d.pop(name, None) is not None:
        _rescue_save(d)


def rescue_exhausted(name: str) -> bool:
    """只在**跨过阈值那一次**为真: 一直响和一直不响一样没用。"""
    return int(_rescue_all().get(name, 0) or 0) == RESCUE_ALERT_AT


def _notify_rescue_failed(home: Path, name: str, n: int) -> None:
    hook = os.environ.get("FLEET_NOTIFY_HOOK", "").strip()
    if not hook:
        return
    try:
        subprocess.run(["python3", hook,
                        "--home", str(home),
                        "--title", "🚨 我重启了也没救回来",
                        "--text", f"连续 {n} 次 kickstart 之后心跳依然陈旧, "
                                  f"多半是**一起来就崩** (启动期异常), 自动重启救不了。\n\n"
                                  f"**这段时间发给我的消息都没人处理**, 请找人看 "
                                  f"`{home}/bridge.log` 的启动段。\n\n"
                                  f"_(心跳恢复后我会自动闭嘴, 不会每 5 分钟响一遍。)_"],
                       capture_output=True, timeout=25)
    except Exception as exc:  # noqa: BLE001
        print(f"  救不活通知失败: {exc}")


# ── watchdog 自己失灵时的告警 ──────────────────────────
# 原来读不到注册表只是 `print(...)` + `return`。而 print 去的是 watchdog.log ——
# **一个没人会打开的文件**, 每 5 分钟重复一次。
# 同一个脚本里"救不活"会推送给用户, "读不到注册表"只进日志, 而后者严重得多:
# 救不活 = 一个 bot 没了 (用户会发现它不回话);
# 读不到注册表时 watchdog 无法检查任何 bot。
# 判据: 「大声」不是"有没有输出", 是**"这行字会出现在谁的视线里"**。
_WD_DOWN_FLAG = Path.home() / ".fleet-watchdog-down"   # 哨兵: 路径**不参与本次改名迁移**


def _alert_watchdog_down(why: str) -> None:
    """watchdog 自己跑不下去了 —— 尽最大努力让人知道。

    依次尝试可选通知钩子、配置的网关目标和本地哨兵。
    ⚠ 哨兵文件仅用于留痕；只有外部监控主动检查时才能成为告警。
    修改哨兵路径或格式时需同步更新该监控。

    去重按**状态翻转**: 只在从"能读"变成"不能读"时推一次, 恢复即清零。
    每 5 分钟推一遍等于骚扰, 而骚扰的下场是被忽略 —— 与救不活那条同一个口径。
    """
    if _WD_DOWN_FLAG.exists():
        return                        # 已经报过, 等恢复清零后才会再报
    text = (f"🚨 **watchdog 已停止工作**，无法自动检查节点。\n\n"
            f"原因: {why}\n\n"
            f"后果: 挂死/心跳陈旧的 bot 不会被自动重启, 而它们看起来一切正常。"
            f"请尽快检查注册表和节点状态。")
    ok = False
    hook = os.environ.get("FLEET_NOTIFY_HOOK", "").strip()
    try:                              # ① 可选通知钩子
        if not hook:
            raise ValueError("notification hook not configured")
        r = subprocess.run(["python3", hook,
                            "--title", "🚨 watchdog 停摆", "--text", text],
                           capture_output=True, timeout=25)
        ok = r.returncode == 0
    except Exception:  # noqa: BLE001
        pass
    alert_bot = os.environ.get("FLEET_WATCHDOG_ALERT_BOT", "").strip()
    if not ok and alert_bot:          # ② 仅在明确配置目标时使用网关
        try:
            import json as _json
            import urllib.request
            tok = (Path.home() / ".fleet-app" / "token").read_text(encoding="utf-8").strip()
            req = urllib.request.Request(
                "http://127.0.0.1:8790/notify",
                data=_json.dumps({"bot": alert_bot, "text": text}).encode(),
                headers={"Content-Type": "application/json", "X-Fleet-Token": tok})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status == 200
        except Exception:  # noqa: BLE001
            pass
    try:                              # ③ 哨兵 (留痕; 需要别的监控来看才算告警)
        _WD_DOWN_FLAG.write_text(f"{datetime.now(timezone.utc).isoformat()} {why}\n",
                                 encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"🚨 watchdog 停摆: {why} (告警送达={ok})", flush=True)


def _clear_watchdog_down() -> None:
    """能正常读到注册表了 → 清零, 下次再坏还能再报。"""
    _WD_DOWN_FLAG.unlink(missing_ok=True)


def check_gateway() -> None:
    """网关探活：launchd 只保证进程存活，不能检测内部卡死。
    网关不可用时客户端无法连接。
    连续两次探测失败才重启, 避免刚重启时的瞬时不可达误判。
    """
    import urllib.request
    label = os.environ.get("FLEET_GATEWAY_LAUNCHD_LABEL", "").strip()
    if not label:
        print("⚠ 未配置 FLEET_GATEWAY_LAUNCHD_LABEL，跳过网关探测及重启")
        return
    flag = Path.home() / ".fleet-ops" / ".gateway_probe_fail"
    ok = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8790/", timeout=8) as r:
            ok = r.status == 200
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        flag.unlink(missing_ok=True)
        return
    fails = 0
    try:
        fails = int(flag.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        pass
    fails += 1
    flag.write_text(str(fails), encoding="utf-8")
    if fails < 2:
        print(f"⚠ 网关探测失败 (第 {fails} 次), 再观察一轮")
        return
    print(f"⚠ 网关连续 {fails} 次探测失败 → kickstart {label}")
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # 服务被整个卸载 (bootout 过) 时 kickstart 无从下手 → 重新 bootstrap
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        r2 = subprocess.run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)],
                            capture_output=True, text=True)
        print(f"  kickstart 失败 ({r.stderr.strip()[:60]}) → bootstrap "
              + ("成功" if r2.returncode == 0 else f"也失败: {r2.stderr.strip()[:60]}"))
        if r2.returncode == 0:
            flag.write_text("0", encoding="utf-8")
    else:
        flag.write_text("0", encoding="utf-8")


def main() -> None:
    check_gateway()
    try:
        fleet = json.loads(FLEET.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # 读不到注册表时必须发出告警。
        _alert_watchdog_down(f"读不到 bot 注册表 {FLEET}: {exc}")
        return
    _clear_watchdog_down()
    now = time.time()
    for name, e in fleet.items():
        if e.get("addr"):  # 远程节点不由本机 launchd 管理
            continue
        home = Path(os.path.expanduser(e["home"]))
        label = _bot_label(name)
        try:
            age = now - int((home / "heartbeat").read_text(encoding="utf-8").strip())
        except Exception:  # noqa: BLE001
            print(f"· {name}: 无心跳文件 (刚启动? 或没装 launchd) — 跳过, 交 KeepAlive")
            continue
        if age > STALE_S:
            # 先记账再救: 本轮还是陈旧 = 上一轮那次 kickstart 没起效。连续记满阈值就报警,
            # 否则「计划内重启」标记会一直替启动即崩的 bot 挡掉 check_crashes 的告警。
            streak = note_rescue(name)
            print(f"⚠ {name} 心跳陈旧 {int(age)}s (>{STALE_S}) → kickstart {label} (第 {streak} 次)")
            mark_planned(name)
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                _alert_watchdog_down(f"kickstart {label} 失败: "
                                     f"{(r.stderr or r.stdout).strip()[:120]}")
            if rescue_exhausted(name):
                print(f"🚨 {name} 连救 {streak} 次仍未恢复 → 通知用户")
                _notify_rescue_failed(home, name, streak)
            continue
        clear_rescue(name)                        # 心跳恢复 = 救活了, 下次再坏还能再报
        reason = _stuck_reason(home, now)          # 心跳正常但回合挂死 → 也要救
        if reason:
            # 所有救援路径均计入连续失败次数。
            # 教训: **加一个保护时, 要数清有几条路径通向它要保护的那件事。**
            streak = note_rescue(name)
            print(f"⚠ {name} {reason} → kickstart {label} (第 {streak} 次)")
            mark_planned(name)
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                # kickstart 本身失败 ≠ 救了但没活: 前者说明**救援机制自己坏了**
                # (label 错 / 服务被卸载…), 那对**所有** bot 都成立, 比单个救不活严重。
                _alert_watchdog_down(f"kickstart {label} 失败: "
                                     f"{(r.stderr or r.stdout).strip()[:120]}")
            elif rescue_exhausted(name):
                _notify_rescue_failed(home, name, streak)
        else:
            print(f"✓ {name} ok (心跳 {int(age)}s 前)")
    check_crashes(fleet)          # 崩溃 → 主动告诉用户重发


if __name__ == "__main__":
    main()

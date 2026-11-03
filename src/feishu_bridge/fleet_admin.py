#!/usr/bin/env python3
"""飞书 bot 舰队管理 CLI — 查订阅用量 / 各 bot 上下文 / 改 model+effort.

被「管理器 bot」(claude administrator) 当 Bash 工具调用。纯 stdlib, 任意 python3 可跑
(不依赖 claude-agent-sdk / lark)。

文件 IPC (跟 fleet_bridge.py 约定):
  - 各 bot 每轮写 <home>/status.json   → 本工具 `context`/`list` 读 (上下文 token/轮数)
  - 本工具 `set-model`/`set-effort` 写 <home>/runtime.json → 各 bot **下一轮**读, 自动
    重建 client 生效 (上下文重置), 无需重启进程。管理器自身改则下次对话生效。

订阅用量 (`usage`): 离线聚合 ~/.claude/projects/**/*.jsonl 里 assistant 消息的 usage
  (覆盖主 Claude Code session + 所有 bot session)。订阅是定额, 故成本列给的是「等效
  API 成本」, 仅作消耗强度代理, 不是真实扣费。

用法:
    python3 -m feishu_bridge.fleet_admin usage [--days 7]
    python3 -m feishu_bridge.fleet_admin context
    python3 -m feishu_bridge.fleet_admin list
    python3 -m feishu_bridge.fleet_admin config
    python3 -m feishu_bridge.fleet_admin set-model <bot|all> <model|default>
    python3 -m feishu_bridge.fleet_admin set-effort <bot|all> <low|medium|high|xhigh|max|default>
"""
from __future__ import annotations

import argparse
try:
    import fcntl                      # Unix file lock
except ImportError:                   # Windows node: same lock via msvcrt (as fleet_bridge.py does)
    fcntl = None
    import msvcrt
import json
import re
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feishu_bridge.fleet_env import env_any, fleet_registry_path   # 新名优先, 回落旧名 (改名迁移)

DEFAULT_FLEET = fleet_registry_path()   # 只此一份, 见 fleet_env
PROJECTS_DIR = Path(os.getenv("CLAUDE_PROJECTS_DIR")
                    or str(Path.home() / ".claude" / "projects"))
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CLEAR = {None, "", "default", "inherit", "默认", "清空", "none"}   # 还原默认的关键词
_CTX_WINDOW = 1_000_000   # claude-opus-5[1m] 上下文窗口, 仅作占用百分比的粗略标尺

# 价格表校准日期会显示在用量报告中，供读者判断数据时效。
_PRICING_CALIBRATED = "2026-09-02"

# 等效 API 价 ($/百万 token): in / out / cache_read / cache_write。
# **最长键优先匹配** (见 _price) —— 顺序无关, 加新档不用管写在哪一行。
#
# 不同模型版本可能使用不同价格或缓存费率，匹配时需优先选更具体的键。
_PRICING = {
    "fable-5-1":  (10.0, 50.0, 0.25, 12.50),   # cache 命中 0.025×, 全表唯一的例外
    "mythos-5-1": (10.0, 50.0, 0.25, 12.50),
    "fable":      (10.0, 50.0, 1.00, 12.50),
    "mythos":     (10.0, 50.0, 1.00, 12.50),   # 与 fable 同规格同价
    "opus":       (5.0, 25.0, 0.50, 6.25),
    "sonnet-5":   (2.0, 10.0, 0.20, 2.50),     # 标准价 (原 introductory, 9/1 涨价已取消)
    "sonnet":     (3.0, 15.0, 0.30, 3.75),     # 4.6 / 4.5 / 4
    "haiku":      (1.0, 5.0, 0.10, 1.25),
}


# ── DeepSeek (backend/T-deepseek-model) ─────────────────────────────
# 来源: https://api-docs.deepseek.com/quick_start/pricing。
# **币种: 美元 (USD), $/百万 token** —— 页面原文就是 $; 跟上面 Claude 那张表同币种,
# 但性质不同: Claude 那张是订阅下的「等效 API 估值」, DeepSeek 这张是**真实扣费价**
# (从充值余额里扣)。
# 分高峰/低谷: 低谷价 = 高峰价的一半。高峰 = **UTC 周一至周五 01:00–04:00 与 06:00–10:00**
# (北京时间 09:00–12:00、14:00–18:00), 不含中国法定节假日; 其余时间 (含周末、节假日全天) 为低谷。
# ⚠ 节假日这里判不了 → 按高峰算, 节假日当天会**高估一倍**, 不会低估。
# DeepSeek 没有单独的"缓存写"价: Claude Code 报的 cache_creation token 按缓存未命中 (= 输入价) 计。
# 元组同上: in(缓存未命中) / out / cache_read(缓存命中) / cache_write(=未命中)。
_DEEPSEEK_PRICING = {
    # deepseek-flash = DeepSeek-V4.1-Flash-1017; 已退役别名 deepseek-v4-flash* 文档写明按 Flash 价计
    "deepseek-flash":   {"peak": (0.30, 1.20, 0.006, 0.30), "offpeak": (0.15, 0.60, 0.003, 0.15)},
    "deepseek-v4-flash": {"peak": (0.30, 1.20, 0.006, 0.30), "offpeak": (0.15, 0.60, 0.003, 0.15)},
    "deepseek-v4-pro":  {"peak": (1.32, 3.96, 0.044, 1.32), "offpeak": (0.66, 1.98, 0.022, 0.66)},
}
_DEEPSEEK_PRICING_CALIBRATED = "2026-10-19"
_DEEPSEEK_PEAK_UTC = ((1, 4), (6, 10))     # [start, end) 小时, 仅周一至周五


def _deepseek_peak(when: datetime | None) -> bool:
    """when 缺省 → 按高峰 (保守)。只看 UTC 星期与小时; 节假日判不了, 也按高峰。"""
    if when is None:
        return True
    w = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    if w.weekday() >= 5:
        return False
    return any(a <= w.hour < b for a, b in _DEEPSEEK_PEAK_UTC)


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(str(p)))


def _atomic_write(path: Path, text: str) -> None:
    """写临时文件再 rename — POSIX 下 rename 原子, 防读到半截/崩溃中途损坏。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _bjt(iso: str) -> str:
    """state 文件里的 UTC ISO 时间 → 面向 user 显示用 BJT (UTC+8)。"""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M BJT")
    except Exception:  # noqa: BLE001
        return str(iso)


def _price(model: str, when: datetime | None = None) -> tuple[float, float, float, float] | None:
    """按 model 名匹配价格, **最长键优先**。

    deepseek-* 走 _DEEPSEEK_PRICING (真实 USD 扣费价, 按 `when` 分高峰/低谷)。
    **认不出的 deepseek-* 返回 None = 价格未知** —— 不按最贵的 Claude 估: 那会把一个便宜
    几十倍的模型报成天价, 用户据此做的"要不要省着用"判断是反的。调用方把它单列为未知。
    (非 deepseek 的未知模型维持老规矩: 按最贵估。)

    最长匹配避免包含关系依赖字典的书写顺序。
    """
    m = (model or "").lower()
    if "deepseek" in m:
        for key, tiers in sorted(_DEEPSEEK_PRICING.items(), key=lambda kv: -len(kv[0])):
            if key in m:
                return tiers["peak" if _deepseek_peak(when) else "offpeak"]
        return None
    for key, tup in sorted(_PRICING.items(), key=lambda kv: -len(kv[0])):
        if key in m:
            return tup
    return _PRICING["fable"]   # 未知模型 → 按最贵估 (保守, 不低报消耗)


def _h(n) -> str:
    """token 数人类可读。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


# ── fleet 注册表 / 运行时配置 / 状态快照 ──────────────────────────────
def load_fleet(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"读 fleet.json 失败 ({path}): {exc}")
    fleet: dict = {}
    try:
        for name, e in raw.items():
            fleet[name] = {
                "home": _expand(e["home"]),
                "repo": e.get("repo", ""),
                "self": bool(e.get("self")),
                # 远程 home 不能作为本地路径操作；Windows 路径在 POSIX 下可能是相对路径。
                "addr": str(e.get("addr", "") or ""),
                "node": str(e.get("node", "") or ""),
            }
    except (KeyError, TypeError, AttributeError) as exc:
        raise SystemExit(f"fleet.json 条目格式错误 ({path}): 每个 bot 需含 home 字段 ({exc})")
    return fleet


def read_runtime(home: Path) -> dict:
    try:
        return json.loads((Path(home) / "runtime.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _set_or_clear(upd: dict, key: str, value) -> None:
    """value 是 _CLEAR 关键词/None/空 → 删 key (还原默认); 否则规整后写入。"""
    if str(value).strip().lower() in _CLEAR:
        upd.pop(key, None)
    else:
        upd[key] = value.strip() if isinstance(value, str) else value


def write_runtime(home: Path, *, model=..., effort=..., advisor=...) -> dict:
    """合并写 <home>/runtime.json (dict spread, 不写死字段)。

    model/effort 传 _CLEAR 里的值 → 删该键 (还原默认); 传 ... → 不动该键。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    # flock(<home>/runtime.lock) 串行化读-改-写, 防与 bridge /model 对同 home 并发覆盖
    with open(home / "runtime.lock", "w") as lf:
        if fcntl is not None:
            fcntl.flock(lf, fcntl.LOCK_EX)
        else:                         # Windows: lock 1 byte, released when the process exits
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        upd = dict(read_runtime(home))
        if model is not ...:
            _set_or_clear(upd, "model", model)
        if effort is not ...:
            _set_or_clear(upd, "effort", effort)
        if advisor is not ...:
            _set_or_clear(upd, "advisor", advisor)
        upd["updated_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_write(home / "runtime.json", json.dumps(upd, ensure_ascii=False, indent=2))
    return upd


def read_status(home: Path) -> dict | None:
    try:
        return json.loads((Path(home) / "status.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_ratelimit(home: Path) -> dict | None:
    """真·订阅额度 (bridge 把 RateLimitEvent 落盘 <home>/ratelimit.json)."""
    try:
        return json.loads((Path(home) / "ratelimit.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _fmt_ratelimit(r: dict) -> str:
    status = r.get("status", "?")
    emoji = {"allowed": "🟢", "allowed_warning": "🟡", "rejected": "🔴"}.get(status, "⚪")
    reset_s = ""
    if r.get("resets_at"):
        try:
            dt = datetime.fromtimestamp(int(r["resets_at"]), timezone(timedelta(hours=8)))
            reset_s = " · 重置 " + dt.strftime("%m-%d %H:%M BJT")
        except Exception:  # noqa: BLE001
            pass
    util = r.get("utilization")
    util_s = f" · 用量 {util}%" if util is not None else ""
    return (f"{emoji} 真·订阅额度 [{r.get('rate_limit_type', '?')}]: {status}{util_s}{reset_s}"
            f"  (引擎 RateLimitEvent, 非估算)")


_STALE_S = 180        # 与 fleet_watchdog.STALE_S 同一个数 (心跳 30s 一次, 6 次没写)


def _is_running(home: Path) -> bool:
    """按心跳判活 (<home>/heartbeat, bridge 每 30s 写一次 epoch)。

    心跳反映事件循环活动，避免仅凭进程存在判断运行状态。
    """
    try:
        ts = int((Path(home) / "heartbeat").read_text(encoding="utf-8").strip())
        return (time.time() - ts) < _STALE_S
    except Exception:  # noqa: BLE001
        return False     # 文件缺失/内容坏/读不了 → 当作没在跑 (与改前同)


# ── 订阅用量聚合 (离线读 transcript) ─────────────────────────────────
def aggregate_usage(projects_dir: Path, days: int) -> dict:
    """聚合 ~/.claude/projects/**/*.jsonl 的 assistant.usage, 按天分桶 + 估等效成本。"""
    projects_dir = Path(projects_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_day: dict[str, dict] = {}
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    unpriced: set = set()        # 价格未知的模型名 (只可能是认不出的 deepseek-*)
    scanned = 0
    if not projects_dir.exists():
        return {"days": days, "by_day": by_day, "total": total, "files_scanned": 0,
                "unpriced_models": []}

    for f in projects_dir.rglob("*.jsonl"):
        # 先按文件 mtime 粗筛, 跳过窗口外的旧会话 (留 1d 余量防跨日边界)
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        if mtime < cutoff - timedelta(days=1):
            continue
        scanned += 1
        try:
            fh = f.open(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        with fh:                              # 逐行读, 不把整文件(可达数百 MB)载入内存
            for line in fh:
                line = line.strip()
                if not line or '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                try:
                    d = datetime.fromisoformat(str(rec.get("timestamp", "")).replace("Z", "+00:00"))
                except Exception:  # noqa: BLE001
                    continue
                if d < cutoff:
                    continue
                price = _price(msg.get("model", ""), d)
                inp = int(usage.get("input_tokens", 0) or 0)
                out = int(usage.get("output_tokens", 0) or 0)
                cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                day = d.date().isoformat()
                b = by_day.setdefault(
                    day, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
                if price is None:
                    # 价格未知: token 照记, 成本不猜 —— 单列出来, 报告里明说"有多少没算价"
                    cost = 0.0
                    for bucket in (b, total):
                        bucket["unpriced_tokens"] = bucket.get("unpriced_tokens", 0) + inp + out + cr + cw
                    unpriced.add(str(msg.get("model", "")))
                else:
                    pin, pout, pcr, pcw = price
                    cost = inp / 1e6 * pin + out / 1e6 * pout + cr / 1e6 * pcr + cw / 1e6 * pcw
                if "deepseek" in str(msg.get("model", "")).lower() and price is not None:
                    for bucket in (b, total):
                        bucket["deepseek_cost"] = bucket.get("deepseek_cost", 0.0) + cost
                for k, v in (("input", inp), ("output", out), ("cache_read", cr),
                             ("cache_write", cw), ("cost", cost)):
                    b[k] += v
                    total[k] += v
    return {"days": days, "by_day": by_day, "total": total, "files_scanned": scanned,
            "unpriced_models": sorted(unpriced)}


# ── 子命令 ───────────────────────────────────────────────────────────
def cmd_usage(args) -> None:
    # 真·订阅额度 (各 bot 落盘的 RateLimitEvent; 账户级共享, 取最新一条)
    fleet = load_fleet(args.fleet)
    rls = [(read_ratelimit(e["home"])) for e in fleet.values()]
    rls = [r for r in rls if r]
    if rls:
        latest = max(rls, key=lambda r: r.get("ts", ""))
        print(_fmt_ratelimit(latest))
        print()

    agg = aggregate_usage(PROJECTS_DIR, args.days)
    t = agg["total"]
    today = datetime.now(timezone.utc).date().isoformat()
    out = [f"📊 等效用量 · 近 {args.days}d (扫 {agg['files_scanned']} 个会话文件)",
           "  来源: 本地 transcript = 主 session + 所有 bot; 订阅定额, 成本为等效 API 估值",
           # 把"这份价格表有多旧"摆在读数字的人眼前: 价格变了没有任何东西会
           # 提醒我们 —— 报告照出、数字照算, 只是全错。sonnet 那档就这么错了一阵子。
           f"  价格表校准于 {_PRICING_CALIBRATED} (官方 pricing 页); 久了请重核",
           ""]
    td = agg["by_day"].get(today)
    if td:
        out.append(f"今天 {today}: 输入 {_h(td['input'])} · 输出 {_h(td['output'])} · "
                   f"缓存读 {_h(td['cache_read'])} · ≈${td['cost']:.2f}")
    else:
        out.append(f"今天 {today}: (暂无记录)")
    out += ["",
            f"近 {args.days}d 合计: 输入 {_h(t['input'])} · 输出 {_h(t['output'])} · "
            f"缓存读 {_h(t['cache_read'])} · 缓存写 {_h(t['cache_write'])}",
            f"近 {args.days}d 等效成本: ≈${t['cost']:.2f}"]
    if t.get("deepseek_cost"):
        out.append(f"  其中 DeepSeek 实际扣费估算 ≈${t['deepseek_cost']:.4f} (USD, 分高峰/低谷; "
                   f"价格表校准于 {_DEEPSEEK_PRICING_CALIBRATED}, 来源 api-docs.deepseek.com/quick_start/pricing)")
    if t.get("unpriced_tokens"):
        out.append(f"  ⚠ 价格未知 (未计入成本): {_h(t['unpriced_tokens'])} token · "
                   f"模型 {', '.join(agg.get('unpriced_models') or []) or '?'}")
    out += ["", "按天:"]
    for day in sorted(agg["by_day"], reverse=True)[:min(args.days, 14)]:
        b = agg["by_day"][day]
        out.append(f"  {day}: in {_h(b['input'])} · out {_h(b['output'])} · ≈${b['cost']:.2f}")
    print("\n".join(out))


def cmd_list(args) -> None:
    fleet = load_fleet(args.fleet)
    print("🤖 Bot 舰队:")
    for name, e in fleet.items():
        if is_remote(e):
            # 原来这里会显示 `[停止] model=(默认)` —— 本机读不到它的文件而已, 却渲染成
            # 一个**确定的事实**。空白已经够糟, 一个自信的假事实更糟。
            print(f"  • {name} [远程节点 {e['addr'] or 'Mac 主机'}] — 状态本机读不到 (去该节点上看)")
            continue
        rt, st = read_runtime(e["home"]), read_status(e["home"])
        model = rt.get("model") or (st or {}).get("model") or "(默认)"
        effort = rt.get("effort") or (st or {}).get("effort") or "(默认)"
        running = "运行中" if _is_running(e["home"]) else "停止"
        ctx = ""
        if st and st.get("sessions"):
            mx = max((s.get("context_tokens", 0) for s in st["sessions"].values()), default=0)
            ctx = f" · ctx≈{_h(mx)}"
        tag = " (管理器自身)" if e["self"] else ""
        print(f"  • {name} [{running}] model={model} effort={effort}{ctx}{tag}")


def cmd_context(args) -> None:
    fleet = load_fleet(args.fleet)
    print("🧠 各 bot 上下文占用 (按最近一轮快照):")
    for name, e in fleet.items():
        if is_remote(e):
            # 「暂无快照 — 下一轮对话后可见」在这里是假的: 它再对话一百轮, 本机也读不到
            print(f"  • {name}: (远程节点 {e['addr'] or 'Mac 主机'} — 快照在那台机器上, 本机读不到)")
            continue
        st = read_status(e["home"])
        if not st or not st.get("sessions"):
            print(f"  • {name}: (暂无快照 — 该 bot 下一轮对话后可见)")
            continue
        for cid, s in st["sessions"].items():
            ctx = int(s.get("context_tokens", 0) or 0)
            mx = int(s.get("max_tokens", 0) or 0)
            pct = s.get("context_pct")
            pct = float(pct) if pct is not None else ctx / _CTX_WINDOW * 100
            cap = _h(mx) if mx else "1M"
            print(f"  • {name} [{cid[:8]}]: ctx≈{_h(ctx)}/{cap} ({pct:.0f}%) · "
                  f"{s.get('num_turns', 0)} 轮 · 模型 {st.get('model', '?')} · 更新 {_bjt(s.get('ts', '?'))}")


def cmd_config(args) -> None:
    fleet = load_fleet(args.fleet)
    print("⚙️ 各 bot 模型/思考强度 (runtime.json 覆盖, 空=SDK 默认 claude-opus-5):")
    for name, e in fleet.items():
        rt = read_runtime(e["home"])
        print(f"  • {name}: model={rt.get('model') or '(默认 claude-opus-5)'} · "
              f"effort={rt.get('effort') or '(默认)'}"
              + (f" · advisor={rt['advisor']}" if rt.get("advisor") else ""))


def _targets(fleet: dict, target: str) -> list:
    if target in ("all", "所有", "全部", "*"):
        return list(fleet.items())
    if target not in fleet:
        raise SystemExit(f"未知 bot: {target} (可选: {', '.join(fleet)}, 或 all)")
    return [(target, fleet[target])]


def is_remote(entry: dict, windows: bool | None = None) -> bool:
    """这个 bot 是否跑在别的机器上 (本机对它的 home 做文件操作一律无意义)。

    An addr entry counts as local only when we ARE on a Windows node, the entry is a Windows
    bot, and its home exists here as an absolute directory. On the Mac it stays remote: a Windows path is a
    *relative* POSIX path, so "exists" there would be a cwd accident.
    """
    on_windows = (os.name == "nt") if windows is None else windows
    if not on_windows:
        return bool(entry.get("addr"))          # Mac: unchanged, addr = elsewhere
    # Windows: only this node's own bots are local. A Mac bot (no addr) must not be "local"
    
    if entry.get("node") != "win":
        return True
    home = Path(entry.get("home") or "")
    return not (home.is_absolute() and home.is_dir())


def _remote_note(name: str, entry: dict) -> str:
    where = entry.get("addr") or "Mac 主机"
    return (f"⛔ {name} 在远程节点 ({where}) — 本工具在本机改不了它, "
            f"请在该节点上执行, 或请该节点的管理员代办")


def _apply_note(entry: dict) -> str:
    return ("（管理器自身: 下次对话生效）" if entry["self"]
            else "（该 bot 下一条消息生效, 记忆保留）")


def _apply_runtime(targets: list, **kw) -> None:
    """对一批目标写 runtime.json, **远程节点明确拒绝而不是假装做了**。

    原来这里对所有目标一律 write_runtime + 打 ✅。远程节点的 home 在本机
    是相对路径, 于是 mkdir 在 cwd 下造假目录、写入成功、照常打勾 —— 用户以为改了,
    那台机器上的 bot 从来没收到过。**点名单个远程 bot 时非零退出** (请求的事一件没做,
    不能算成功); `all` 则本机照常改、远程逐个列出来说明没改 (沉默等于假装全做了)。
    """
    skipped = [(n, e) for n, e in targets if is_remote(e)]
    for name, e in targets:
        if is_remote(e):
            continue
        rt = write_runtime(e["home"], **kw)
        field = next(iter(kw))
        print(f"✅ {name} {field} → {rt.get(field) or '默认'} {_apply_note(e)}")
    for name, e in skipped:
        print(_remote_note(name, e))
    if skipped and len(skipped) == len(targets):
        raise SystemExit(1)          # 一件没做成 → 别让调用方当成功


def cmd_set_model(args) -> None:
    fleet = load_fleet(args.fleet)
    _apply_runtime(_targets(fleet, args.target), model=args.model)


def cmd_set_effort(args) -> None:
    val = (args.effort or "").strip().lower()    # EFFORTS 全小写, 大小写不敏感
    if val not in EFFORTS and val not in _CLEAR:
        raise SystemExit(f"effort 必须是 {'/'.join(EFFORTS)} 或 default, 收到: {args.effort!r}")
    fleet = load_fleet(args.fleet)
    _apply_runtime(_targets(fleet, args.target), effort=val)


_ADVISOR_NAME = re.compile(r"[A-Za-z0-9._\-]{1,64}")


def cmd_set_advisor(args) -> None:
    """runtime.json "advisor" → the bridge passes `--advisor <model>` on its next client build.

    `off` clears it (and makes the bridge forget a previous rejection, so off→on is the retry).
    The name is only shape-checked here; whether the CLI accepts it is found out at connect time,
    where a refusal is logged and the bot falls back to no advisor instead of failing turns.
    """
    val = (args.advisor or "").strip()
    if val.lower() == "off" or val.lower() in _CLEAR:
        val = "default"
    elif not _ADVISOR_NAME.fullmatch(val):
        raise SystemExit(f"advisor 必须是模型 id (字母数字 . _ -, ≤64) 或 off, 收到: {args.advisor!r}")
    fleet = load_fleet(args.fleet)
    _apply_runtime(_targets(fleet, args.target), advisor=val)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="飞书 bot 舰队管理 CLI")
    p.add_argument("--fleet", type=_expand, default=DEFAULT_FLEET,
                   help=f"fleet.json 路径 (默认 {DEFAULT_FLEET})")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("usage", help="订阅用量 (离线聚合 transcript)")
    u.add_argument("--days", type=int, default=7)
    u.set_defaults(func=cmd_usage)

    sub.add_parser("list", help="列出所有 bot + 状态").set_defaults(func=cmd_list)
    sub.add_parser("context", help="各 bot 上下文占用").set_defaults(func=cmd_context)
    sub.add_parser("config", help="各 bot 模型/effort 配置").set_defaults(func=cmd_config)

    sm = sub.add_parser("set-model", help="改某 bot/all 的模型")
    sm.add_argument("target")
    sm.add_argument("model", help="opus/sonnet/haiku/完整 id, 或 default 还原")
    sm.set_defaults(func=cmd_set_model)

    se = sub.add_parser("set-effort", help="改某 bot/all 的思考强度")
    se.add_argument("target")
    se.add_argument("effort", help=f"{'/'.join(EFFORTS)} 或 default")
    se.set_defaults(func=cmd_set_effort)

    sa = sub.add_parser("set-advisor", help="给某 bot/all 开 advisor (--advisor <model>), off 关")
    sa.add_argument("target")
    sa.add_argument("advisor", help="模型 id, 如 claude-fable-5-1; off 关闭")
    sa.set_defaults(func=cmd_set_advisor)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

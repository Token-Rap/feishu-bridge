#!/usr/bin/env python3
"""通过 task_track.json 管理任务卡，bridge 会监视文件并更新客户端。

用法 (bot 侧):
  task_card.py --home <bridge_home> set "读代码|in_progress" "改代码" "跑测试"
  task_card.py --home <bridge_home> start 2      # 第 2 条 → 进行中
  task_card.py --home <bridge_home> done 2       # 第 2 条 → 完成
  task_card.py --home <bridge_home> clear
  task_card.py --home <bridge_home> show

状态取值 pending | in_progress | completed (省略 = pending)。
序号 1-based, 就是 show 里显示的那个。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ⚠ **输出编码由脚本自己保证, 别指望调用方设环境变量**:
# Windows 的 Git Bash 通道里 Python 按本地 ANSI 码页 (cp936) 编 stdout, 不是 UTF-8 ——
# 这个脚本几乎每一行输出都含中文, 于是那边跑出来是**退出码 0 + 乱码**。
# 注意失败形状: 同一个根因在 win_probe 那边是**崩溃** (UnicodeEncodeError 当场看得见),
# 这里却是"命令成功了、文件也真写进去了, 只是人读不懂" —— 一吵一静默, 静默那个只有
# 真的有人去读输出时才会发现。
# **stderr 一起 reconfigure**: 错误消息全是中文, 而人最需要读懂的恰恰是出错那次。
# 用 PYTHONIOENCODING 也能修, 但那条命令将来会被复用 (cron / 手敲 / 别的 bot 抄走),
# 环境变量不跟着走 —— 同「解释器该由进程自己报」是一条思路: 自己的形态自己负责。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001  (被重定向成非 TextIO 时没有这个方法, 不该因此崩)
        pass

TRACK_V = 2                      # 必须与 bridge 的 _TASK_TRACK_V 一致, 不然它整份丢弃
STATUSES = ("pending", "in_progress", "completed")


def _die(msg: str) -> None:
    print(f"task_card: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _load(home: Path) -> dict:
    """读现有清单; 坏文件不当成空 —— 那会把一次手滑放大成「任务卡全没了」。"""
    f = home / "task_track.json"
    if not f.exists():
        return {}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _die(f"{f} 读不动 ({exc}) —— 没动它, 请人看一眼")
    if not isinstance(raw, dict):
        _die(f"{f} 不是对象, 没动它")
    raw.pop("_v", None)
    return raw


def _chats_of(home: Path) -> list[str]:
    """这个 bot **实际有几个会话** (读 sessions.json), 跟清单里已经有几个键无关。"""
    try:
        s = json.loads((home / "sessions.json").read_text(encoding="utf-8"))
        return [k for k in s if isinstance(s.get(k), dict)]
    except Exception:  # noqa: BLE001
        return []


def _pick_chat(home: Path, track: dict, want: str | None) -> str:
    """定位**这次写入**属于哪个会话。多会话时必须显式指定。

    同时检查会话列表和已有任务清单；仅查看清单键会漏掉尚未写入任务的会话，
    导致不同会话误用同一张任务卡。
    """
    if want:
        return want
    # ⚠ **两处来源合起来判**, 任何一处有歧义就罢工:
    #   · sessions.json = 这个 bot 现在有几个会话;
    #   · track         = 清单里已经有几个会话在写 (老判据, 原来那条测试守的就是它)。
    # 只看前者会漏掉「另一个会话暂时不在 sessions.json 里、但它的条目还在卡上」;
    # 只看后者就是那个 bug 本身 (第一次写入之后它恒为 1, 守卫永远触发不了)。
    known = sorted(set(_chats_of(home)) | {k for k in track if not k.startswith("_")})
    if len(known) == 1:
        return known[0]
    if not known:
        _die("还没有任何会话 (先聊一句再建清单)")
    _die(f"这个 bot 有 {len(known)} 个会话, **必须**加 --chat <chat_id>: "
         f"{', '.join(known)}\n"
         f"  (清单是共享的: 各会话写各自的条目, 合并成一张卡给用户看。"
         f"不指定可能写进其他会话的卡。)")
    return ""                                   # unreachable, 让类型检查闭嘴


def _active_sid(home: Path, chat: str) -> str:
    """当前 active session_id —— **必须带对**: bridge 的 _session_tasks 按 sid 判断
    「这份清单是不是这个会话的」, 对不上的下一次就被当成换会话清掉。"""
    try:
        s = json.loads((home / "sessions.json").read_text(encoding="utf-8"))
        return str((s.get(chat) or {}).get("active") or "")
    except Exception:  # noqa: BLE001
        return ""


def _save(home: Path, track: dict) -> None:
    """原子写 —— bridge 正在轮询这个文件, 半截内容会被它读到。"""
    f = home / "task_track.json"
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({**track, "_v": TRACK_V}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(f)
    except Exception as exc:  # noqa: BLE001
        _die(f"写 {f} 失败: {exc}")


def save_one(home: Path, chat: str, entry: dict | None, retries: int = 6) -> dict:
    '只更新指定会话，从最新磁盘状态合并；并发读改写必须考虑冲突。\n\n    ⚠ **诚实说清它防到哪一步**: 用 mtime_ns 做乐观锁 + 重试, 把窗口从 **5 秒**压到\n    「读盘到 rename 之间的几十微秒」。**不是无锁安全** —— 两个写者撞进同一微秒仍会丢。\n    没上文件锁是因为 `fcntl` 只有 POSIX 有, 而这个脚本 Windows 节点也在跑\n    (灰度同步过去的), **跨平台的锁值不回这点收益**。\n    真要再紧一档就得引跨平台锁, 那是另一件事; 在那之前**别把这里当成"已经安全了"**。\n    '
    f = home / "task_track.json"
    for _ in range(retries):
        try:
            before = f.stat().st_mtime_ns
        except FileNotFoundError:
            before = -1
        track = _load(home)
        if entry is None:
            track.pop(chat, None)
        else:
            track[chat] = entry
        try:
            now = f.stat().st_mtime_ns
        except FileNotFoundError:
            now = -1
        if now != before:
            continue            # 读完到现在有人动过 → 重读, 别拿陈旧的底稿写回去
        _save(home, track)
        return track
    _die("task_track.json 一直被别人改, 重试 6 次都没抢到 —— 稍后再试 (清单没被动过)")
    return {}                                   # unreachable


def _items(track: dict, chat: str) -> list:
    return list((track.get(chat) or {}).get("items") or [])


def merged(track: dict) -> list:
    """所有会话的条目合并成一张清单, **顺序稳定** (按会话键排序)。

    ⚠ **bridge 也 import 这个函数** —— 用户看到的序号和 `start N`/`done N` 认的序号
    必须是同一套。各写一份合并逻辑迟早漂开, 而漂开的表现是"我说 done 5, 划掉的是第 6 条"。

    每条带 `_chat` 标出归属 (显示用; 不落盘, `_save` 存的仍是分会话的原结构)。
    ⇒ 分会话存放带来两件免费的东西, 别为了"一张卡"把它拍平:
      · `set` 天然只动自己那份, **不会覆盖另一个会话的条目**;
      · 换会话 (`/new`) 的清空是按会话走的, **只清自己的**, 不会连带清掉别人的。

    ⚠ **但按 content 去重**: 同一件事在两个会话里各写一条时,
    卡上只出现一次，任务状态跨会话合并。
    ⇒ 上面那两条好处照旧成立 —— 错的不是分桶存, 是把**存储的隔离**泄露成了**显示的分组**。
    状态取**最进展**的那个: 一件事只要有人标完成了它就是完成的。
    这样 `start`/`done` 一行都不用改 —— 在任一会话里划掉, 合并后就是划掉的;
    否则用户点了完成而卡上纹丝不动, **且没有任何报错**。
    """
    rank = {"completed": 2, "in_progress": 1, "pending": 0}
    out: list = []
    at: dict = {}                       # content → 它在 out 里的下标
    for c in sorted(k for k in track if not k.startswith("_")):
        for it in _items(track, c):
            key = it.get("content", "")
            if key in at:               # 同一件事: 只升级状态, 不再添一行
                prev = out[at[key]]
                if rank.get(it.get("status"), 0) > rank.get(prev.get("status"), 0):
                    out[at[key]] = {**prev, "status": it.get("status")}
                continue
            at[key] = len(out)
            out.append({**it, "_chat": c})
    return out


def _show(items: list, mine: str = "") -> None:
    if not items:
        print("(清单为空)")
        return
    mark = {"completed": "✓", "in_progress": "▶", "pending": "·"}
    for i, it in enumerate(items, 1):
        # 别人写的条目标出来源 —— 不标的话它在你卡上就是"凭空冒出来的一条任务"
        # (用户 就是这么撞上的)。
        own = it.get("_chat", mine)
        tag = "" if own == mine else f"   [{own}]"
        print(f"{i:>2}. {mark.get(it.get('status'), '·')} {it.get('content', '')}{tag}")


def main(argv: list[str]) -> None:
    home = None
    chat = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--home" and i + 1 < len(argv):
            home = Path(argv[i + 1]).expanduser(); i += 2
        elif argv[i] == "--chat" and i + 1 < len(argv):
            chat = argv[i + 1]; i += 2
        else:
            rest.append(argv[i]); i += 1
    if not home:
        _die("缺 --home <bridge_home>")
    if not home.is_dir():
        _die(f"--home {home} 不是目录")
    if not rest:
        _die("缺子命令: set | start | done | clear | show")

    cmd, args = rest[0], rest[1:]
    track = _load(home)
    chat = _pick_chat(home, track, chat)
    items = _items(track, chat)

    if cmd == "show":
        _show(merged(track), chat)
        return

    if cmd == "set":
        if not args:
            _die('set 要至少一条, 例: set "读代码|in_progress" "改代码"')
        items = []
        for n, raw in enumerate(args, 1):
            content, _, st = raw.partition("|")
            st = (st or "pending").strip()
            if st not in STATUSES:
                _die(f"第 {n} 条的状态「{st}」不认识, 只能是 {' / '.join(STATUSES)}")
            items.append({"id": str(n), "content": content.strip(),
                          "activeForm": "", "status": st})
    elif cmd in ("start", "done"):
        if len(args) != 1 or not args[0].isdigit():
            _die(f"{cmd} 要一个序号, 例: {cmd} 2")
        n = int(args[0])
        # ⚠ 序号认的是**合并后**那一套 —— 也就是用户在卡上看到的那个数。
        # 只按自己那份编号的话, 你说 done 5 划掉的会是别的一条, 而且看起来完全正常。
        all_items = merged(track)
        if not 1 <= n <= len(all_items):
            _die(f"没有第 {n} 条 (当前 {len(all_items)} 条)")
        hit = all_items[n - 1]
        owner = hit.get("_chat", chat)
        own_items = _items(track, owner)
        # 在归属会话的那份里定位它 (合并顺序是稳定的, 同会话内相对顺序不变)
        idx = [i for i, it in enumerate(own_items)
               if it.get("id") == hit.get("id")
               and it.get("content") == hit.get("content")]
        if not idx:
            _die(f"第 {n} 条对不上归属会话 {owner} 的清单 —— 清单刚被另一边改过? 重跑一次 show")
        own_items[idx[0]] = {**own_items[idx[0]],
                             "status": "in_progress" if cmd == "start" else "completed"}
        track = save_one(home, owner,
                         {"sid": (track.get(owner) or {}).get("sid") or _active_sid(home, owner),
                          "items": own_items})
        _show(merged(track), chat)
        return
    elif cmd == "clear":
        items = []          # ⚠ 只清自己这份, 别人的条目不动
    else:
        _die(f"不认识的子命令「{cmd}」: set | start | done | clear | show")

    track = save_one(home, chat, {"sid": _active_sid(home, chat), "items": items})
    _show(merged(track), chat)


if __name__ == "__main__":
    main(sys.argv[1:])

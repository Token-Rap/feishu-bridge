"""引用回复 ref 判定 (纯逻辑, 无 lark 依赖, 便于单测)。

引用只认 parent_id；线程根 root_id 不表示引用。
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path


def pick_quote_ref(parent_id: str | None, last_ref: str | None = None) -> str | None:
    """决定本条消息要前置的引用 ref。

    只认 `parent_id` (真·回复才有; 普通消息 / 仅线程 root 没有 → 不前置)。
    线程恒定 `root_id` 从不传进来, 故 off-by-one 不会复发。
    不按 last_ref 去重 —— 用户**手动重复引用同一条**应照常前置。
    last_ref 仅保留签名向后兼容, 不参与判定。
    """
    return parent_id or None


def remember_sent(cache, message_id: str | None, text: str | None, cap: int = 300) -> None:
    """记 bot 出站消息正文 → 供用户引用回复时兜底 (纯逻辑, 就地改 cache)。

    飞书 `im/v1/messages/{id}` 对 CardKit 卡片只返「请升级客户端」占位符, 取不到真正文;
    bot 自己发的正文这里缓存下来, 引用回复时按 message_id 兜底命中。
    cache: OrderedDict[message_id → text]; message_id/text 空则 no-op;
    超 cap 逐出最旧 (LRU, move_to_end 维持新近性)。
    """
    if not message_id or not text:
        return
    cache[message_id] = text
    cache.move_to_end(message_id)
    while len(cache) > cap:
        cache.popitem(last=False)


def load_sent_cache(path, cap: int = 300) -> "OrderedDict[str, str]":
    """启动时从磁盘载入 bot 出站正文缓存 → 重启后引用 bot 卡片回复仍可命中。
    读不到 / 损坏 / 超长 → 安全降级 (空缓存 / 截断到 cap 条最新)。纯逻辑, 便于单测。
    """
    od: "OrderedDict[str, str]" = OrderedDict()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  (文件缺失/损坏 → 空缓存)
        return od
    if not isinstance(data, dict):
        return od
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            od[k] = v
    while len(od) > cap:
        od.popitem(last=False)
    return od


def save_sent_cache(cache, path) -> None:
    """把出站正文缓存原子落盘 (JSON)。失败静默, 绝不影响主消息流程。纯逻辑, 便于单测。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(dict(cache), ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # 原子替换, 防写一半被读到
    except Exception:  # noqa: BLE001
        pass


def load_notify_entry(path, message_id: str) -> str | None:
    """查外部 cron 推送脚本写的通知卡正文缓存 (notify_card_cache.json)。

    该文件由 通知工具等**外部进程**写, bridge 只读且
    现读现取 (不常驻内存) —— cron 在 bridge 启动后新发的卡也能查到。
    文件缺失 / 坏 JSON / id 不在 → None。纯逻辑, 便于单测。
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get(message_id)
    except Exception:  # noqa: BLE001
        return None

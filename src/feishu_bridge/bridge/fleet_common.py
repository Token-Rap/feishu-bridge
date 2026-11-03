"""Leaf helpers shared by fleet_bridge and the modules split out of it (T022).

No lark / SDK imports. New modules import from here instead of reaching back into
fleet_bridge.py — that is how we avoid import cycles when the big file is cut.

This file is **added first** (「先加后减」): fleet_bridge.py still has its own
copies and is unchanged. The next commit switches fleet_bridge over to
`from fleet_common import …` and drops the duplicates.

What belongs here: pure / nearly-pure helpers that many pieces need
(log, token math, text chunking). Stateful writers like `_write_status` stay in
bridge until their own module cut, because they depend on bridge globals.
"""
from __future__ import annotations

from datetime import datetime, timezone


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def ctx_tokens(usage: dict) -> int:
    """当前上下文窗口占用 ≈ 本轮全部输入 (input + 两类 cache)."""
    if not usage:
        return 0
    return (int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0))


def mu_total(entry: dict) -> int:
    if not isinstance(entry, dict):
        return 0
    return sum(int(entry.get(k, 0) or 0) for k in (
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens"))


def chunks(text: str, size: int):
    if not text:
        return
    for i in range(0, len(text), size):
        yield text[i:i + size]

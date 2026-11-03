"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


class StubFront:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


_TASKS = [{"status": "completed", "content": "甲"}, {"status": "pending", "content": "乙"}]


def _broadcast(**kw):
    b.APP_FRONT = StubFront()
    b._broadcast_todos(_TASKS, **kw)
    return b.APP_FRONT.events[-1]







def test_shared_card_carries_the_files_mtime_not_now(monkeypatch, tmp_path):
    """靶心: `ts` 必须来自 task_track.json 的 mtime, **不是 time.time()**。"""
    import json as _json
    import os
    import time as _time
    f = tmp_path / "task_track.json"
    f.write_text(_json.dumps({"app": {"sid": "s", "items": _TASKS}}), encoding="utf-8")
    old = _time.time() - 3600           # 一小时前写的卡
    os.utime(f, (old, old))
    monkeypatch.setattr(b, "_TASK_TRACK_FILE", f)
    monkeypatch.setattr(b, "_TASK_TRACK", {"app": {"sid": "s", "items": _TASKS}})
    monkeypatch.setattr(b, "CLIENTS", {"app": object()})
    b.APP_FRONT = StubFront()
    b.APP_FRONT.chat_id = "app"
    assert b._push_task_card() is True
    ts = {e["ts"] for e in b.APP_FRONT.events if e.get("ev") == "todos"}
    assert ts == {int(old)}, f"ts 不是文件 mtime: {ts} (现在是 {int(_time.time())})"


def test_every_chat_gets_the_same_ts():
    """同一张卡播给几个会话, `ts` 必须一样 —— 不一样就等于说"几张卡"。"""
    b.APP_FRONT = StubFront()
    for c in ("app", "group:fleet-product"):
        b._broadcast_todos(_TASKS, replay=True, chat=c, ts=1700000000)
    assert {e["ts"] for e in b.APP_FRONT.events} == {1700000000}


def test_no_ts_means_no_key_at_all():
    """Behavioral regression coverage."""
    ev = _broadcast(replay=True, chat="app")
    assert "ts" not in ev, ev


def test_live_todos_still_carry_no_ts():
    """回合里的清单不带 `ts` —— 它不是共享卡, 客户端走 live 那条路。"""
    assert "ts" not in _broadcast()


def test_live_update_has_no_replay_flag():
    """回合里的清单变更**不能**带 replay —— 带了客户端会当成对齐, 不认为回合在跑。"""
    ev = _broadcast()
    assert ev["ev"] == "todos" and "replay" not in ev


def test_startup_align_is_marked_replay():
    ev = _broadcast(replay=True)
    assert ev["replay"] is True


def test_items_identical_either_way():
    """标记只是元信息, 清单内容两种情况完全一致。"""
    assert _broadcast()["items"] == _broadcast(replay=True)["items"]
    assert _broadcast()["items"] == [{"status": "completed", "content": "甲"},
                                     {"status": "pending", "content": "乙"}]


def test_broadcast_never_raises():
    """fail-soft: 广播炸了也不能掀翻回合。"""
    class Boom:
        def broadcast(self, ev):
            raise RuntimeError("网关没了")

    b.APP_FRONT = Boom()
    b._broadcast_todos(_TASKS, replay=True)      # 不抛就算过


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

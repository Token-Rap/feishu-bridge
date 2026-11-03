"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-intr-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "c-intr"


class _Client:
    def __init__(self, on_interrupt=None, boom=False):
        self.calls, self._on, self._boom = 0, on_interrupt, boom

    async def interrupt(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("SDK 拒绝")
        if self._on:
            self._on()


@pytest.fixture(autouse=True)
def _clean():
    for s in (b.ACTIVE, b.IDLE_ACTIVE, b.INTERRUPTED):
        s.discard(CHAT)
    b.CLIENTS.pop(CHAT, None)
    b.COALESCE.pop(CHAT, None)
    b.APP_FRONT.broadcast = lambda _e: None
    yield
    for s in (b.ACTIVE, b.IDLE_ACTIVE, b.INTERRUPTED):
        s.discard(CHAT)
    b.CLIENTS.pop(CHAT, None)


def _run(client, active=True, queued=0):
    b.CLIENTS[CHAT] = client
    if active:
        b.ACTIVE.add(CHAT)
    if queued:
        b.COALESCE[CHAT] = [("x", 0.0)] * queued
    return asyncio.run(b._do_interrupt(CHAT))


# ── 三态各自分得开 ──────────────────────────────────────────────────
def test_idle_when_nothing_is_running():
    c = _Client()
    state, dropped = _run(c, active=False)
    assert state == "idle" and dropped == 0
    assert c.calls == 0, "没在跑就不该去掐"


def test_stopped_when_the_turn_really_ends():
    """真结束 = ACTIVE 被那轮自己的 finally 清掉。"""
    c = _Client(on_interrupt=lambda: b.ACTIVE.discard(CHAT))
    state, _ = _run(c)
    assert state == "stopped"


def test_stuck_when_the_turn_never_ends(monkeypatch):
    """Behavioral regression coverage."""
    monkeypatch.setattr(b, "_INTERRUPT_WAIT_S", 0.3)     # 别让测试等满
    c = _Client()                                        # 不清 ACTIVE
    state, _ = _run(c)
    assert state == "stuck", "掐不动却报成功 = 又一个假成功信号"
    assert CHAT in b.ACTIVE


def test_stuck_is_bounded(monkeypatch):
    """等待必须有界 —— 否则停止按钮自己也挂住。"""
    monkeypatch.setattr(b, "_INTERRUPT_WAIT_S", 0.3)
    import time as _t
    t0 = _t.monotonic()
    _run(_Client())
    assert _t.monotonic() - t0 < 3, "等太久, 用户会以为按钮坏了"


# ── 既有行为不许丢 ──────────────────────────────────────────────────
def test_queue_is_cleared_and_counted():
    c = _Client(on_interrupt=lambda: b.ACTIVE.discard(CHAT))
    state, dropped = _run(c, queued=3)
    assert state == "stopped" and dropped == 3


def test_interrupt_flag_rolled_back_on_error():
    """掐失败要把 INTERRUPTED 收回 —— 否则下一轮的错误回复被无故抑制。"""
    with pytest.raises(RuntimeError):
        _run(_Client(boom=True))
    assert CHAT not in b.INTERRUPTED


def test_idle_active_counts_as_running():
    """轮间监听里的后台回合也算在跑 (既有语义)。"""
    c = _Client(on_interrupt=lambda: b.IDLE_ACTIVE.discard(CHAT))
    b.IDLE_ACTIVE.add(CHAT)
    state, _ = _run(c, active=False)
    assert state == "stopped"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

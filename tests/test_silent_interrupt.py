"""App 静默中断帧 (interrupt) 的行为测试。

要点: 不发任何聊天消息, 只广播 {"ev":"interrupted", ok, cleared} 回执;
「停」文本路仍走 SENDER 出 🛑 (两条路共用 _do_interrupt)。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "silent-interrupt-test"


class StubSender:
    def __init__(self):
        self.texts: list[str] = []

    def send(self, chat, text):
        self.texts.append(text)

    def send_text(self, chat, text):
        self.texts.append(text)


class StubFront:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


class StubClient:
    def __init__(self):
        self.interrupted = 0

    async def interrupt(self):
        self.interrupted += 1


def _setup(active: bool):
    sender, front = StubSender(), StubFront(CHAT)
    b.SENDER, b.APP_FRONT = sender, front
    client = StubClient()
    # 掐完要模拟**这一轮真的收尾** (真实里是 run_turn 的 finally 放掉 ACTIVE)。
    # 不模拟的话, 桥现在会如实回 "stuck" —— 那是新加的诚实回执, 不是回归。
    _orig_interrupt = client.interrupt

    async def _interrupt_and_finish():
        await _orig_interrupt()
        b.ACTIVE.discard(CHAT)
        b.IDLE_ACTIVE.discard(CHAT)
    client.interrupt = _interrupt_and_finish
    b.CLIENTS[CHAT] = client
    b.ACTIVE.discard(CHAT)
    b.IDLE_ACTIVE.discard(CHAT)
    b.INTERRUPTED.discard(CHAT)
    b.COALESCE.pop(CHAT, None)
    if active:
        b.ACTIVE.add(CHAT)
    return sender, front, client


def test_frame_interrupts_active_turn_silently():
    sender, front, client = _setup(active=True)
    b.COALESCE[CHAT] = [("暂存的消息", time.time())]
    asyncio.run(b._app_interrupt())
    assert client.interrupted == 1
    assert CHAT in b.INTERRUPTED
    assert not b.COALESCE.get(CHAT)
    assert sender.texts == []                      # 静默: 聊天里零消息
    # state 表示精确状态；ok 保留给旧客户端。
    assert front.events == [{"ev": "queue_flush"},  # 恢复排队气泡样式
                            {"ev": "interrupted", "ok": True, "state": "stopped",
                             "cleared": 1}]


def test_frame_when_idle_reports_idle():
    sender, front, client = _setup(active=False)
    asyncio.run(b._app_interrupt())
    assert client.interrupted == 0
    assert sender.texts == []
    # ok=False 保留, 但**光看它分不出"本来没在跑"和"掐不动"** —— state 才分得出
    assert front.events == [{"ev": "interrupted", "ok": False, "state": "idle",
                             "cleared": 0}]


def test_text_stop_path_still_replies():
    """「停」文本路语义不变: 出 🛑 到聊天 (飞书用户还靠它)。"""
    sender, front, client = _setup(active=True)
    asyncio.run(b.handle_message(CHAT, "停"))
    assert client.interrupted == 1
    assert any(t.startswith("🛑") for t in sender.texts)


def test_frame_cancels_pending_ask():
    _sender, front, client = _setup(active=True)
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        b.ASK_PENDING[CHAT] = ("ask-1", fut)
        loop.run_until_complete(b._app_interrupt())
        assert fut.done() and fut.result() is None   # 提问被取消
        assert client.interrupted == 1
    finally:
        b.ASK_PENDING.pop(CHAT, None)
        loop.close()


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

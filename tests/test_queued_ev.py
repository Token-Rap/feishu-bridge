"""忙时暂存的结构化排队事件: App 端不发「已暂存」文字, 广播 queued;
飞书保留文字提示 (没有气泡样式可用)。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "queued-ev-test"


class StubFeishu:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, chat, text):
        self.sent.append(text)


class StubSender:
    def __init__(self):
        self.texts: list[str] = []
        self.feishu = StubFeishu()

    def send(self, chat, text):
        self.texts.append(text)

    def send_text(self, chat, text):
        self.texts.append(text)


class StubFront:
    chat_id = CHAT

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


def test_stash_broadcasts_queued_and_keeps_app_silent():
    sender, front = StubSender(), StubFront()
    b.SENDER, b.APP_FRONT = sender, front
    b.ACTIVE.add(CHAT)
    try:
        asyncio.run(b.handle_message(CHAT, "排队的消息"))
        # 服务端时间戳供重连后的客户端计算排队时间。
        # 逐字段断言允许兼容地增加字段。
        q = next((e for e in front.events if e.get("ev") == "queued"), None)
        assert q is not None and q["count"] == 1, front.events
        assert isinstance(q.get("ts"), float) and q["ts"] > 1_700_000_000, q
        assert sender.texts == []                       # App/Tee 通道零文字
        assert any("已暂存" in t for t in sender.feishu.sent)   # 飞书仍有文字提示
        assert [b._queued_parts(x)[0] for x in b.COALESCE[CHAT]] == ["排队的消息"]
    finally:
        b.ACTIVE.discard(CHAT)
        b.COALESCE.pop(CHAT, None)


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

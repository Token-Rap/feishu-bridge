"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("claude_default")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv

from claude_agent_sdk import AssistantMessage, TextBlock  # noqa: E402

CHAT = "ask-cut-test"


class StubSender:
    def __init__(self):
        self.replies: list[str] = []
        self.texts: list[str] = []

    def send_reply(self, chat, text, thinking=None, reply_to=None, extra=None):
        self.replies.append(text)
        return f"mid-{len(self.replies)}"

    def send(self, chat, text):
        self.texts.append(text)

    def send_text(self, chat, text):
        self.texts.append(text)

    def open_stream(self, chat, reply_to=None, summary=""):
        class _S:                       # 建流式卡失败 → run_turn 走整卡兜底路径
            async def start(self):
                return False
        return _S()

    def begin_busy(self, message_id):
        return None

    def end_busy(self, message_id, reaction_id):
        pass


class StubFront:
    chat_id = CHAT

    def broadcast(self, ev):
        pass


class FakeClient:
    """第一次: 前段 → 等 gate (模拟提问卡阻塞) → 后段; 之后永远阻塞
    (模拟流上没动静, 否则轮间监听会反复重放脚本 → 测试挂死)。"""

    def __init__(self, gate: asyncio.Event):
        self._gate = gate
        self._calls = 0

    async def query(self, prompt):
        pass

    async def receive_response(self):
        self._calls += 1
        if self._calls == 1:
            yield AssistantMessage(content=[TextBlock(text="前段内容")], model="test-model")
            await self._gate.wait()
            yield AssistantMessage(content=[TextBlock(text="后段内容")], model="test-model")
        else:
            await asyncio.Event().wait()


def test_ask_cut_splits_turn_into_two_messages():
    async def _run():
        sender = StubSender()
        b.SENDER, b.APP_FRONT = sender, StubFront()
        gate = asyncio.Event()
        b.CLIENTS[CHAT] = FakeClient(gate)
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        task = asyncio.create_task(b.run_turn(CHAT, "测试问题"))
        await asyncio.sleep(0.05)          # 前段先被消费
        b.ASK_CUT.add(CHAT)                # 模拟提问卡已作答 (钩子里打的标记)
        gate.set()
        await task
        await b._stop_idle_watch(CHAT)
        return sender

    sender = asyncio.run(_run())
    assert len(sender.replies) == 2
    assert "前段内容" in sender.replies[0] and "后段内容" not in sender.replies[0]
    assert "后段内容" in sender.replies[1] and "前段内容" not in sender.replies[1]
    assert CHAT not in b.ASK_CUT           # 标记已消费


def test_no_cut_without_flag():
    """没有提问 → 整轮一条消息, 行为不变。"""
    async def _run():
        sender = StubSender()
        b.SENDER, b.APP_FRONT = sender, StubFront()
        gate = asyncio.Event()
        gate.set()                          # 不阻塞
        b.CLIENTS[CHAT] = FakeClient(gate)
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        await b.run_turn(CHAT, "测试问题")
        await b._stop_idle_watch(CHAT)
        return sender

    sender = asyncio.run(_run())
    assert len(sender.replies) == 1
    assert "前段内容" in sender.replies[0] and "后段内容" in sender.replies[0]


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

"""思考过程实时接出 (reasoning 事件)。

要点: ThinkingBlock → 广播 {"ev":"reasoning","text":累积}; 只给 App (不经 SENDER,
飞书无此内容); 思考不混进正文回复。
"""
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
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock  # noqa: E402

CHAT = "reasoning-ev-test"


class StubSender:
    def __init__(self):
        self.replies: list[str] = []
        self.texts: list[str] = []

    def send_reply(self, chat, text, thinking=None, reply_to=None, extra=None):
        self.replies.append(text)
        return "mid-1"

    def send(self, chat, text):
        self.texts.append(text)

    def send_text(self, chat, text):
        self.texts.append(text)

    def open_stream(self, chat, reply_to=None, summary=""):
        class _S:
            async def start(self):
                return False
        return _S()

    def begin_busy(self, message_id):
        return None

    def end_busy(self, message_id, reaction_id):
        pass


class StubFront:
    chat_id = CHAT

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


class FakeClient:
    def __init__(self):
        self._calls = 0

    async def query(self, prompt):
        pass

    async def receive_response(self):
        self._calls += 1
        if self._calls == 1:
            yield AssistantMessage(
                content=[ThinkingBlock(thinking="先想想怎么答", signature=""),
                         TextBlock(text="答案正文")],
                model="test-model")
        else:
            await asyncio.Event().wait()


def test_thinking_block_broadcasts_reasoning():
    async def _run():
        sender, front = StubSender(), StubFront()
        b.SENDER, b.APP_FRONT = sender, front
        b.CLIENTS[CHAT] = FakeClient()
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        await b.run_turn(CHAT, "测试问题")
        await b._stop_idle_watch(CHAT)
        return sender, front

    sender, front = asyncio.run(_run())
    assert {"ev": "reasoning", "text": "先想想怎么答"} in front.events
    assert len(sender.replies) == 1
    assert "先想想怎么答" not in sender.replies[0]     # 思考不混进正文
    assert "答案正文" in sender.replies[0]


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

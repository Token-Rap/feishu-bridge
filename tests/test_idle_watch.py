"""Behavioral regression coverage."""
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

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock  # noqa: E402

CHAT = "idlewatch-test"


class StubSender:
    def __init__(self):
        self.replies: list[str] = []
        self.texts: list[str] = []

    def send_reply(self, chat, text, thinking=None, reply_to=None, extra=None):
        self.replies.append(text)
        return "mid-stub"

    def send_text(self, chat, text):
        self.texts.append(text)

    def send(self, chat, text):
        self.texts.append(text)


class StubFront:
    def __init__(self):
        self.events = []

    def broadcast(self, ev):
        self.events.append(ev)


class FakeClient:
    """第一次 receive_response 按脚本吐消息; 之后阻塞 (模拟流上没动静)。"""

    def __init__(self, script, gate: asyncio.Event | None = None):
        self._script = script          # [(消息, 之前要不要等 gate)]
        self._gate = gate
        self.calls = 0

    async def receive_response(self):
        self.calls += 1
        if self.calls == 1:
            for msg, wait in self._script:
                if wait and self._gate is not None:
                    await self._gate.wait()
                yield msg
        else:
            await asyncio.Event().wait()   # 永远等 (后续没有后台回合)


def _asst(*blocks):
    return AssistantMessage(content=list(blocks), model="test-model")


def _setup(script, gate=None):
    sender, front = StubSender(), StubFront()
    b.SENDER, b.APP_FRONT = sender, front
    b.CLIENTS[CHAT] = FakeClient(script, gate)
    return sender, front


async def _teardown():
    task = b.IDLE_WATCH.pop(CHAT, None)
    if task:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    b.CLIENTS.pop(CHAT, None)
    b.IDLE_STOP.discard(CHAT)
    b.IDLE_ACTIVE.discard(CHAT)


def test_background_turn_pushed_immediately():
    async def run():
        sender, front = _setup([( _asst(ToolUseBlock(id="t1", name="Bash", input={})), False),
                                (_asst(TextBlock(text="雷达跑完了, 今日无新增")), False)])
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.1)
        assert sender.replies and "雷达跑完了" in sender.replies[0]
        assert "后台任务续报" in sender.replies[0]
        assert any(e.get("ev") == "tool" for e in front.events)
        await _teardown()
    asyncio.run(run())


def test_empty_stale_turn_swallowed_silently():
    async def run():
        sender, _ = _setup([])         # 空回合 (陈旧终止符)
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.1)
        assert sender.replies == []    # 不推空卡
        await _teardown()
    asyncio.run(run())


def test_handoff_waits_for_inflight_background_turn():
    async def run():
        gate = asyncio.Event()
        sender, _ = _setup([(_asst(TextBlock(text="第一段")), False),
                            (_asst(TextBlock(text=" 第二段")), True)], gate)
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.1)
        assert CHAT in b.IDLE_ACTIVE          # 播到一半
        stop = asyncio.get_running_loop().create_task(b._stop_idle_watch(CHAT))
        await asyncio.sleep(0.1)
        assert not stop.done()                # 不能硬掐, 要等播完
        assert any("后台回合正在收尾" in t for t in sender.texts)
        gate.set()
        await asyncio.wait_for(stop, 5)
        assert sender.replies and "第一段 第二段" in sender.replies[0]   # 完整送达
        assert CHAT not in b.IDLE_WATCH
        await _teardown()
    asyncio.run(run())


def test_idle_handoff_is_instant_when_quiet():
    async def run():
        _setup([])
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.15)              # 首轮空回合已清扫, 第二次调用在永久等
        await asyncio.wait_for(b._stop_idle_watch(CHAT), 2)   # 空等状态交接必须秒回
        assert CHAT not in b.IDLE_WATCH
        await _teardown()
    asyncio.run(run())


def test_only_model_output_marks_active():
    """Behavioral regression coverage."""
    async def run():
        class Sys:                      # 非 Assistant/StreamEvent 的杂事件
            pass
        _setup([(Sys(), False)])
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.1)
        assert CHAT not in b.IDLE_ACTIVE
        await asyncio.wait_for(b._stop_idle_watch(CHAT), 2)   # 交接必须秒回, 不等
        await _teardown()
    asyncio.run(run())


def test_stale_active_flag_does_not_block_handoff():
    """置位但久无产出 (>90s) = 死回合, 交接直接掐, 不等 180 秒。"""
    async def run():
        _setup([])
        b._start_idle_watch(CHAT)
        await asyncio.sleep(0.05)
        b.IDLE_ACTIVE.add(CHAT)
        b.IDLE_SEEN[CHAT] = 0            # 远古
        await asyncio.wait_for(b._stop_idle_watch(CHAT), 2)
        await _teardown()
    asyncio.run(run())


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

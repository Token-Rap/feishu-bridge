'队列应在每种回合结束路径上按顺序释放。'
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-drain-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "drain-test"


class _Feishu:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, chat, text):
        self.sent.append(text)


class _Sender:
    def __init__(self):
        self.feishu = _Feishu()

    def send(self, chat, text):
        self.feishu.sent.append(text)

    def send_text(self, chat, text):
        self.feishu.sent.append(text)


class _Front:
    chat_id = CHAT

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "inflight_mark", lambda *a, **k: None)
    monkeypatch.setattr(b, "inflight_queue", lambda *a, **k: None)
    monkeypatch.setattr(b, "inflight_clear", lambda *a, **k: None)
    b.ACTIVE.discard(CHAT)
    b.COALESCE.pop(CHAT, None)
    yield
    b.ACTIVE.discard(CHAT)
    b.COALESCE.pop(CHAT, None)


def _bare(prompts) -> list:
    """Strip _stamp metadata; these tests cover order, not message-envelope fields."""
    return ["\n".join(line for line in p.splitlines()
                      if not line.startswith(("[消息时间 ", "[会话路由 ")))
            for p in prompts]


def _spy_turns(monkeypatch) -> list:
    ran: list[str] = []

    async def fake_turn(chat_id, prompt, message_id=None, plan=False):
        with b._busy_guard(chat_id):        # 真 run_turn 会占忙标志, 桩也得占
            ran.append(prompt)
    monkeypatch.setattr(b, "run_turn", fake_turn)
    return ran


# ── 靶心: 走斜杠命令那条路, 队列也要被放出来 ────────────────────────
def test_slash_command_path_drains_the_queue(monkeypatch):
    """Behavioral regression coverage."""
    ran = _spy_turns(monkeypatch)

    async def fake_command(chat_id, text):
        # 模拟 /compact: 它自己跑一个 turn, 期间用户发来一条 (走真排队逻辑)
        with b._busy_guard(chat_id):
            await b.handle_message(chat_id, "compact 期间发的")
        return True
    monkeypatch.setattr(b, "handle_command", fake_command)

    asyncio.run(b.handle_message(CHAT, "/compact"))
    assert _bare(ran) == ["compact 期间发的"], f"命令跑完队列没人放出来: {ran}"
    assert not b.COALESCE.get(CHAT)


def test_the_queued_message_keeps_its_place_in_line(monkeypatch):
    """先到的先跑。用户看到的症状里, **乱序**比晚到更难理解 ——
    他会以为自己那句话被无视了, 因为回答的是后发的那条。"""
    ran = _spy_turns(monkeypatch)

    async def fake_command(chat_id, text):
        with b._busy_guard(chat_id):
            await b.handle_message(chat_id, "先发的")
        return True
    monkeypatch.setattr(b, "handle_command", fake_command)

    asyncio.run(b.handle_message(CHAT, "/compact"))
    asyncio.run(b.handle_message(CHAT, "后发的"))
    assert _bare(ran) == ["先发的", "后发的"], f"顺序反了: {ran}"


# ── drain 自己的性质 ────────────────────────────────────────────────
def test_drain_holds_the_busy_flag(monkeypatch):
    """drain 期间必须占着忙标志, 否则这时进来的消息会**插到队列前面**去 ——
    那正好是这次要修的症状换个形态再来一遍。"""
    seen: list[bool] = []

    async def fake_turn(chat_id, prompt, message_id=None, plan=False):
        seen.append(chat_id in b.ACTIVE)
    monkeypatch.setattr(b, "run_turn", fake_turn)

    b.COALESCE[CHAT] = [("排着的", 1.0)]
    asyncio.run(b._drain_coalesce(CHAT))
    assert seen == [True], "drain 期间忙标志没占住"


def test_drain_on_empty_queue_is_a_noop(monkeypatch):
    """Behavioral regression coverage."""
    ran = _spy_turns(monkeypatch)
    asyncio.run(b._drain_coalesce(CHAT))
    assert ran == []


def test_drain_clears_the_queue_even_if_a_turn_blows_up(monkeypatch):
    """一条炸了不能让剩下的永远躺在内存里 (那就成了"消息丢了"的真形态)。"""
    async def boom(chat_id, prompt, message_id=None, plan=False):
        raise RuntimeError("boom")
    monkeypatch.setattr(b, "run_turn", boom)

    b.COALESCE[CHAT] = [("会炸的", 1.0)]
    with pytest.raises(RuntimeError):
        asyncio.run(b._drain_coalesce(CHAT))
    assert not b.COALESCE.get(CHAT)


# ── 回归: 原来那条路 (第 6 步) 照旧 ─────────────────────────────────
def test_normal_path_still_drains(monkeypatch):
    """别为了修第 5 步把第 6 步弄坏了 —— 那条路是绝大多数消息走的。"""
    ran = _spy_turns(monkeypatch)

    async def _second(chat_id, prompt, message_id=None, plan=False):
        with b._busy_guard(chat_id):
            ran.append(prompt)
            if prompt.endswith("第一条"):
                await b.handle_message(chat_id, "忙时发的")
    monkeypatch.setattr(b, "run_turn", _second)

    asyncio.run(b.handle_message(CHAT, "第一条"))
    assert _bare(ran) == ["第一条", "忙时发的"]

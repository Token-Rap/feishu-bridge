from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-steer-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "steer-test"


class _Feishu:
    def __init__(self):
        self.sent = []

    def send(self, _chat, text):
        self.sent.append(text)


class _Sender:
    def __init__(self):
        self.feishu = _Feishu()


class _Front:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    b.ACTIVE.add(CHAT)
    b.COALESCE.pop(CHAT, None)
    b.CLAUDE_STEERABLE.discard(CHAT)
    b.CODEX_TURNS.pop(CHAT, None)
    b.STEER_LOCKS.pop(CHAT, None)
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "inflight_queue", lambda *_args: None)
    yield
    b.ACTIVE.discard(CHAT)
    b.COALESCE.pop(CHAT, None)
    b.CLAUDE_STEERABLE.discard(CHAT)
    b.CODEX_TURNS.pop(CHAT, None)
    b.STEER_LOCKS.pop(CHAT, None)


def test_claude_busy_message_is_steered_into_current_turn(monkeypatch):
    queries = []

    class _Client:
        async def query(self, text):
            queries.append(text)

    b.CLIENTS[CHAT] = _Client()
    b.CLAUDE_STEERABLE.add(CHAT)
    try:
        asyncio.run(b.handle_message(CHAT, "补充条件"))
    finally:
        b.CLIENTS.pop(CHAT, None)

    assert len(queries) == 1 and queries[0].endswith("补充条件")
    assert CHAT not in b.COALESCE
    assert [e["ev"] for e in b.APP_FRONT.events] == ["queued", "queue_accept", "queue_flush"]
    assert b.APP_FRONT.events[1]["count"] == 1


def test_codex_busy_message_uses_turn_steer():
    steered = []

    class _Turn:
        async def steer(self, text):
            steered.append(text)

    b.CODEX_TURNS[CHAT] = _Turn()
    asyncio.run(b.handle_message(CHAT, "改成先跑测试"))

    assert len(steered) == 1 and steered[0].endswith("改成先跑测试")
    assert CHAT not in b.COALESCE
    assert [e["ev"] for e in b.APP_FRONT.events] == ["queued", "queue_accept", "queue_flush"]


def test_accept_only_covers_the_snapshot_and_leaves_later_message_queued():
    """steer 等待 provider 时仍可能来新消息；accept 只能认自己送进去的前缀。"""
    class _Turn:
        async def steer(self, _text):
            b.COALESCE[CHAT].append(("后到的", 2.0, CHAT))

    b.CODEX_TURNS[CHAT] = _Turn()
    asyncio.run(b.handle_message(CHAT, "先到的"))

    assert [b._queued_parts(x)[0] for x in b.COALESCE[CHAT]] == ["后到的"]
    assert [e["ev"] for e in b.APP_FRONT.events] == ["queued", "queue_accept"]
    assert b.APP_FRONT.events[-1]["count"] == 1


def test_internal_group_task_steers_silently_without_queue_ui_events():
    class _Turn:
        async def steer(self, _text):
            return None

    b.CODEX_TURNS[CHAT] = _Turn()
    tok = b.app_frontend.INTERNAL_GROUP_TASK.set(True)
    try:
        asyncio.run(b.handle_message(CHAT, "内部 brief"))
    finally:
        b.app_frontend.INTERNAL_GROUP_TASK.reset(tok)

    assert CHAT not in b.COALESCE
    assert b.APP_FRONT.events == []


def test_control_command_waits_for_post_turn_drain():
    b.CLAUDE_STEERABLE.add(CHAT)
    asyncio.run(b.handle_message(CHAT, "/compact"))

    assert [b._queued_parts(x)[0] for x in b.COALESCE[CHAT]] == ["/compact"]
    assert not any(e.get("ev") == "queue_flush" for e in b.APP_FRONT.events)


def test_failed_steering_keeps_message_in_queue():
    class _Turn:
        async def steer(self, _text):
            raise RuntimeError("not steerable")

    b.CODEX_TURNS[CHAT] = _Turn()
    asyncio.run(b.handle_message(CHAT, "别改配置"))

    assert [b._queued_parts(x)[0] for x in b.COALESCE[CHAT]] == ["别改配置"]

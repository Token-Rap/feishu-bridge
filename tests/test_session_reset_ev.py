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

CHAT = "session-reset-test"


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


def _wire(monkeypatch, tmp_path):
    sender, front = StubSender(), StubFront()
    monkeypatch.setattr(b, "SENDER", sender)
    monkeypatch.setattr(b, "APP_FRONT", front)
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", tmp_path / "sessions.json")

    async def _noop_drop(chat_id):
        return None
    monkeypatch.setattr(b, "_drop_client", _noop_drop)
    return sender, front


def test_app_gets_no_chat_text(monkeypatch, tmp_path):
    """靶心: App/Tee 通道一个字都不发。"""
    sender, _front = _wire(monkeypatch, tmp_path)
    asyncio.run(b._new_session(CHAT))
    assert sender.texts == [], sender.texts


def test_feishu_keeps_the_text(monkeypatch, tmp_path):
    """飞书没有分隔线这种 UI, 文字是它唯一的表达方式。"""
    sender, _front = _wire(monkeypatch, tmp_path)
    asyncio.run(b._new_session(CHAT))
    assert any("已开新会话" in t for t in sender.feishu.sent), sender.feishu.sent


def test_broadcasts_structured_event(monkeypatch, tmp_path):
    """删文字的同时必须给出等价信号 —— 否则 App 端就是静默。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    asyncio.run(b._new_session(CHAT))
    ev = next((e for e in front.events if e.get("ev") == "session_reset"), None)
    assert ev is not None, front.events
    assert isinstance(ev.get("ts"), float) and ev["ts"] > 1_700_000_000, ev


def test_event_carries_the_previous_session_id(monkeypatch, tmp_path):
    """「旧会话仍可 /resume」这条信息不能跟着文字一起丢 —— 它换成字段给客户端做入口。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    b._record_session(CHAT, "sid-old-123456", preview="上一个会话")
    asyncio.run(b._new_session(CHAT))
    ev = next(e for e in front.events if e.get("ev") == "session_reset")
    assert ev["prev"] == "sid-old-123456", ev


def test_prev_is_empty_when_there_was_none(monkeypatch, tmp_path):
    """从没跑过任何会话时也要能开新会话, prev 给空串而不是 None/崩掉。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    asyncio.run(b._new_session(CHAT))
    ev = next(e for e in front.events if e.get("ev") == "session_reset")
    assert ev["prev"] == "", ev


def test_active_session_is_actually_cleared(monkeypatch, tmp_path):
    """事件发了但 active 没清 = 骗人的状态信号, 比不发更糟。"""
    _sender, _front = _wire(monkeypatch, tmp_path)
    b._record_session(CHAT, "sid-old-123456", preview="上一个会话")
    asyncio.run(b._new_session(CHAT))
    assert b._load_session(CHAT) is None


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

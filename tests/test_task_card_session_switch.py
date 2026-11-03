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

CHAT = "task-switch-test"


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
    monkeypatch.setattr(b, "_TASK_TRACK_FILE", tmp_path / "task_track.json")

    async def _noop_drop(chat_id):
        return None
    monkeypatch.setattr(b, "_drop_client", _noop_drop)
    # /resume 会先查 transcript 文件在不在 (真环境是 ~/.claude/projects 下的 jsonl),
    # 测试里没有 → 会走「记录已不在」提前 return, 根本走不到换会话那步。
    monkeypatch.setattr(b, "_session_exists", lambda sid: True)
    b._TASK_TRACK.clear()
    return sender, front


def _todos_events(front):
    return [e for e in front.events if e.get("ev") == "todos"]


def _seed_old_session(tasks=3):
    """模拟旧会话里攒下的清单。"""
    b._record_session(CHAT, "sid-old-d2e51433", preview="旧会话")
    items = b._session_tasks(CHAT)
    for i in range(tasks):
        items.append({"id": str(i + 1), "content": f"第 {i+1} 条", "activeForm": "", "status": "completed"})
    return items


def test_new_session_clears_the_card(monkeypatch, tmp_path):
    """靶心: 开新会话后广播一次空清单, 卡当场收掉。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    _seed_old_session(11)
    front.events.clear()
    asyncio.run(b._new_session(CHAT))
    evs = _todos_events(front)
    assert evs, front.events
    assert evs[-1]["items"] == [], evs[-1]


def test_new_session_clears_memory_and_disk(monkeypatch, tmp_path):
    """光广播不落盘 = 桥接一重启 11 条又回来了。"""
    _sender, _front = _wire(monkeypatch, tmp_path)
    _seed_old_session(11)
    asyncio.run(b._new_session(CHAT))
    assert b._session_tasks(CHAT) == []
    import json
    saved = json.loads((tmp_path / "task_track.json").read_text(encoding="utf-8"))
    assert saved.get(CHAT, {}).get("items") == [], saved


def test_resume_also_clears_the_card(monkeypatch, tmp_path):
    """/resume 也是换会话 —— 同一个形状, 不能只修 /new。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    _seed_old_session(4)
    b._record_session(CHAT, "sid-other-99999999", preview="另一个会话")
    b._set_active(CHAT, "sid-old-d2e51433")     # 先回到旧的, 让 /resume 有得切
    front.events.clear()
    assert asyncio.run(b.handle_command(CHAT, "/resume sid-other-99999999")) is True
    evs = _todos_events(front)
    assert evs and evs[-1]["items"] == [], front.events


def test_session_tasks_self_heals_on_sid_change(monkeypatch, tmp_path):
    """兜底: 任何路径走到 _session_tasks 都该自愈 —— 免得将来加入口时又漏一个。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    _seed_old_session(5)
    front.events.clear()
    b._clear_session(CHAT)          # 绕过 _new_session, 只改 active
    assert b._session_tasks(CHAT) == []
    assert _todos_events(front) and _todos_events(front)[-1]["items"] == []


def test_no_broadcast_when_session_is_unchanged(monkeypatch, tmp_path):
    """sid 没变就别播 —— 采集循环里每条 Task* 都会调它, 多播就是自造噪音。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    _seed_old_session(2)
    front.events.clear()
    b._session_tasks(CHAT)
    b._session_tasks(CHAT)
    assert _todos_events(front) == [], front.events


def test_first_ever_session_does_not_broadcast(monkeypatch, tmp_path):
    """从没有过清单的 chat 第一次被问到时别播空清单 (等于开机噪音)。"""
    _sender, front = _wire(monkeypatch, tmp_path)
    b._session_tasks(CHAT)
    assert _todos_events(front) == [], front.events


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

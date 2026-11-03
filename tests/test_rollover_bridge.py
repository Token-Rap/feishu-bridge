"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-rollover-")]
try:
    import app_frontend
    import fleet_bridge as b
finally:
    sys.argv = _argv

SID = "11111111-2222-3333-4444-555555555555"


def _night(hour=3):
    return time.mktime((2026, 1, 15, hour, 10, 0, 0, 0, -1))


class _Clock:
    """One test clock for the scheduler and its real-time safety recheck."""
    now = _night()

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _write_note(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    # The fake writer's filesystem timestamp must use the same clock as started.
    os.utime(path, (b.time.time(), b.time.time()))


class _Front:
    chat_id = "app"

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append({**ev, "chat_id": app_frontend.DISPLAY_CHAT.get() or self.chat_id})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "time", _Clock())
    home, projects = tmp_path / "home", tmp_path / "projects" / "-repo"
    home.mkdir()
    projects.mkdir(parents=True)
    monkeypatch.setattr(b, "_BRIDGE_HOME", home)
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", home / "sessions.json")
    monkeypatch.setattr(b._fs, "PROJECTS_DIR", projects.parent)
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    lines: list[str] = []
    monkeypatch.setattr(b, "log", lines.append)
    monkeypatch.setattr(b, "_read_runtime", lambda: {})
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("m", "high", "claude"))

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(b, "_drop_client", _noop)
    drained: list[str] = []

    async def _drain(key):
        drained.append(key)
        b.COALESCE.pop(key, None)
    monkeypatch.setattr(b, "_drain_coalesce", _drain)
    calls: list[str] = []

    async def _note(key, sid, path, compacts):
        calls.append(key)
        _write_note(path, "交接笔记 " * 60)
        return ""
    monkeypatch.setattr(b, "_rollover_write_note", _note)
    b._ROLLOVER_TRIED.clear()
    b._ROLLOVER_SKIPPED.clear()
    for s in (b.ACTIVE, b.IDLE_ACTIVE, b.ROLLOVER_HOLD):
        s.clear()
    b.COALESCE.clear()
    b.ASK_PENDING.clear()

    def add_session(key="app", compacts=3, idle_s=7200, sid=SID):
        t = projects / f"{sid}.jsonl"
        t.write_text("".join(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
                             for _ in range(compacts)), encoding="utf-8")
        old = _night() - idle_s
        os.utime(t, (old, old))
        b._record_session(key, sid)
        return t
    return {"home": home, "lines": lines, "calls": calls, "drained": drained,
            "add": add_session, "monkeypatch": monkeypatch}


def _tick(hour=3):
    b.time.now = _night(hour)
    asyncio.run(b._rollover_tick(b.time.time()))


def test_rolls_over_a_quiet_session_with_enough_compacts(env):
    env["add"]()
    _tick()
    assert env["calls"] == ["app"]
    assert b._load_session("app") is None, "旧会话应被搁下 (仍可 /resume)"
    ev = [e for e in b.APP_FRONT.events if e.get("ev") == "session_reset"]
    assert ev and ev[0]["hint"] == "auto_rollover" and ev[0]["prev"] == SID
    assert not [e for e in b.APP_FRONT.events if e.get("ev") in ("reply", "stream", "notice")]
    assert any("🔁 auto-rollover: 3 compacts" in s for s in env["lines"]), env["lines"]


def test_group_session_divider_lands_in_the_group(env):
    env["add"](key="group:ops")
    _tick()
    ev = [e for e in b.APP_FRONT.events if e.get("ev") == "session_reset"]
    assert ev and ev[0]["chat_id"] == "group:ops"


@pytest.mark.parametrize("idle_s,rolls", [(3599, False), (3600, True), (3601, True)])
def test_quiet_boundary_uses_the_same_clock_at_both_checks(env, idle_s, rolls):
    env["add"](idle_s=idle_s)
    seen = []
    original = b._rollover_unsafe

    def observe(key, transcript, now):
        seen.append(now)
        return original(key, transcript, now)

    env["monkeypatch"].setattr(b, "_rollover_unsafe", observe)
    _tick()
    assert seen == [_night()] * (2 if rolls else 1)
    assert env["calls"] == (["app"] if rolls else [])
    assert b._load_session("app") == (None if rolls else SID)


@pytest.mark.parametrize("hour", [1, 6, 14])
def test_nothing_outside_the_night_window(env, hour):
    env["add"]()
    _tick(hour)
    assert env["calls"] == [] and b._load_session("app") == SID


def test_below_n_compacts_is_left_alone(env):
    env["add"](compacts=2)
    _tick()
    assert env["calls"] == []


def test_opt_out_and_override(env):
    env["add"]()
    env["monkeypatch"].setattr(b, "_read_runtime", lambda: {"auto_rollover": False})
    _tick()
    assert env["calls"] == []
    env["monkeypatch"].setattr(b, "_read_runtime", lambda: {"auto_rollover_compacts": 4})
    _tick()
    assert env["calls"] == [], "N=4 而只有 3 次"


@pytest.mark.parametrize("busy", ["active", "ask", "queued", "recent"])
def test_unsafe_sessions_wait_and_say_why_once(env, busy):
    env["add"](idle_s=600 if busy == "recent" else 7200)
    if busy == "active":
        b.ACTIVE.add("app")
    elif busy == "ask":
        b.ASK_PENDING["app"] = ("a1", None)
    elif busy == "queued":
        b.COALESCE["app"] = [("hi", 1.0)]
    _tick()
    _tick()
    assert env["calls"] == [] and b._load_session("app") == SID
    assert len([s for s in env["lines"] if "auto-rollover waits" in s]) == 1


def test_failed_note_keeps_the_session_tells_the_user_and_waits_a_night(env):
    env["add"]()

    async def _fail(*_a):
        env["calls"].append("fail")
        return "note not written"
    env["monkeypatch"].setattr(b, "_rollover_write_note", _fail)
    _tick()
    _tick()
    assert env["calls"] == ["fail"], "同一晚不重试"
    assert b._load_session("app") == SID
    notices = [e for e in b.APP_FRONT.events if e.get("ev") == "notice"]
    assert notices and "没成功" in notices[0]["text"]
    assert not [e for e in b.APP_FRONT.events if e.get("ev") == "session_reset"]


def test_activity_during_the_note_aborts(env):
    t = env["add"]()

    async def _note_then_user_speaks(key, sid, path, compacts):
        _write_note(path, "x" * 500)
        os.utime(t, (b.time.time(), b.time.time()))  # original transcript moved on
        return ""
    env["monkeypatch"].setattr(b, "_rollover_write_note", _note_then_user_speaks)
    _tick()
    assert b._load_session("app") == SID


def test_messages_that_arrive_during_the_note_are_drained_after(env):
    env["add"]()

    async def _note(key, sid, path, compacts):
        assert key in b.ROLLOVER_HOLD, "写笔记期间必须挂着 hold, 入站才会排队"
        b.COALESCE[key] = [("用户半夜发的", 1.0)]
        _write_note(path, "x" * 500)
        return ""
    env["monkeypatch"].setattr(b, "_rollover_write_note", _note)
    _tick()
    assert env["drained"] == ["app"] and "app" not in b.ROLLOVER_HOLD


def test_first_turn_of_the_new_session_is_told_where_the_note_is(env):
    env["add"]()
    _tick()
    first = b._rollover_first_turn("app", "[消息时间 x]\n早")
    assert "交接笔记" in first and str(b._ro.note_path(env["home"], "app")) in first
    assert first.endswith("早")
    assert b._rollover_first_turn("app", "第二条") == "第二条", "只提示一次"
    assert not [s for s in env["lines"] if "hint skipped" in s], "平常每一轮都不该留一行报错"


def test_going_back_to_the_old_session_drops_the_hint(env):
    env["add"]()
    _tick()
    b._record_session("app", SID)                 # user did /resume
    assert b._rollover_first_turn("app", "hi") == "hi"
    b._clear_session("app")
    assert b._rollover_first_turn("app", "hi") == "hi", "提示已作废, 不会在以后某个新会话里冒出来"


def test_codex_engine_is_skipped(env):
    env["add"]()
    env["monkeypatch"].setattr(b, "_runtime_snapshot", lambda: ("gpt-6", "high", "codex"))
    _tick()
    assert env["calls"] == []


def test_a_broken_tick_cannot_kill_the_loop(env, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(b, "_rollover_tick", _boom)
    monkeypatch.setattr(b, "ROLLOVER_TICK_S", 0)

    async def run():
        task = asyncio.create_task(b._rollover_loop())
        await asyncio.sleep(0.05)
        alive = not task.done()
        task.cancel()
        return alive
    assert asyncio.run(run())

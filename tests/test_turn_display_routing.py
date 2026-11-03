"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import contextvars
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-display-")]
try:
    from feishu_bridge.bridge import app_frontend
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv

WORK = "oc_private_work_chat"
GROUP = "group:ops"


class _Feishu:
    def send(self, *_a, **_k):
        return None

    def update_card(self, *_a, **_k):
        return None


class _Sender:
    def __init__(self):
        self.feishu = _Feishu()

    def send(self, *_a, **_k):
        return None


class _Front:
    chat_id = WORK

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


class _Client:
    def __init__(self):
        self.queries: list[str] = []

    async def query(self, text):
        self.queries.append(text)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "inflight_queue", lambda *a, **k: None)
    monkeypatch.setattr(b, "log", lambda *_a, **_k: None)
    for s in (b.ACTIVE, b.CLAUDE_STEERABLE):
        s.discard(WORK)
    b.COALESCE.pop(WORK, None)
    b.CLIENTS.pop(WORK, None)
    yield
    for s in (b.ACTIVE, b.CLAUDE_STEERABLE):
        s.discard(WORK)
    b.COALESCE.pop(WORK, None)
    b.CLIENTS.pop(WORK, None)
    b.TURN_DISPLAY.pop(WORK, None)


def _in_display(display, coro_fn):
    """Run coro_fn inside a fresh context whose DISPLAY_CHAT is `display` (like a real frame)."""
    async def runner():
        app_frontend.DISPLAY_CHAT.set(display)
        return await coro_fn()
    return asyncio.run(runner())


# ── run_turn registers the turn's display where non-inheriting callers can read it ──
def test_run_turn_registers_its_display_and_clears_it(monkeypatch):
    seen = []

    async def inner(chat_id, *_a, **_k):
        seen.append(b.TURN_DISPLAY.get(chat_id))
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("m", "high", ""))
    monkeypatch.setattr(b, "_run_turn_inner", inner)
    _in_display(GROUP, lambda: b.run_turn(WORK, "hi"))
    assert seen == [GROUP]
    assert WORK not in b.TURN_DISPLAY


# ── 例 2: the ask hook runs in a context the turn didn't create ──
def test_ask_card_lands_where_the_turn_is_shown_not_where_the_sdk_task_was_born(monkeypatch):
    monkeypatch.setattr(b, "MAIN_LOOP", None)
    b.TURN_DISPLAY[WORK] = GROUP                       # the turn: triggered from the group
    hook = b.make_ask_hook(WORK)
    qs = [{"question": "q?", "header": "h", "options": [{"label": "a"}, {"label": "b"}]}]

    async def call_hook_from_sdk_task():
        # the SDK's reader task was born during an earlier *private* turn
        app_frontend.DISPLAY_CHAT.set(WORK)
        b.MAIN_LOOP = asyncio.get_running_loop()
        task = asyncio.create_task(hook({"tool_input": {"questions": qs}}, "t1", None),
                                   context=contextvars.copy_context())
        while WORK not in b.ASK_PENDING:
            await asyncio.sleep(0)
        b.ASK_PENDING[WORK][1].set_result("a")
        return await task
    asyncio.run(call_hook_from_sdk_task())
    asks = [e for e in b.APP_FRONT.events if e.get("ev") == "ask"]
    assert [e["chat_id"] for e in asks] == [GROUP], b.APP_FRONT.events
    cancels = [e for e in b.APP_FRONT.events if e.get("ev") == "ask_cancel"]
    assert cancels and all(e["chat_id"] == GROUP for e in cancels)


# ── 例 1: steering must not pull another display's message into this turn ──
def test_group_message_is_not_steered_into_a_private_turn():
    client = _Client()
    b.CLIENTS[WORK] = client
    b.CLAUDE_STEERABLE.add(WORK)
    b.TURN_DISPLAY[WORK] = WORK                        # private turn running
    b.COALESCE[WORK] = [("群里发的", 1.0, GROUP, True)]
    took = asyncio.run(b._steer_pending(WORK))
    assert took is False and client.queries == []
    assert b.COALESCE[WORK] == [("群里发的", 1.0, GROUP, True)], "未并轮的消息必须留在队列里"


def test_same_display_message_is_still_steered():
    """回归: 同落点的补充照旧并进当前回合 —— 那是 steering 存在的理由。"""
    client = _Client()
    b.CLIENTS[WORK] = client
    b.CLAUDE_STEERABLE.add(WORK)
    b.TURN_DISPLAY[WORK] = GROUP
    b.COALESCE[WORK] = [("群里补一句", 1.0, GROUP, True)]
    assert asyncio.run(b._steer_pending(WORK)) is True
    assert len(client.queries) == 1 and not b.COALESCE.get(WORK)


def test_legacy_items_without_display_count_as_the_work_chat():
    client = _Client()
    b.CLIENTS[WORK] = client
    b.CLAUDE_STEERABLE.add(WORK)
    b.TURN_DISPLAY[WORK] = WORK
    b.COALESCE[WORK] = [("老格式", 1.0)]
    assert asyncio.run(b._steer_pending(WORK)) is True


# ── drain: each queued item runs under its own display ──
def test_drain_runs_each_item_under_its_own_display(monkeypatch):
    ran: list[tuple[str, str]] = []

    async def fake_turn(chat_id, prompt, message_id=None, plan=False):
        with b._busy_guard(chat_id):
            body = [ln for ln in prompt.splitlines()
                    if not ln.startswith(("[消息时间 ", "[会话路由 "))]
            ran.append(("|".join(body), app_frontend.DISPLAY_CHAT.get()))
    monkeypatch.setattr(b, "run_turn", fake_turn)
    b.COALESCE[WORK] = [("私聊一", 1.0, WORK, True), ("群一", 2.0, GROUP, True),
                        ("群二", 3.0, GROUP, True), ("私聊二", 4.0, WORK, True)]
    _in_display(WORK, lambda: b._drain_coalesce(WORK))
    assert ran == [("私聊一", WORK), ("群一||群二", GROUP), ("私聊二", WORK)], ran


def test_drain_keeps_context_for_items_without_display(monkeypatch):
    ran: list[str] = []

    async def fake_turn(chat_id, prompt, message_id=None, plan=False):
        ran.append(app_frontend.DISPLAY_CHAT.get())
    monkeypatch.setattr(b, "run_turn", fake_turn)
    b.COALESCE[WORK] = [("老格式", 1.0)]
    _in_display(GROUP, lambda: b._drain_coalesce(WORK))
    assert ran == [GROUP]


def test_drain_flushes_the_queued_bubbles_in_every_display_it_touches(monkeypatch):
    """群消息改成留到 drain 之后, 这条是它的副作用守门: drain 跑在上一轮 (私聊) 的
    context 里, 靠 contextvar 的 queue_flush 只会清私聊 ⇒ 群里那条气泡永远半透明。"""
    async def fake_turn(chat_id, prompt, message_id=None, plan=False):
        return None
    monkeypatch.setattr(b, "run_turn", fake_turn)
    b.COALESCE[WORK] = [("群一", 1.0, GROUP, True), ("私聊", 2.0, WORK, True),
                        ("看不见的", 3.0, "group:other", False)]
    _in_display(WORK, lambda: b._drain_coalesce(WORK))
    flushed = [e["chat_id"] for e in b.APP_FRONT.events if e.get("ev") == "queue_flush"]
    assert flushed == [GROUP, WORK], b.APP_FRONT.events


def test_steer_accept_is_stamped_with_the_turn_display():
    client = _Client()
    b.CLIENTS[WORK] = client
    b.CLAUDE_STEERABLE.add(WORK)
    b.TURN_DISPLAY[WORK] = GROUP
    b.COALESCE[WORK] = [("群里补一句", 1.0, GROUP, True)]
    _in_display(WORK, lambda: b._steer_pending(WORK))     # caller context deliberately wrong
    evs = [e for e in b.APP_FRONT.events if e.get("ev") in ("queue_accept", "queue_flush")]
    assert evs and all(e["chat_id"] == GROUP for e in evs), evs

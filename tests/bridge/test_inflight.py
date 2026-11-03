"""Behavioral regression coverage."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-inflight-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "_INFLIGHT_FILE", tmp_path / "inflight.json")
    yield


def _raw() -> dict:
    return json.loads(b._INFLIGHT_FILE.read_text(encoding="utf-8"))


# ── 写 / 清 ────────────────────────────────────────────────────────
def test_mark_records_chat_and_prompt():
    b.inflight_mark("chat1", "把日报发给我")
    e = _raw()["chat1"]
    assert e["text"] == "把日报发给我"
    assert e["ts"] > 0


def test_clear_removes_only_that_chat():
    b.inflight_mark("chat1", "活 A")
    b.inflight_mark("chat2", "活 B")
    b.inflight_clear("chat1")
    assert list(_raw()) == ["chat2"]


def test_file_gone_when_last_chat_cleared():
    """没有未完成回合时不该留一个空文件 —— 残留文件本身就是判据。"""
    b.inflight_mark("chat1", "活")
    b.inflight_clear("chat1")
    assert not b._INFLIGHT_FILE.exists()


def test_clear_unknown_chat_is_noop():
    b.inflight_clear("从没跑过")          # 不抛
    assert not b._INFLIGHT_FILE.exists()


def test_queued_messages_recorded():
    """排队消息是纯内存 (COALESCE), 重启即蒸发 —— 一起落盘。"""
    b.inflight_mark("chat1", "正在跑的活")
    b.inflight_queue("chat1", ["排队1", "排队2"])
    assert _raw()["chat1"]["queued"] == ["排队1", "排队2"]


def test_queue_on_unknown_chat_does_not_create_entry():
    """没有在跑的回合就不该有 inflight 条目 (排队只在 ACTIVE 时发生)。"""
    b.inflight_queue("chat1", ["x"])
    assert not b._INFLIGHT_FILE.exists()


# ── 启动时取残留 ───────────────────────────────────────────────────
def test_take_pending_returns_and_deletes():
    """取走即删: 提示只发一次, 不然每次重启都再提示同一件事。"""
    b.inflight_mark("chat1", "被打断的活")
    got = b.inflight_take_pending()
    assert got["chat1"]["text"] == "被打断的活"
    assert not b._INFLIGHT_FILE.exists()
    assert b.inflight_take_pending() == {}


def test_take_pending_empty_when_no_file():
    assert b.inflight_take_pending() == {}


def test_corrupt_file_does_not_raise():
    """半截写入 (正好断电在写的时候) 不能让进程起不来。"""
    b._INFLIGHT_FILE.write_text("{坏的", encoding="utf-8")
    assert b.inflight_take_pending() == {}
    assert not b._INFLIGHT_FILE.exists()      # 坏文件顺手清掉, 别每次启动都撞


# ── fail-soft: 落盘出问题不许影响回合 ───────────────────────────────
def test_mark_never_raises_even_if_unwritable(monkeypatch, tmp_path):
    monkeypatch.setattr(b, "_INFLIGHT_FILE", tmp_path / "no-such-dir" / "x.json")
    b.inflight_mark("chat1", "活")            # 目录不存在 → 不抛
    b.inflight_clear("chat1")


def test_prompt_is_truncated():
    """原始输入可能很长 (贴日志/贴代码), 提示里放不下也没必要。"""
    b.inflight_mark("chat1", "x" * 5000)
    assert len(_raw()["chat1"]["text"]) <= b._INFLIGHT_TEXT_MAX


# ── 提示文案 ───────────────────────────────────────────────────────
def test_notice_names_the_work_and_does_not_promise_rerun():
    """**不能**说「已恢复」「正在继续」—— 我们没有重跑, 说了就是第二个假成功信号。"""
    txt = b.inflight_notice({"text": "把日报发给我", "ts": 1.0, "queued": []})
    assert "把日报发给我" in txt
    assert "被打断" in txt
    for forbidden in ("已恢复", "正在继续", "已重新执行"):
        assert forbidden not in txt


def test_notice_mentions_queued_count():
    txt = b.inflight_notice({"text": "活", "ts": 1.0, "queued": ["a", "b"]})
    assert "2" in txt and "排队" in txt


# ── 集成: 真的挂在回合生命周期上了吗 ────────────────────────────────
# 上面测的是三个函数本身。这一组测**挂点** —— 函数写对了但没接上, 表现和没做一模一样。
import asyncio  # noqa: E402

CHAT = "inflight-chat"
_REAL_SLEEP = asyncio.sleep    # patch 前存好, 见 _nosleep


class _StubFeishu:
    def __init__(self): self.sent = []
    def send(self, chat, text, *a, **k): self.sent.append(text)


class _StubSender:
    def __init__(self): self.feishu, self.texts = _StubFeishu(), []
    def send(self, chat, text, *a, **k): self.texts.append(text)


class _StubFront:
    chat_id = CHAT
    def __init__(self): self.events = []
    def broadcast(self, ev): self.events.append(ev)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(b, "SENDER", _StubSender())
    monkeypatch.setattr(b, "APP_FRONT", _StubFront())
    b.ACTIVE.discard(CHAT); b.COALESCE.pop(CHAT, None)
    yield b.SENDER
    b.ACTIVE.discard(CHAT); b.COALESCE.pop(CHAT, None)


def test_turn_marks_then_clears(wired, monkeypatch):
    """跑一轮: 期间文件在 (被杀就留得下), 跑完文件没了 (不会假报打断)。"""
    seen = {}

    async def _fake_turn(chat, prompt, mid=None, **k):
        seen["during"] = _raw()          # 回合进行中的盘上状态
    monkeypatch.setattr(b, "run_turn", _fake_turn)
    asyncio.run(b.handle_message(CHAT, "把日报发给我"))
    assert seen["during"][CHAT]["text"] == "把日报发给我"
    assert not b._INFLIGHT_FILE.exists()          # finally 收干净


def test_turn_crash_still_clears(wired, monkeypatch):
    """回合抛异常 —— finally 照样收尾, 不留假残留 (漏一条出口就是天天狼来了)。"""
    async def _boom(chat, prompt, mid=None, **k):
        raise RuntimeError("炸了")
    monkeypatch.setattr(b, "run_turn", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(b.handle_message(CHAT, "会炸的活"))
    assert not b._INFLIGHT_FILE.exists()


def test_queued_messages_land_on_disk(wired):
    """忙时来的消息进 COALESCE (纯内存) —— 必须同时落盘, 否则重启照样蒸发。"""
    b.ACTIVE.add(CHAT)                    # 假装正忙
    b.inflight_mark(CHAT, "正在跑的活")
    asyncio.run(b.handle_message(CHAT, "排队的消息"))
    assert _raw()[CHAT]["queued"] == ["排队的消息"]


def test_reset_leaves_no_residue(wired):
    """/reset 在进 ACTIVE 前就 return —— 它压根不该产生 inflight 条目。"""
    asyncio.run(b.handle_message(CHAT, "/reset"))
    assert not b._INFLIGHT_FILE.exists()


def test_startup_reports_to_the_recorded_chat(wired, monkeypatch):
    """启动上报: 发回**记录里那个会话**, 并且取走即删 (同一件事只说一次)。"""
    monkeypatch.setattr(b.asyncio, "sleep", lambda *_a: _REAL_SLEEP(0))
    b.inflight_mark("群A", "群A 的活")
    b.inflight_mark("群B", "群B 的活")
    asyncio.run(b._report_interrupted_turns())
    sent = wired.texts
    assert len(sent) == 2
    assert any("群A 的活" in t for t in sent) and any("群B 的活" in t for t in sent)
    assert not b._INFLIGHT_FILE.exists()


def test_startup_silent_when_nothing_pending(wired, monkeypatch):
    """没有残留就一个字都不说 —— 86% 的重启是空闲时的, 这里多话就是天天狼来了。"""
    monkeypatch.setattr(b.asyncio, "sleep", lambda *_a: _REAL_SLEEP(0))
    asyncio.run(b._report_interrupted_turns())
    assert wired.texts == []


def test_one_failing_chat_does_not_block_others(wired, monkeypatch):
    """一个会话发失败 (群没了/凭证过期), 别的会话照样要收到。"""
    monkeypatch.setattr(b.asyncio, "sleep", lambda *_a: _REAL_SLEEP(0))
    orig = wired.send

    def _flaky(chat, text, *a, **k):
        if chat == "坏群":
            raise RuntimeError("群不存在")
        orig(chat, text)
    monkeypatch.setattr(wired, "send", _flaky)
    b.inflight_mark("坏群", "发不出去的活")
    b.inflight_mark("好群", "该收到的活")
    asyncio.run(b._report_interrupted_turns())
    assert any("该收到的活" in t for t in wired.texts)


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

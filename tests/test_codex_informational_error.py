"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv

_SKILL_NOTE = ("Skill descriptions were shortened to fit the skills context budget. "
               "Codex can still use all installed skills.")
_REAL_13 = "exec_command failed: CreateProcess message: Rejected"


def _run_one_turn(monkeypatch, middle_rows: list[dict]):
    """用假 app-server 跑完整一轮 `_run_turn_codex`, 返回 (回复, 回复参数, 告警)。"""
    from codex_storage_fixture import make_storage
    fixture=make_storage(monkeypatch)
    monkeypatch.setattr(b._fs,"SESSIONS_FILE",fixture.scope.home.parent/"sessions.json")
    replies, reply_kw, notices = [], [], []

    class _Writer:
        def write(self, raw): pass
        async def drain(self): pass
        def close(self): pass

    class _Reader:
        def __init__(self, rows):
            self.rows = [(json.dumps(r, ensure_ascii=False) + "\n").encode() for r in rows]
        def __aiter__(self): return self
        async def __anext__(self):
            if not self.rows:
                raise StopAsyncIteration
            return self.rows.pop(0)
        async def read(self): return b""
        async def readline(self): return self.rows.pop(0) if self.rows else b""

    rows = ([{"id": 1, "result": {}},
             {"id": 21, "result": {"config": fixture.config}},
             {"id": 2, "result": {"thread": {"id": "thread-1"}}},
             {"id": 3, "result": {"turn": {"id": "turn-1"}}}]
            + middle_rows
            + [{"method": "item/agentMessage/delta", "params": {"delta": "看起来很正常的回答"}},
               {"method": "turn/completed", "params": {
                   "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}}])

    class _Proc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = _Writer(), _Reader(rows), _Reader([])
            self.returncode = 0
        async def wait(self): return 0

    class _Sender:
        def __init__(self): self.feishu = self
        def begin_busy(self, _m): return None
        def end_busy(self, _m, _r): pass
        def send_reply(self, _chat, text, _thinking, **kw):
            replies.append(text); reply_kw.append(kw); return "mid-1"
        # ⚠ 旧脚手架的 send 不收 kind= —— 真代码发告警时带着它,
        #   所以旧测试**从没跑到过告警那一支**。这里必须收。
        def send(self, _chat, text, **kw):
            notices.append((text, kw.get("kind")))

    class _Front:
        def broadcast(self, _e): pass
        def broadcast_runtime(self, _m=""): pass
        def resolve_work_display_ex(self, requested): return requested, requested, ""

    proc = _Proc()

    async def fake_exec(*_a, **_k): return proc
    async def fake_drop(_c): pass

    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-5.6-luna", "high", "codex"))
    monkeypatch.setattr(b, "_load_session", lambda _k: None)
    monkeypatch.setattr(b, "_drop_client", fake_drop)
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", fake_exec)
    for name in ("_record_session", "_write_codex_status", "_append_codex_ledger",
                 "_remember", "_app_pin_last"):
        monkeypatch.setattr(b, name, lambda *_a, **_k: None)
    asyncio.run(b._run_turn_codex("chat", "问题"))
    return replies, reply_kw, notices


def _failed(reply_kw) -> bool:
    return any((kw.get("extra") or kw).get("failed") for kw in reply_kw)


def _error_item(text: str, *, legacy: bool = False) -> dict:
    item = {"type": "error", "message": text}
    return ({"type": "item.completed", "item": item} if legacy
            else {"method": "item/completed", "params": {"item": item}})


# ── 提示不许判失败 ────────────────────────────────────────────────────
def test_skill_budget_note_does_not_fail_the_turn(monkeypatch):
    replies, kw, notices = _run_one_turn(monkeypatch, [_error_item(_SKILL_NOTE)])
    assert replies and "看起来很正常的回答" in replies[0]
    assert not _failed(kw), "提示性 error 条目把整轮判成了失败 —— 用户会被叫去重试一个成功的回合"
    assert not [n for n in notices if n[1] == "codex_error"], (
        f"提示性条目仍发了工具链错误告警: {notices}")


def test_skill_budget_note_in_legacy_exec_shape_too(monkeypatch):
    """Behavioral regression coverage."""
    _r, kw, notices = _run_one_turn(monkeypatch, [_error_item(_SKILL_NOTE, legacy=True)])
    assert not _failed(kw)
    assert not [n for n in notices if n[1] == "codex_error"]


# ── ⑬ 当初要堵的洞不许重开 ──────────────────────────────────────────
def test_real_13_failure_still_fails_despite_a_fluent_answer(monkeypatch):
    """**这条是整份文件存在的底线**: 工具被拦 + 模型照样流畅作答 + turn.completed ——
    ⑬ 就是为这个组合立的。白名单不许让它漏过去。"""
    _r, kw, notices = _run_one_turn(monkeypatch, [_error_item(_REAL_13)])
    assert _failed(kw), "⑬ 那种'回答流畅但工具被拦'的回合没被判失败 —— 洞被重开了"
    assert [n for n in notices if n[1] == "codex_error"], "真失败没有发告警"


def test_unknown_error_text_still_fails(monkeypatch):
    """白名单之外的一律照旧 —— 安全的那一侧是"多报", 不是"漏报"。"""
    _r, kw, _n = _run_one_turn(monkeypatch, [_error_item("something new went wrong")])
    assert _failed(kw)


def test_note_and_real_failure_in_same_turn_still_fails(monkeypatch):
    """同一轮既有提示又有真失败 ⇒ 仍然是失败。提示不许"抵消"失败。"""
    _r, kw, _n = _run_one_turn(monkeypatch, [_error_item(_SKILL_NOTE), _error_item(_REAL_13)])
    assert _failed(kw)

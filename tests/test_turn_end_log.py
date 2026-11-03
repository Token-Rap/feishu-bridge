"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def _run(monkeypatch, engine: str, body):
    lines: list[str] = []
    monkeypatch.setattr(b, "log", lambda s: lines.append(s))
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("m", "high", engine))
    monkeypatch.setattr(b, "_run_turn_codex", body)
    monkeypatch.setattr(b, "_run_turn_inner", body)
    return lines


def _ends(lines: list[str]) -> list[str]:
    return [s for s in lines if "⏹ 回合" in s]


@pytest.mark.parametrize("engine,label", [("codex", "codex"), ("", "claude")])
def test_successful_turn_leaves_an_end_line_naming_the_engine(monkeypatch, engine, label):
    async def ok(*_a, **_k):
        return None
    lines = _run(monkeypatch, engine, ok)
    asyncio.run(b.run_turn("chat12345678", "hi"))
    ends = _ends(lines)
    assert len(ends) == 1, lines
    assert ends[0].startswith("[chat1234]") and "回合结束" in ends[0] and label in ends[0]


def test_crashing_turn_still_leaves_an_end_line_and_reraises(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("x")
    lines = _run(monkeypatch, "codex", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(b.run_turn("chat12345678", "hi"))
    ends = _ends(lines)
    assert len(ends) == 1 and "异常退出" in ends[0], lines


def test_cancelled_turn_is_labelled_not_as_normal_end(monkeypatch):
    async def cancelled(*_a, **_k):
        raise asyncio.CancelledError
    lines = _run(monkeypatch, "", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(b.run_turn("chat12345678", "hi"))
    ends = _ends(lines)
    assert len(ends) == 1 and "回合结束" not in ends[0], lines


def test_a_broken_logger_cannot_break_the_turn(monkeypatch):
    """Behavioral regression coverage."""
    async def ok(*_a, **_k):
        return None
    _run(monkeypatch, "codex", ok)

    def bad_log(_s):
        raise OSError("disk full")
    monkeypatch.setattr(b, "log", bad_log)
    asyncio.run(b.run_turn("chat12345678", "hi"))

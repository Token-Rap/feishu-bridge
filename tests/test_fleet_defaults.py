"""Inherited defaults choose Codex; explicit per-bot choices stay authoritative."""
import asyncio
import contextvars
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-defaults-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


@pytest.mark.parametrize("runtime,expected", [
    ({}, ("gpt-6.1-sol", "high", "codex")),
    ({"model": "claude-opus-5", "effort": "max"},
     ("claude-opus-5", "max", "claude")),
])
def test_inherited_and_explicit_runtime_dispatch(monkeypatch, runtime, expected):
    seen = []
    monkeypatch.setattr(b, "_read_runtime", lambda: dict(runtime))
    monkeypatch.setattr(b, "ACTIVE", set())
    monkeypatch.setattr(b, "TURN_DISPLAY", {})
    monkeypatch.setattr(b, "CODEX_QUESTIONS", SimpleNamespace(cancel_work=lambda *args: None))
    monkeypatch.setattr(b, "_QUESTION_CONTINUATION", contextvars.ContextVar("default-test", default=None))
    monkeypatch.setattr(b, "_display_chat_for", lambda chat: chat)
    monkeypatch.setattr(b, "_rollover_first_turn", lambda chat, prompt: prompt)
    monkeypatch.setattr(b, "log", lambda *args: None)

    async def codex(*args):
        seen.append(("codex", b._runtime_snapshot()))

    async def claude(*args):
        seen.append(("claude", b._runtime_snapshot()))

    monkeypatch.setattr(b, "_run_turn_codex", codex)
    monkeypatch.setattr(b, "_run_turn_inner", claude)
    asyncio.run(b.run_turn("synthetic-default", "test"))
    assert seen == [(expected[2], expected)]
    assert "synthetic-default" not in b.ACTIVE

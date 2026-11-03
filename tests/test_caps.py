"""桥上报的能力集 (`AppFrontend.caps`): 网关靠它决定给客户端开哪些入口。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_frontend


def _front():
    return app_frontend.AppFrontend(Path(tempfile.mkdtemp(prefix="caps-")), lambda _m: None)


def test_group_chat_and_compact_are_always_reported():
    caps = _front().caps()
    assert "group_chat" in caps and "compact" in caps


def test_codex_follows_the_capability_switch(monkeypatch):
    monkeypatch.setattr(app_frontend, "CODEX_CAPABLE", True)
    assert "codex" in _front().caps()
    monkeypatch.setattr(app_frontend, "CODEX_CAPABLE", False)
    assert "codex" not in _front().caps()


def test_terminal_caps_are_gone():
    assert not [c for c in _front().caps() if c.startswith("terminal")]

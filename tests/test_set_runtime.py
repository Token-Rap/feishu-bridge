"""App 静默切模型/强度通道 (set_runtime) 的行为测试。

要点: 只写 runtime.json、不产生任何聊天消息; 非法值忽略; default 清覆盖。
"""
from __future__ import annotations

import json
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


def _runtime() -> dict:
    try:
        return json.loads(b._RUNTIME_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _reset() -> None:
    b._RUNTIME_FILE.unlink(missing_ok=True)


def test_set_model_only():
    _reset()
    b._app_set_runtime("sonnet", None)
    assert _runtime() == {"model": "sonnet"}


def test_set_gpt_model():
    _reset()
    b._app_set_runtime("gpt-5.6-sol", None)
    assert _runtime() == {"model": "gpt-5.6-sol"}


def test_engine_switch_broadcasts_structured_session_reset(monkeypatch):
    _reset()
    events = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "_load_session",
                        lambda key: "claude-session-1" if key == "app" else None)
    monkeypatch.setattr(b, "_session_exists", lambda sid: sid == "claude-session-1")

    b._app_set_runtime("gpt-5.6-sol", None)

    assert len(events) == 1
    ev = events[0]
    assert ev["ev"] == "session_reset"
    assert ev["engine"] == "codex"
    assert ev["prev"] == "claude-session-1"
    assert ev["prev_engine"] == "claude"
    assert ev["hint"] == "engine_switched"
    assert isinstance(ev["ts"], float)


def test_engine_switch_hides_unresumable_previous_session(monkeypatch):
    _reset()
    events = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "_load_session", lambda _key: "pruned-session")
    monkeypatch.setattr(b, "_session_exists", lambda _sid: False)

    b._app_set_runtime("gpt-5.6-sol", None)

    assert events[0]["prev"] == ""
    assert events[0]["prev_engine"] == ""


def test_switching_back_carries_the_codex_thread(monkeypatch):
    _reset()
    b._write_runtime_model("gpt-5.6-sol")
    events = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "_load_session",
                        lambda key: "codex-thread-1" if key == "app::codex" else None)

    b._app_set_runtime("sonnet", None)

    assert events[0]["engine"] == "claude"
    assert events[0]["prev"] == "codex-thread-1"
    assert events[0]["prev_engine"] == "codex"


def test_same_engine_model_switch_does_not_reset_session(monkeypatch):
    _reset()
    events = []
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)

    b._app_set_runtime("sonnet", None)

    assert events == []


def test_disabled_codex_rejects_runtime_without_writing(monkeypatch):
    _reset()
    events = []
    monkeypatch.setattr(b, "CODEX_ENABLED", False)
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)

    b._app_set_runtime("gpt-5.6-sol", None)

    assert _runtime() == {}
    assert events == [{"ev": "error", "kind": "unsupported_engine",
                       "engine": "codex",
                       "text": "这台节点暂不开放 Codex 引擎。"}]


def test_set_effort_merges_with_model():
    _reset()
    b._app_set_runtime("opus", None)
    b._app_set_runtime(None, "xhigh")
    assert _runtime() == {"model": "opus", "effort": "xhigh"}


def test_default_clears_override():
    _reset()
    b._app_set_runtime("opus", "high")
    b._app_set_runtime("default", "default")
    assert _runtime() == {}


def test_invalid_values_ignored():
    _reset()
    b._app_set_runtime("opus", "high")
    b._app_set_runtime("bad name!", "ultra")     # 空格/感叹号非法; ultra 不在强度表
    assert _runtime() == {"model": "opus", "effort": "high"}


def test_no_chat_message_emitted():
    """静默是本功能的全部意义: 走 set_runtime 不允许碰 SENDER。"""
    _reset()

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("set_runtime 不应产生任何出站消息")

    old = b.SENDER
    b.SENDER = Boom()
    try:
        b._app_set_runtime("sonnet", "low")
    finally:
        b.SENDER = old
    assert _runtime() == {"model": "sonnet", "effort": "low"}


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")


def test_provider_switch_session_reset_carries_new_model(monkeypatch):
    _reset()
    b._RUNTIME_FILE.write_text(json.dumps({"model": "claude-opus-5"}), encoding="utf-8")
    events = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "_load_session", lambda key: "claude-session-1" if key == "app" else None)
    monkeypatch.setattr(b, "_session_exists", lambda sid: sid == "claude-session-1")
    monkeypatch.setattr(b._prov, "deepseek_configured", lambda: True)
    monkeypatch.setattr(b._prov, "cross_provider_resume_allowed", lambda: False)

    b._app_set_runtime("deepseek-flash", None)

    assert len(events) == 1
    ev = events[0]
    assert ev["ev"] == "session_reset"
    assert ev["hint"] == "provider_switched"
    assert ev["prev_provider"] == "anthropic"
    assert ev["provider"] == "deepseek"
    assert ev["model"] == "deepseek-flash"
    assert ev["prev"] == "claude-session-1"

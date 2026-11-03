"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from feishu_bridge.app_frontend import AppFrontend  # noqa: E402

_D = {"model": "claude-opus-5", "effort": "high"}


class _W:
    def __init__(self): self.buf = []
    def write(self, b): self.buf.append(json.loads(b.decode()))
    async def drain(self): pass
    def close(self): pass


def _front(reader=None) -> AppFrontend:
    f = AppFrontend(Path(tempfile.mkdtemp()), lambda *_a: None)
    f.defaults = dict(_D)
    if reader:
        f.set_runtime_reader(reader)
    return f


def _no_caps(frames: list) -> list:
    """比对时把 `caps` 摘掉: 这一族只测 runtime 帧的 defaults/runtime/observed,
    caps 归 test_caps 管; 写死整张表会让每个新能力误伤这里。"""
    return [{k: v for k, v in f.items() if k != "caps"} for f in frames]


def test_hello_carries_current_override():
    """靶心之一: 连上时就要知道当前切到了哪个 —— 否则重连后胶囊还是错的。"""
    f = _front(lambda: {"model": "claude-fable-5"})
    w = _W()
    asyncio.run(f._handle_frame({"type": "hello"}, w))
    assert _no_caps(w.buf) == [{"ev": "runtime", "defaults": _D,
                                "runtime": {"model": "claude-fable-5"},
                                "observed": "", "engine": "claude"}]


def test_set_runtime_broadcasts_immediately():
    """靶心之二: **落盘不等于对面知道。** 切完不报, 网关手上就还是旧值,
    而客户端每 15s 轮询到的正是那个旧值 —— 用户看到的是"切了没反应"。"""
    seen = {}
    f = _front(lambda: seen)
    f._set_runtime = lambda m, e: seen.update({k: v for k, v in (("model", m), ("effort", e)) if v})
    sent = []
    f.broadcast = lambda ev: sent.append(ev)
    asyncio.run(f._handle_frame({"type": "set_runtime", "model": "claude-fable-5"}, _W()))
    assert _no_caps(sent) == [{"ev": "runtime", "defaults": _D,
                               "runtime": {"model": "claude-fable-5"},
                               "observed": "", "engine": "claude"}]


def test_broadcast_is_after_the_write_not_before():
    """顺序不能反: 先落盘再报, 否则报的是旧值 (而且报完才写, 中间崩了两边就不一致)。"""
    order = []
    f = _front(lambda: {"model": "x"})
    f._set_runtime = lambda m, e: order.append("write")
    f.broadcast = lambda ev: order.append("broadcast")
    asyncio.run(f._handle_frame({"type": "set_runtime", "model": "x"}, _W()))
    assert order == ["write", "broadcast"]


def test_group_set_runtime_is_ignored():
    """model/effort 是 bot 属性; 群聊只是展示面, 不允许从 group 改 runtime。"""
    f = _front(lambda: {})
    f._chat_validator = lambda c: (c == "group:fleet-product", "")
    f._set_runtime = lambda *_a: (_ for _ in ()).throw(AssertionError("不该写 runtime"))
    sent = []
    f.broadcast = lambda ev: sent.append(ev)
    asyncio.run(f._handle_frame({"type": "set_runtime", "chat_id": "group:fleet-product",
                                 "model": "claude-fable-5"}, _W()))
    assert sent == []


def test_missing_reader_yields_empty_not_crash():
    """没注入取值口 (老宿主/测试) → {} → 网关那侧退回老行为, **不会更坏**。"""
    f = _front()
    assert f.runtime_now() == {}


def test_reader_failure_does_not_break_the_frame():
    """Behavioral regression coverage."""
    def boom(): raise OSError("nope")
    f = _front(boom)
    w = _W()
    asyncio.run(f._handle_frame({"type": "hello"}, w))
    assert _no_caps(w.buf) == [{"ev": "runtime", "defaults": _D, "runtime": {},
                                "observed": "", "engine": "claude"}]


def test_defaults_and_runtime_are_not_the_same_thing():
    """Behavioral regression coverage."""
    f = _front(lambda: {"model": "claude-fable-5"})
    w = _W()
    asyncio.run(f._handle_frame({"type": "hello"}, w))
    ev = w.buf[0]
    assert ev["defaults"]["model"] == "claude-opus-5"      # 常量, 没变
    assert ev["runtime"]["model"] == "claude-fable-5"      # 覆盖值, 变了


def test_gpt_runtime_reports_codex_engine_and_native_compact():
    f = _front(lambda: {"model": "gpt-5.6-sol"})
    w = _W()
    asyncio.run(f._handle_frame({"type": "hello"}, w))
    ev = w.buf[0]
    assert ev["engine"] == "codex"
    assert "compact" in ev["caps"]


def test_stale_observed_gpt_does_not_override_claude_default_engine():
    f = _front(lambda: {})
    f.observed_model = "gpt-5.6-sol"
    w = _W()
    asyncio.run(f._handle_frame({"type": "hello"}, w))
    assert w.buf[0]["engine"] == "claude"


def test_bridge_does_not_advertise_codex_when_disabled(monkeypatch):
    from feishu_bridge import app_frontend
    monkeypatch.setattr(app_frontend, "CODEX_CAPABLE", False)
    assert "codex" not in _front().caps("claude")


def test_bridge_advertises_codex_when_enabled(monkeypatch):
    from feishu_bridge import app_frontend
    monkeypatch.setattr(app_frontend, "CODEX_CAPABLE", True)
    assert "codex" in _front().caps("claude")

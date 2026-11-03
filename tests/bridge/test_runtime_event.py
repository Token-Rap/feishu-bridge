"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.bridge.app_frontend import AppFrontend  # noqa: E402


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, b: bytes) -> None:
        self.lines.append(b.decode())


class _Reader:
    """喂一条 hello 然后 EOF —— 模拟网关连上就自报身份。"""

    def __init__(self):
        self._lines = [b'{"type":"hello"}\n']

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


def _connect(defaults: dict | None) -> list[dict]:
    f = AppFrontend(Path(tempfile.mkdtemp(prefix="rt-ev-")), lambda *_a: None)
    if defaults is not None:
        f.defaults = defaults
    w = _Writer()
    asyncio.run(f._on_conn(_Reader(), w))
    return [json.loads(x) for x in w.lines]


def test_reports_defaults_after_hello():
    evs = _connect({"model": "claude-opus-5", "effort": "high"})
    rt = [e for e in evs if e.get("ev") == "runtime"]
    assert len(rt) == 1
    assert rt[0]["defaults"] == {"model": "claude-opus-5", "effort": "high"}
    assert rt[0]["engine"] == "claude"


def test_silent_when_unset():
    """没设过就什么都不报 —— 空的 defaults 会被网关当成"这个 bot 报了个空", 更糟。"""
    assert _connect(None) == []


def test_every_hello_reports_again():
    """网关重启后重连也要能立刻拿到 —— 它的缓存是内存态, 重启就没了。"""
    f = AppFrontend(Path(tempfile.mkdtemp(prefix="rt-ev-")), lambda *_a: None)
    f.defaults = {"model": "m", "effort": "e"}
    for _ in range(2):
        w = _Writer()
        asyncio.run(f._on_conn(_Reader(), w))
        assert any(json.loads(x).get("ev") == "runtime" for x in w.lines)


def test_gpt_default_reports_codex_engine():
    evs = _connect({"model": "gpt-5.6-sol", "effort": "high"})
    rt = [e for e in evs if e.get("ev") == "runtime"][0]
    assert rt["engine"] == "codex"
    assert "compact" in rt["caps"]


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

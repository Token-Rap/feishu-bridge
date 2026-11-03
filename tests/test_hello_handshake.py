"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from feishu_bridge.bridge.app_frontend import AppFrontend  # noqa: E402


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, b: bytes) -> None:
        self.lines.append(b.decode())


class _Reader:
    """按脚本喂入站行; 喂完返回 b'' 表示对端关闭。"""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


def _run(inbound: list[bytes], defaults: dict | None = None, pending: list | None = None):
    f = AppFrontend(Path(tempfile.mkdtemp(prefix="hello-")), lambda *_a: None)
    if defaults:
        f.defaults = defaults
    if pending:
        f._pending = [(9e9, e) for e in pending]      # ts 给未来值, 免得被 TTL 滤掉
    w = _Writer()
    asyncio.run(f._on_conn(_Reader(inbound), w))
    return [json.loads(x) for x in w.lines]


D = {"model": "claude-opus-5", "effort": "high"}


def test_write_only_client_gets_nothing():
    """**核心**: 只发一条消息就关掉的客户端 (session_inject) 不该收到任何字节。"""
    out = _run([json.dumps({"type": "user_message", "text": "hi"}).encode() + b"\n"],
               defaults=D, pending=[{"ev": "reply", "text": "旧的"}])
    assert out == [], f"给只写客户端写了东西: {out}"


def test_silent_client_gets_nothing():
    """连上什么都不说就断开 —— 同样一个字节都不写。"""
    assert _run([], defaults=D, pending=[{"ev": "reply", "text": "旧的"}]) == []


def test_hello_gets_defaults():
    out = _run([b'{"type":"hello"}\n'], defaults=D)
    # runtime 表示当前覆盖值，defaults 表示默认配置。
    # 远程节点的运行状态由桥报告。
    # 这条只守 hello 帧的形状; caps 的内容由 test_caps.py 守。
    assert len(out) == 1
    assert {k: v for k, v in out[0].items() if k != "caps"} == \
        {"ev": "runtime", "defaults": D, "runtime": {}, "observed": "", "engine": "claude"}
    assert isinstance(out[0]["caps"], list)


def test_hello_gets_pending_then_defaults():
    """补发在前、默认值在后 —— 补发是历史, 默认值是当前状态。"""
    out = _run([b'{"type":"hello"}\n'], defaults=D,
               pending=[{"ev": "reply", "text": "掉线期间的"}])
    assert [e.get("ev") for e in out] == ["reply", "runtime"]


def test_hello_without_defaults_is_silent():
    """没设过 defaults 又没积压 → hello 也不回任何东西 (别回一个空的)。"""
    assert _run([b'{"type":"hello"}\n']) == []


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

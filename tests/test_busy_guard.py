"""任何路径调用 run_turn 都必须持有忙标志。

命令处理可能早于外层忙标志的设置，因此 run_turn 自身也要保护 SDK 流。
嵌套调用只清除自己设置的标志，避免释放外层守卫。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("claude_default")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-busy-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


@pytest.fixture(autouse=True)
def _clean():
    b.ACTIVE.discard("c1")
    yield
    b.ACTIVE.discard("c1")


# ── 守卫本身 ────────────────────────────────────────────────────────
def test_guard_marks_and_clears():
    with b._busy_guard("c1") as took:
        assert took is True and "c1" in b.ACTIVE
    assert "c1" not in b.ACTIVE


def test_guard_is_reentrant_and_does_not_steal():
    """外层已经占着 (第 6 步那种要跨越"处理暂存"的守卫) → 内层不许在退出时替它松手。"""
    b.ACTIVE.add("c1")
    with b._busy_guard("c1") as took:
        assert took is False
    assert "c1" in b.ACTIVE, "内层把外层的标志清掉了 —— 暂存那段会失去保护"


def test_guard_clears_on_exception():
    """异常路径也要还锁, 否则一次报错就让这个 bot 从此不理人。"""
    with pytest.raises(RuntimeError):
        with b._busy_guard("c1"):
            raise RuntimeError("boom")
    assert "c1" not in b.ACTIVE


# ── run_turn 真的用上了它 (这条才是靶心) ─────────────────────────────
def test_run_turn_is_busy_even_when_called_from_a_command(monkeypatch):
    """靶心: /compact 那条路不经过第 6 步, 所以保护必须长在 run_turn 里面。

    做法: 让 run_turn 走到取 client 那一步就停下, 在那一刻检查 ACTIVE。
    """
    seen = {}

    async def _fake_get_client(cid):
        seen["busy"] = cid in b.ACTIVE
        raise RuntimeError("探针: 到此为止")

    monkeypatch.setattr(b, "_get_client", _fake_get_client)
    monkeypatch.setattr(b, "STREAMING_ON", False)
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "_start_idle_watch", lambda _c: None)
    monkeypatch.setattr(b.APP_FRONT, "broadcast", lambda _e: None)

    asyncio.run(b.run_turn("c1", "/compact"))
    assert seen.get("busy") is True, "run_turn 跑着的时候 chat 不在 ACTIVE —— 并发那轮就是这么进来的"
    assert "c1" not in b.ACTIVE, "跑完没还锁"


class _Sender:
    """只实现 run_turn 在这条最短路径上会碰到的。"""

    class feishu:
        @staticmethod
        def send(*_a, **_k): ...

    @staticmethod
    def begin_busy(*_a, **_k): return None

    @staticmethod
    def end_busy(*_a, **_k): ...

    @staticmethod
    def send(*_a, **_k): ...

    @staticmethod
    def send_text(*_a, **_k): ...


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

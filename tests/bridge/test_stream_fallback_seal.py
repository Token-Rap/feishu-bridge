"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


class StubFront:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


def test_seal_broadcasts_reply():
    b.APP_FRONT = StubFront()
    b._app_seal("最终正文")
    assert b.APP_FRONT.events == [{"ev": "reply", "text": "最终正文"}]


def test_seal_carries_full_text():
    """收尾事件必须带完整正文 —— 两端的 reply 分支是**替换** live 气泡, 空文本会把
    已经流出来的内容替换没了。"""
    b.APP_FRONT = StubFront()
    b._app_seal("甲乙丙" * 50)
    assert b.APP_FRONT.events[0]["text"] == "甲乙丙" * 50


def test_seal_never_raises():
    """fail-soft 硬要求: 收尾广播失败不能反过来掀翻回合。"""
    class Boom:
        def broadcast(self, ev):
            raise RuntimeError("网关没了")

    b.APP_FRONT = Boom()
    b._app_seal("x")          # 不抛就算过


def test_seal_is_a_terminator_not_a_notice():
    """必须是 reply —— 客户端只认 reply/error 收尾, notice 不清「生成中」
    (这正是 1cc3308 修过的同族问题)。"""
    b.APP_FRONT = StubFront()
    b._app_seal("x")
    assert b.APP_FRONT.events[0]["ev"] == "reply"



class StubInner:
    def __init__(self, ok):
        self.ok_, self.calls = ok, 0

    async def finish(self, full, lines):
        self.calls += 1
        return self.ok_


def _tee_finish(inner_ok: bool):
    import asyncio

    from feishu_bridge.bridge.app_frontend import _TeeStream
    front = StubFront()
    st = _TeeStream(StubInner(inner_ok), front)
    ret = asyncio.run(st.finish("正文", []))
    return ret, [e for e in front.events if e.get("ev") == "reply"]


def test_tee_finish_propagates_inner_result():
    """**返回值必须透传** —— 不透传的话上层 delivered 恒为 None, 每轮都掉进
    「终写失败」兜底 (今天 42 条那种日志全是假警报), 真失败反而分辨不出来。"""
    assert _tee_finish(True)[0] is True
    assert _tee_finish(False)[0] is False


def test_tee_finish_broadcasts_exactly_once_either_way():
    """广播是无条件的: 平台侧写卡成没成, App 都已经拿到这条 reply。
    所以上层**不能**在兜底路径里再补一次 —— 那就是 86eae81 的错, 会每轮双发。"""
    for ok in (True, False):
        _ret, replies = _tee_finish(ok)
        assert len(replies) == 1, f"inner_ok={ok} 时广播了 {len(replies)} 条"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

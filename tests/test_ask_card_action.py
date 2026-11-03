"""飞书按钮提问卡的回调测试: 点按钮 → resolve 等待中的提问 future。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


def _ev(value: dict):
    return SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value=value)))


def test_single_question_tap_resolves():
    async def run():
        b.MAIN_LOOP = asyncio.get_running_loop()
        fut = b.MAIN_LOOP.create_future()
        b.ASK_PENDING["chatX"] = ("askX", fut)
        b.on_card_action(_ev({"fleet_ask": "askX", "qi": 0, "qn": 1,
                              "header": "晚饭", "label": "饺子"}))
        await asyncio.sleep(0.05)
        assert fut.done() and fut.result() == "晚饭: 饺子"
        b.ASK_PENDING.pop("chatX", None)
    asyncio.run(run())


def test_multi_question_accumulates_until_complete():
    async def run():
        b.MAIN_LOOP = asyncio.get_running_loop()
        fut = b.MAIN_LOOP.create_future()
        b.ASK_PENDING["chatY"] = ("askY", fut)
        b.on_card_action(_ev({"fleet_ask": "askY", "qi": 0, "qn": 2, "header": "A", "label": "1"}))
        await asyncio.sleep(0.05)
        assert not fut.done(), "只答了 1/2 不该 resolve"
        b.on_card_action(_ev({"fleet_ask": "askY", "qi": 1, "qn": 2, "header": "B", "label": "2"}))
        await asyncio.sleep(0.05)
        assert fut.done() and fut.result() == "A: 1; B: 2"
        b.ASK_PENDING.pop("chatY", None)
    asyncio.run(run())


def test_expired_card_tap_is_harmless():
    b.ASK_PENDING.clear()
    resp = b.on_card_action(_ev({"fleet_ask": "gone", "qi": 0, "qn": 1, "label": "x"}))
    assert resp is not None            # 不抛异常, 返回「已过期」toast


def test_foreign_card_ignored():
    resp = b.on_card_action(_ev({"whatever": 1}))
    assert resp is not None            # 非提问卡的按钮事件静默放过


def test_timeout_denies_with_no_reask_instruction():
    """超时必须 deny 且明说别再问 — 放行会让模型原样重问, 用户看到连发两张卡。"""
    src = (Path(__file__).resolve().parent.parent / "fleet_bridge.py").read_text("utf-8")
    # 锚到提问钩子那段 (文件里还有一处审批超时, 别抓错)
    seg = src.split("提问 10 分钟未回答", 1)[1].split("finally:", 1)[0]
    assert '"permissionDecision": "deny"' in seg and "不要重复提问" in seg


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

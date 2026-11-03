"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-think-sig-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


def _start(kind="thinking", idx=0):
    return {"type": "content_block_start", "index": idx, "content_block": {"type": kind}}


def _stop(idx=0):
    return {"type": "content_block_stop", "index": idx}


# ── 开: 思考块一开始就点亮 ──────────────────────────────────────────
def test_thinking_start_lights_it_up():
    sig, idx = b._thinking_signal(_start(), None)
    assert sig == {"ev": "reasoning", "text": "", "active": True}, sig
    assert idx == 0


def test_text_start_is_not_thinking():
    assert b._thinking_signal(_start("text", 1), None) == (None, None)


def test_deltas_emit_nothing():
    """空 thinking_delta 数出来只是「有几个空字符串」, 不作为信号。"""
    ev = {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}}
    assert b._thinking_signal(ev, 0) == (None, 0)


def test_second_start_while_active_does_not_re_announce():
    """已经亮着就别再发一遍 —— 客户端会闪。"""
    assert b._thinking_signal(_start(idx=2), 0) == (None, 0)


# ── 关: 同一个 index 停了才算停 ─────────────────────────────────────
def test_matching_stop_turns_it_off():
    sig, idx = b._thinking_signal(_stop(0), 0)
    assert sig == {"ev": "reasoning", "text": "", "active": False}, sig
    assert idx is None


def test_other_block_stop_is_ignored():
    """正文块结束不该把思考指示关掉 —— 它们是不同的 index。"""
    assert b._thinking_signal(_stop(1), 0) == (None, 0)


def test_stop_without_active_is_noop():
    assert b._thinking_signal(_stop(0), None) == (None, None)


def test_think_text_think_toggles_twice():
    """一轮里可以想→写→再想, 指示要跟着来回, 不是"一轮只亮一次"。"""
    seq = [_start(idx=0), _stop(0), _start("text", 1), _stop(1), _start(idx=2), _stop(2)]
    idx, out = None, []
    for ev in seq:
        sig, idx = b._thinking_signal(ev, idx)
        if sig:
            out.append(sig["active"])
    assert out == [True, False, True, False], out


# ── 协议面: 老客户端要能当没看见 ────────────────────────────────────
def test_event_keeps_text_field_for_old_clients():
    """复用 reasoning 帧 + 加字段, 不新开事件类型 (新增要三端同步, 贵一档)。
    老客户端只读 text, 读到空串 = 没有思考内容, 行为与从前一致。"""
    sig, _ = b._thinking_signal(_start(), None)
    assert set(sig) == {"ev", "text", "active"} and sig["text"] == ""


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

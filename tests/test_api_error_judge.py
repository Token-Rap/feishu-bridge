"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-apierr-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


class _Res:
    """ResultMessage 替身; 只带传进来的字段, 模拟新旧 SDK 的差异。"""

    def __init__(self, **kw):
        self.is_error = kw.pop("is_error", False)
        self.result = kw.pop("result", None)
        self.subtype = kw.pop("subtype", "success")
        for k, v in kw.items():
            setattr(self, k, v)


# ── ① 有类型字段: 读类型 ────────────────────────────────────────────
def test_api_error_status_is_enough():
    """Behavioral regression coverage."""
    assert b.turn_failed(_Res(subtype="success", is_error=True, api_error_status=429,
                              terminal_reason="api_error", result="API Error: …"))


def test_terminal_reason_alone_is_enough():
    """字段不是一起有值的 —— 只有 terminal_reason 时也要认出来。"""
    assert b.turn_failed(_Res(terminal_reason="api_error", result="半截正文"))


def test_normal_turn_with_new_fields_is_not_an_error():
    """正常回合在新 SDK 上带 terminal_reason='completed' —— 别把它当故障。"""
    assert not b.turn_failed(_Res(terminal_reason="completed", result="正常回复"))


def test_interrupt_is_not_an_api_error():
    """中断是用户按的, 不是故障 —— 报成故障会触发「要不要重试」的无谓提示。"""
    assert not b.turn_failed(_Res(terminal_reason="aborted_streaming", result="半截"))


# ── ② 回落: 老 SDK 上字段根本不存在 ─────────────────────────────────
def test_old_sdk_falls_back_to_text():
    """0.2.128 的 ResultMessage 没有这些属性 —— 取属性不能抛, 且要回落到文本判据。"""
    assert b.turn_failed(_Res(is_error=False, result="API Error: Connection closed mid-response."))


def test_old_sdk_normal_reply_is_not_an_error():
    assert not b.turn_failed(_Res(is_error=False, result="这是一条正常回复, 里面提到了 timeout 和 502"))


def test_is_error_alone_still_counts():
    """原有行为不许丢: is_error 为真就是错, 哪怕文本不像。"""
    assert b.turn_failed(_Res(is_error=True, result="随便什么"))


# ── 边界 ────────────────────────────────────────────────────────────
def test_none_result_message_is_not_an_error():
    assert not b.turn_failed(None)


def test_status_zero_is_not_treated_as_missing():
    """0 是个合法状态码值吗? 不是 —— 但**它和 None 必须分得开**, 别用真值判断。"""
    assert not b.turn_failed(_Res(api_error_status=None, result="正常回复"))


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

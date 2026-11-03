"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-rfields-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


class _Res:
    def __init__(self, **kw):
        self.is_error = kw.pop("is_error", True)
        self.subtype = kw.pop("subtype", "success")
        for k, v in kw.items():
            setattr(self, k, v)


def test_all_three_fields_appear():
    line = b._result_fields_line(_Res(api_error_status=429, terminal_reason="api_error",
                                     errors=["boom"]))
    for name in ("api_error_status", "terminal_reason", "errors"):
        assert name in line, line


def test_none_is_printed_not_omitted():
    """靶心: None 也要显示 —— 省掉就分不出「SDK 没给」和「我们没读」。"""
    line = b._result_fields_line(_Res(api_error_status=None, terminal_reason=None, errors=None))
    assert line.count("None") >= 3, line


def test_old_sdk_without_the_attrs_still_prints_them():
    """老 SDK 的对象上压根没有这些属性 —— 也要打成 None, 而不是整行消失。"""
    line = b._result_fields_line(_Res())          # 只有 is_error/subtype
    for name in ("api_error_status", "terminal_reason", "errors"):
        assert f"{name}=None" in line, line


def test_values_are_raw_not_interpreted():
    """原样打: 429 就是 429, 别翻译成「限流」之类的结论。"""
    line = b._result_fields_line(_Res(api_error_status=429, terminal_reason="api_error"))
    assert "429" in line and "api_error" in line
    assert "限流" not in line and "结论" not in line


def test_is_error_and_subtype_ride_along():
    """这两个是判据的另一半, 一起打才对得上账 (subtype='success' 而 is_error=True 那种)。"""
    line = b._result_fields_line(_Res(is_error=True, subtype="success"))
    assert "is_error=True" in line and "success" in line


def test_long_errors_are_truncated_but_say_so():
    """errors 可能很长 —— 截断可以, **但要说截了**, 否则又是一次静默残缺。"""
    line = b._result_fields_line(_Res(errors=["x" * 5000]))
    assert len(line) < 1200, len(line)
    assert "截断" in line


def test_none_result_is_handled():
    assert b._result_fields_line(None)


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

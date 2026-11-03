"""「重试」按钮的服务端那一半 (用户要 iOS 上能点而不用手打「重试」)。

**规则只在服务端存一份。** 让客户端去 match 错误文案的话, 同一套判据会在 Swift 和
Python 各存一份, 加一种错误类型要改两处, 迟早漂移 —— 而漂移的后果恰好是这次要避免的
那个: **在不该重试的错误上给出重试入口, 把无用功从打字变成点击, 还更顺手。**

三个字段只在失败时出现, 正常回复一个都不带 —— 所以 `failed` 存在与否就是"这轮失败没有"。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="retry-meta-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


class _RM:
    def __init__(self, result=None, terminal_reason=None):
        self.result, self.terminal_reason = result, terminal_reason


# ── retryable 的分流 ──────────────────────────────────────────────
def test_auth_failure_is_not_retryable():
    """靶心: 认证失效不给按钮 —— 重试用的是同一份凭据, 通常还是失败。"""
    m = b._failure_meta(_RM("Failed to authenticate: OAuth session expired"), False, False)
    assert m["failed"] is True and m["retryable"] is False, m


def test_the_other_auth_wording_too():
    """Behavioral regression coverage."""
    m = b._failure_meta(_RM("API Error: 401 OAuth access token has expired. Re-authenticate..."),
                        False, False)
    assert m["retryable"] is False, m


def test_network_failure_is_retryable():
    """Behavioral regression coverage."""
    m = b._failure_meta(_RM("API Error: Connection lost mid-response."), False, False)
    assert m["retryable"] is True, m


def test_unknown_failure_defaults_to_retryable():
    """认不出来的错误给按钮 —— 让用户白试一次, 好过在能救的错上不给入口。"""
    m = b._failure_meta(_RM(None, "api_error"), False, False)
    assert m["retryable"] is True, m


# ── retry_hint 的形状 (与客户端的约定) ────────────────────────────
def test_hint_has_no_outer_parens():
    """客户端要把它单独渲染成副文本 —— 正文里那句嵌在句中需要括号, 单独显示时是多余的。"""
    m = b._failure_meta(_RM("Failed to authenticate: OAuth session expired"), False, False)
    assert not m["retry_hint"].startswith("（") and not m["retry_hint"].endswith("）"), m


def test_body_keeps_its_parens_so_the_client_can_strip_the_suffix():
    """Behavioral regression coverage."""
    suffix = b._error_suffix(_RM("Failed to authenticate: OAuth session expired"),
                             "", False, False)
    assert suffix.rstrip().endswith("）"), suffix
    m = b._failure_meta(_RM("Failed to authenticate: OAuth session expired"), False, False)
    assert f"（{m['retry_hint']}）" in suffix, (suffix, m["retry_hint"])


# ── /retry 命令 ───────────────────────────────────────────────────
def test_retry_command_is_recognised():
    """按钮走 command 帧 → 不回显用户气泡; 而 _app_command 只收斜杠命令。"""
    src = Path(__file__).resolve().parent.parent.joinpath(
        "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    assert 'if low == "/retry":' in src, "/retry 分支没接上"
    assert 'text, low = "重试", "重试"' in src, (
        "/retry 应该翻译成普通消息「重试」而不是机械重跑上一条 prompt —— "
        "上一轮可能已经做了一半, 机械重跑会重复副作用")


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-tip-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


def test_unsafe_tool_tells_him_to_resend():
    """有写操作 → 我们不敢自动重跑, 所以必须**让他知道该他动手**。"""
    tip = b._failure_tip(saw_unsafe_tool=True, retried=False)
    assert "写操作" in tip and "重试" in tip


def test_retried_and_still_failed_says_so():
    """自动重试过还是不行 —— 别让他以为我们没试过。"""
    tip = b._failure_tip(saw_unsafe_tool=False, retried=True)
    assert "重试" in tip and ("已自动" in tip or "仍" in tip)


def test_not_retried_still_offers_the_action():
    """既没重试也没写操作 (比如非网络类错误) —— 照样告诉他能做什么。"""
    tip = b._failure_tip(saw_unsafe_tool=False, retried=False)
    assert "重试" in tip


def test_tip_is_never_empty():
    """任何组合都要给出下一步 —— 空字符串等于把他晾在那儿。"""
    for unsafe in (True, False):
        for retried in (True, False):
            assert b._failure_tip(saw_unsafe_tool=unsafe, retried=retried).strip()


def test_tip_does_not_repeat_the_english_sentence():
    """SDK 那句英文由上游给, 提示只补「你要做什么」, 别复读。"""
    for unsafe in (True, False):
        for retried in (True, False):
            assert "API Error" not in b._failure_tip(saw_unsafe_tool=unsafe, retried=retried)














class _RM:
    def __init__(self, result=None, subtype=None, terminal_reason=None):
        self.result, self.subtype, self.terminal_reason = result, subtype, terminal_reason
        self.is_error = True


def test_uses_the_result_text_when_present():
    s = b._error_suffix(_RM(result="API Error: Connection lost"), "", False, False)
    assert "API Error: Connection lost" in s


def test_falls_back_to_terminal_reason_not_subtype():
    """靶心: 出错时 subtype 可能是 'success' —— 「⚠ 出错: success」是自相矛盾的输出,
    用户会当成乱码忽略, 等于没通知, 而我们还以为通知过了。
    **回落链上不该有一个会产生自相矛盾输出的环节。**"""
    s = b._error_suffix(_RM(subtype="success", terminal_reason="api_error"), "", False, False)
    assert "api_error" in s
    assert "success" not in s


def test_never_says_success_even_without_terminal_reason():
    """两个都没有时也不许退回 subtype —— 宁可说「未知错误」。"""
    s = b._error_suffix(_RM(subtype="success"), "", False, False)
    assert "success" not in s, s


def test_does_not_repeat_what_sdk_already_put_in_the_body():
    """SDK 会把那句英文塞进正文末尾, 我们再拼一遍就是同一句出现两次。"""
    err = "API Error: Connection lost mid-response."
    body = f"半截正文…\n{err}"
    s = b._error_suffix(_RM(result=err), body, True, False)
    assert s.count(err) == 0, s


def test_dedup_never_drops_the_marker_or_the_action():
    """去重只去错误原文 —— **「⚠」和「你该做什么」永远保留**:
    前者是用户分辨"这不是正常回复"的唯一标记, 后者是他唯一能做的动作。"""
    err = "API Error: Connection lost mid-response."
    s = b._error_suffix(_RM(result=err), f"正文\n{err}", True, False)
    assert "⚠" in s and "重试" in s, s


def test_result_field_is_logged_too():
    """result 是唯一进到**用户可见文案**的字段, 原来五个判据字段里偏偏没有它 ——
    后果当场兑现: 出错后想知道"用户到底看到了什么"只能去翻网关的 history。
    **不能指望每次都有第二份记录。**"""
    line = b._result_fields_line(_RM(result="API Error: boom", terminal_reason="api_error"))
    assert "result=" in line and "boom" in line










def test_auth_failure_does_not_tell_the_user_to_retry():
    """靶心: 认证失效时不许再说「发一句重试」。"""
    tip = b._failure_tip(False, False,
                         "Failed to authenticate: OAuth session expired and could not be refreshed")
    assert "重试」我再跑" not in tip and "重试」可再跑" not in tip, tip
    assert "重新登录" in tip, tip


def test_the_8_13_wording_is_caught_too():
    """Behavioral regression coverage."""
    tip = b._failure_tip(False, False,
                         "API Error: 401 OAuth access token has expired. Re-authenticate...")
    assert "重新登录" in tip, tip


def test_it_says_no_restart_needed():
    """Behavioral regression coverage."""
    tip = b._failure_tip(False, False, "Failed to authenticate: OAuth session expired")
    assert "不用重启" in tip, tip


def test_auth_branch_wins_over_unsafe_tool():
    """有写操作时也该说重登 —— 「未自动重试以防重复」在认证失效面前是次要信息。"""
    tip = b._failure_tip(True, False, "Failed to authenticate: OAuth session expired")
    assert "重新登录" in tip, tip


def test_normal_errors_keep_the_retry_advice():
    """Behavioral regression coverage."""
    tip = b._failure_tip(False, False, "API Error: Connection lost mid-response.")
    assert "重试" in tip and "重新登录" not in tip, tip


def test_missing_err_keeps_old_behaviour():
    """老调用点不传 err 时行为不变 —— 参数是加上去的, 不该改变既有分支。"""
    assert b._failure_tip(False, False) == b._failure_tip(False, False, "")


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

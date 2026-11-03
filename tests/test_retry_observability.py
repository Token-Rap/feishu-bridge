'Retry paths report their outcome.'
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="retry-obs-test-")]
try:
    from feishu_bridge.fleet_bridge import _retry_summary, _retry_user_note  # noqa: E402
finally:
    sys.argv = _argv


# (elapsed, chars, tools, ok, exc, errored) -> substrings that must appear
_SUMMARY_CASES = [
    ((664.0, 1820, 7, True, None), ["664s", "7 次工具", "成功", "1820 字"]),
    ((3.2, 40, 0, True, None), ["3s", "成功", "40 字"]),
    ((12.0, 0, 2, False, None), ["12s", "2 次工具", "没拿回内容"]),
    ((5.0, 0, 1, False, "ConnectionError: peer closed"), ["异常告终", "ConnectionError"]),
    # got text, but the text *is* an error. This case used to be counted
    # as "成功" -- see test_retry_error_tail_is_not_success below.
    ((370.0, 398, 6, False, None, True), ["370s", "6 次工具", "398 字", "错着结束", "未跑完"]),
]

_NOTE_CASES = [
    ((True, None), "已自动重试并接上"),
    ((False, None), "没拿回内容"),
    ((False, "RuntimeError: x"), "异常中断"),
    ((False, None, True), "撞上同一个错"),
]


def test_summary_says_which_ending_it_was():
    for args, must in _SUMMARY_CASES:
        got = _retry_summary(*args)
        assert all(m in got for m in must), f"{args} -> {got!r} missing {must}"


def test_note_says_which_ending_it_was():
    for args, must in _NOTE_CASES:
        got = _retry_user_note(*args)
        assert must in got, f"{args} -> {got!r}"


def test_summary_distinguishes_every_case():
    """Behavioral regression coverage."""
    outs = {_retry_summary(*args) for args, _ in _SUMMARY_CASES}
    assert len(outs) == len(_SUMMARY_CASES), f"only {len(outs)} distinct outputs"


def test_note_distinguishes_every_case():
    outs = {_retry_user_note(*args) for args, _ in _NOTE_CASES}
    assert len(outs) == len(_NOTE_CASES), f"only {len(outs)} distinct outputs"


def test_exception_text_is_truncated():
    """Behavioral regression coverage."""
    got = _retry_summary(1.0, 0, 0, False, "E: " + "x" * 500)
    assert len(got) < 200 and "异常告终" in got


# ── the retry that reported success while delivering an error ──────

def test_retry_error_tail_is_not_reported_as_success():
    """Behavioral regression coverage."""
    got = _retry_summary(370.0, 398, 6, False, None, True)
    assert "成功" not in got, got
    assert "398 字" in got and "未跑完" in got, got
    # ⚠ 措辞不许描述一个我们**故意不做**的判断: 第一版写的是
    # 「结尾是 API 报错」, 而判据是重试自己那条 ResultMessage, 不是正文的尾巴。
    # 日志在这仓是被当证据读的 —— 引后来人去看错的地方, 比不写更贵。
    assert "结尾" not in got, got
    note = _retry_user_note(False, None, True)
    assert "接上" not in note, note


def test_retry_verdict_is_not_read_off_the_prose():
    """⚠ The retry verdict must come from the retry's own ResultMessage, never from
    pattern-matching the answer text.

    Kept as a source assertion because it is a *design* constraint, not a behaviour:
    the first cut of this fix scored "does the answer end like an error?" with a
    regex, and the false-positive test caught it flagging
    「修好了, 现在不会再吐 API Error: Connection closed 了。」 -- this repo talks about
    those error strings all day. Reading a verdict off prose is how a fix for a
    silent failure becomes a new silent failure.
    """
    src = (Path(__file__).resolve().parent.parent / "src/feishu_bridge/fleet_bridge.py").read_text(encoding="utf-8")
    i = src.index("_r_bad = (")
    verdict = src[i:src.index("\n", src.index("looks_like_api_error(r_result.result)", i))]
    assert "r_result" in verdict, verdict
    for prose in ("r_final", "answer_buf", "endswith", "search("):
        assert prose not in verdict, f"retry verdict is reading prose: {prose!r} in {verdict!r}"


def test_missing_result_message_is_not_success():
    """No ResultMessage = no evidence the turn finished. Absence of evidence must not
    be scored as success -- that is the same shape as the bug being fixed."""
    src = (Path(__file__).resolve().parent.parent / "src/feishu_bridge/fleet_bridge.py").read_text(encoding="utf-8")
    assert "r_result is None or r_result.is_error" in src, \
        "a retry with no ResultMessage would be reported as 成功 again"


def test_tool_count_omitted_when_zero():
    """Behavioral regression coverage."""
    assert "工具" not in _retry_summary(3.0, 10, 0, True, None)

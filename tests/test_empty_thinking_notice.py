"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-empty-think-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def _lines(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(b, "log", said.append)
    monkeypatch.setattr(b, "_EMPTY_THINKING_NOTED", False)
    return said


def test_says_it_once(monkeypatch):
    said = _lines(monkeypatch)
    b._note_empty_thinking()
    assert len(said) == 1, said


def test_does_not_repeat(monkeypatch):
    """每块都说就是刷屏 —— 一晚上几百块, 真正的日志会被冲掉。"""
    said = _lines(monkeypatch)
    for _ in range(50):
        b._note_empty_thinking()
    assert len(said) == 1, len(said)


def test_line_says_what_it_means_for_the_user(monkeypatch):
    """判据是「收方能不能分辨这是什么」: 要说清是**没数据**, 不是没实现、也不是坏了。"""
    said = _lines(monkeypatch)
    b._note_empty_thinking()
    line = said[0]
    assert "思考" in line and ("空" in line or "明文" in line), line
    assert "面板" in line or "前端" in line, f"得说清对用户意味着什么: {line}"


def test_line_states_the_observation_not_just_the_explanation(monkeypatch):
    """先写观测, 再写解释, 且解释要标成解释。

    「本模型不给明文」是**当下的断言** —— 哪天模型给了明文, 这行不再触发, 但如果它
    把解释写成了事实, 读旧日志的人会被一句过期的断言带偏。观测 (「thinking 文本为空」)
    永远为真, 因为那正是它触发的条件。
    """
    said = _lines(monkeypatch)
    b._note_empty_thinking()
    line = said[0]
    assert "为空" in line, f"没写观测本身: {line}"
    assert "解释" in line or "推测" in line, f"解释没标成解释: {line}"


def test_never_raises(monkeypatch):
    """留痕是旁路: log 自己炸了也不许把这一轮带走。"""
    monkeypatch.setattr(b, "_EMPTY_THINKING_NOTED", False)
    monkeypatch.setattr(b, "log", lambda _m: (_ for _ in ()).throw(RuntimeError("boom")))
    b._note_empty_thinking()          # 不抛就算过


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

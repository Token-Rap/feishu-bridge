"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="pin-app-test-")]
try:
    import fleet_bridge as fb  # noqa: E402
finally:
    sys.argv = _argv


class _Spy:
    def __init__(self):
        self.sent = []

    def broadcast(self, ev):
        self.sent.append(ev)


def _run(*, want_pin: bool, key: str = "", mid=None, boom=False):
    spy = _Spy()
    if boom:
        spy.broadcast = lambda ev: (_ for _ in ()).throw(RuntimeError("网关断了"))
    old = fb.APP_FRONT
    fb.APP_FRONT = spy
    try:
        fb._app_pin_last(want_pin, key, mid)
    finally:
        fb.APP_FRONT = old
    return spy.sent


# ── 靶心: 纯 App 模式 (mid=None) 也要发出置顶请求 ────────────────────
def test_pure_app_mode_still_pins():
    """mid 是**飞书**的 message_id —— 纯 App 下它恒为 None, 而这正是原来那条
    `if want_pin and mid` 把整件事吞掉的地方。"""
    assert _run(want_pin=True, mid=None) == [{"ev": "pin_last", "key": ""}]


def test_dual_mode_pins_the_app_side_too():
    """飞书+App 双跑时, 飞书那侧 pin 得好好的, **App 那侧一直是空的** ——
    只是没人报, 因为用户在飞书上看到置顶就以为成了。两种模式都要发。"""
    assert _run(want_pin=True, mid="om_xxx") == [{"ev": "pin_last", "key": ""}]


def test_key_is_carried_through():
    """Behavioral regression coverage."""
    assert _run(want_pin=True, key="jobs")[0]["key"] == "jobs"


def test_no_marker_no_broadcast():
    assert _run(want_pin=False) == []


# ── 失败不许再静默 ──────────────────────────────────────────────────
def test_broadcast_failure_does_not_raise():
    """广播失败不能反过来掀翻这一轮 —— 置顶是回合的附加动作, 不是回合本身。"""
    _run(want_pin=True, boom=True)


def test_failure_is_logged(capsys):
    """**这条是这次 bug 的真正教训。**

    原来的失效之所以躺着没被发现, 不是因为没实现, 是因为**没实现和没数据长得一样**:
    用户看不到置顶、看不到标记, 日志里也没有一个字。所以这条路上任何一次失败都必须
    留下痕迹 —— 哪怕只是一行日志, 也比"三层都不报错"强。
    """
    _run(want_pin=True, boom=True)
    assert "置顶" in capsys.readouterr().out


def test_success_is_logged(capsys):
    """Behavioral regression coverage."""
    _run(want_pin=True, key="jobs")
    assert "置顶" in capsys.readouterr().out












def test_empty_answer_does_not_pin():
    spy = _Spy()
    old = fb.APP_FRONT
    fb.APP_FRONT = spy
    try:
        fb._app_pin_last(True, "jobs", None, "   \n  ")
    finally:
        fb.APP_FRONT = old
    assert spy.sent == [], "空正文也发了置顶请求, 会钉到上一轮那条"


def test_empty_answer_is_logged(capsys):
    """跳过也要留痕 —— 否则"没置顶"又变成一个没有任何线索的现象 (这次 bug 的本体)。"""
    old = fb.APP_FRONT
    fb.APP_FRONT = _Spy()
    try:
        fb._app_pin_last(True, "jobs", None, "")
    finally:
        fb.APP_FRONT = old
    assert "跳过" in capsys.readouterr().out


def test_nonempty_answer_still_pins():
    """别把边界修成"一律不 pin" —— 正常回复照旧。"""
    spy = _Spy()
    old = fb.APP_FRONT
    fb.APP_FRONT = spy
    try:
        fb._app_pin_last(True, "jobs", None, "今日 6 条岗位")
    finally:
        fb.APP_FRONT = old
    assert spy.sent == [{"ev": "pin_last", "key": "jobs"}]

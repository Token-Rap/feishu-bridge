"""Behavioral regression coverage."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
_HOME = tempfile.mkdtemp(prefix="bridge-test-home-")
sys.argv = [_argv[0], _HOME]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


class StubSender:
    """记下 pin/unpin 调用序列。"""

    def __init__(self, pin_ok: bool = True):
        self.pinned: list[str] = []
        self.unpinned: list[str] = []
        self._pin_ok = pin_ok

    def pin(self, mid):
        self.pinned.append(mid)
        return self._pin_ok

    def unpin(self, mid):
        self.unpinned.append(mid)
        return True


# ── 标记解析 ────────────────────────────────────────────────────
def test_plain_marker_has_no_key():
    text, want, key = b._extract_pin_marker("正文\n[[置顶]]\n")
    assert want and key == "" and "[[置顶]]" not in text


def test_keyed_marker():
    text, want, key = b._extract_pin_marker("正文\n[[置顶:boss-digest]]\n")
    assert want and key == "boss-digest" and "[[" not in text


def test_english_pin_and_case_insensitive():
    assert b._extract_pin_marker("x\n[[PIN:daily]]\n")[2] == "daily"


def test_no_marker():
    assert b._extract_pin_marker("普通正文")[1] is False


def test_marker_must_be_own_line():
    """行内出现不算 —— 否则聊到这个语法就会被误触发 (同 session_inject 那个坑)。"""
    assert b._extract_pin_marker("我建议你写 [[置顶:x]] 这样")[1] is False


def test_empty_text():
    assert b._extract_pin_marker("") == ("", False, "")


# ── 同 key 顶替 ─────────────────────────────────────────────────
def test_keyless_never_unpins():
    """无 key = 历史行为: 纯 pin, 不撤任何东西。"""
    b.SENDER = StubSender()
    b._pin_keyed("m1", "")
    b._pin_keyed("m2", "")
    assert b.SENDER.pinned == ["m1", "m2"] and b.SENDER.unpinned == []


def test_same_key_replaces_previous():
    b.SENDER = StubSender()
    b._pin_keyed("m1", "daily")
    b._pin_keyed("m2", "daily")
    assert b.SENDER.unpinned == ["m1"]


def test_different_keys_coexist():
    b.SENDER = StubSender()
    b._pin_keyed("m1", "morning")
    b._pin_keyed("m2", "evening")
    assert b.SENDER.unpinned == []          # 谁也不顶谁


def test_state_file_shared_with_fleet_notify():
    """登记文件名必须和 fleet_notify.py 一致 —— 两条路径共用一份「谁是活的」。

    注意用 b._BRIDGE_HOME 而不是本文件的 _HOME: 模块在 import 时就从 argv 定死了 home,
    整套跑时**第一个导入它的测试文件**说了算 (单跑能过、全跑挂就是这么来的)。
    """
    b.SENDER = StubSender()
    b._pin_keyed("m9", "boss-digest")
    sp = b._BRIDGE_HOME / "pin_state_boss-digest.json"
    assert sp.exists() and json.loads(sp.read_text())["message_id"] == "m9"


def test_unsafe_key_sanitized():
    b.SENDER = StubSender()
    b._pin_keyed("m1", "a/b*c")
    assert (b._BRIDGE_HOME / "pin_state_abc.json").exists()


def test_pin_failure_skips_unpin():
    """新的没钉上就撤旧的 → 顶部会空窗。必须 pin 成功才动旧的。"""
    b.SENDER = StubSender()
    b._pin_keyed("m1", "k")
    b.SENDER = StubSender(pin_ok=False)
    b._pin_keyed("m2", "k")
    assert b.SENDER.unpinned == []


def test_pin_never_raises():
    """fail-soft 硬要求: 置顶再怎么炸也不能掀翻回合。"""
    class Boom:
        def pin(self, mid):
            raise RuntimeError("飞书挂了")

    b.SENDER = Boom()
    b._pin_keyed("m1", "k")        # 不抛就算过


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

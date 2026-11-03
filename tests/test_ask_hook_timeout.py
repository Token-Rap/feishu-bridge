"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def test_hook_timeout_leaves_margin_over_our_wait():
    """交给 CLI 的钩子超时要比我们等答案的时间明显长 (留发卡+回程的余量)。"""
    assert b.ASK_HOOK_TIMEOUT_SEC >= b.ASK_WAIT_SEC + 30


def test_matcher_carries_the_timeout():
    """HookMatcher 必须真的带上 timeout —— 不带就回落 CLI 默认 600s, 又撞上。"""
    m = b._ask_hook_matcher("chat-x")
    assert m.matcher == "AskUserQuestion"
    assert m.timeout == b.ASK_HOOK_TIMEOUT_SEC
    assert len(m.hooks) == 1


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

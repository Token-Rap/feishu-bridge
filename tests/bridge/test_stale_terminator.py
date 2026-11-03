"""Behavioral regression coverage."""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

_TMP_HOME = tempfile.mkdtemp(prefix="bridge-test-home-")
_argv = sys.argv
sys.argv = [_argv[0], _TMP_HOME]
try:
    from feishu_bridge.bridge import fleet_bridge as b  # noqa: E402
finally:
    sys.argv = _argv

_SRC = (_REPO / "src/feishu_bridge/bridge/fleet_bridge.py").read_text("utf-8")


def test_interrupted_flag_tracks_set():
    b.INTERRUPTED.discard("c1")
    assert b.interrupted_flag("c1") is False
    b.INTERRUPTED.add("c1")
    assert b.interrupted_flag("c1") is True
    b.INTERRUPTED.discard("c1")


def test_receive_loop_is_wrapped_for_retry():
    """收流循环必须包在重试 for 里, 否则空回合会直接发空回复。"""
    assert re.search(r"for _stale_try in range\(2\):\s*\n\s*saw_activity = False\s*\n"
                     r"\s*async for msg in client\.receive_response\(\):", _SRC), \
        "receive_response 循环没有被自愈重试包裹"


def test_activity_marked_on_both_content_paths():
    """流式增量与整块消息都要标活动 — 只标一个会漏判 (非流式 bot / 流式失败回退)。"""
    assert "partial_buf += delta[\"text\"]\n                        saw_activity = True" in _SRC
    assert "elif isinstance(msg, AssistantMessage):\n                    saw_activity = True" in _SRC


def test_retry_guarded_against_infinite_and_interrupt():
    """重试必须有三重闸: 有产出 / 用户中断 / 已重试过 —— 任一即停, 不能空转。"""
    m = re.search(r"if saw_activity or interrupted_flag\(chat_id\) or _stale_try or prompt == \"/compact\":\s*\n\s*break", _SRC)
    assert m, "自愈退出条件缺失或不完整"
    assert "range(2)" in _SRC, "重试次数必须有上限"


def test_result_msg_reset_before_retry():
    """重读前要清掉陈旧 ResultMessage, 否则它的 usage/cost 会被记进本轮统计。"""
    assert re.search(r"重新读取本轮真实响应\"\)\s*\n\s*result_msg = None", _SRC)


def test_compact_command_sends_raw_slash():
    """Behavioral regression coverage."""
    assert 'await run_turn(chat_id, "/compact")' in _SRC
    assert '"/compact"' in _SRC.split("async def handle_command", 1)[1]


def test_compact_turn_exempt_from_stale_retry():
    """/compact 回合零模型活动是正常形态, 自愈重试会把真结果当残留扔掉后永久阻塞。"""
    assert 'or prompt == "/compact":' in _SRC


def test_network_error_auto_retry_is_a_continuation():
    'Retry sends a continuation rather than replaying the original prompt.'
    assert "_READONLY_TOOLS" in _SRC and "saw_unsafe_tool" in _SRC
    assert 'await client.query("重试")' in _SRC, "重试必须是 continuation, 不是原 prompt"
    # 写类工具仍要标记不安全 —— 它现在只影响提示文案, 不再拦住重试
    assert "block.name not in _READONLY_TOOLS" in _SRC
    # 那句文案仍在 (给"没重试过"的那种失败用)。T022 后文案在 fleet_failures。
    failures = (_REPO / "src/feishu_bridge/bridge/fleet_failures.py").read_text("utf-8")
    assert "未自动重试以防重复" in failures


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

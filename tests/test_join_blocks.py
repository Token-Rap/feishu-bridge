"""跨 TextBlock 拼接补换行 (shopping 报: 工具调用后的围栏块失效)。

一轮里「文字 → 调工具 → 围栏块」时, 两段文本直接首尾相接 → ``` 不在行首 →
Markdown 不认 → doc/map 卡片退化成普通文字。
"""
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

J = b._join_blocks


def test_fence_after_text_gets_blank_line():
    out = J("比我在这儿翻促销文靠谱。", '```doc\n{"name": "Mac mini.md"}\n```')
    assert "\n\n```doc" in out
    assert out.splitlines()[-3].startswith("```doc") or "```doc" in out.split("\n\n")[1]


def test_plain_continuation_gets_single_newline():
    out = J("我来查一下。", "查到了, 一共 3 条。")
    assert out == "我来查一下。\n查到了, 一共 3 条。"


def test_no_double_newline_when_already_terminated():
    out = J("上一段结束了。\n", "```doc\n{}\n```")
    assert out == "上一段结束了。\n```doc\n{}\n```"      # 已有换行 → 不再补


def test_empty_buffer_unchanged():
    assert J("", "```doc\n{}\n```") == "```doc\n{}\n```"


def test_other_block_starts():
    for head in ("# 标题", "| a | b |", "> 引用", "- 列表项", "1. 有序项"):
        assert J("正文。", head).startswith("正文。\n\n"), head


def test_leading_newlines_in_next_block_still_detected():
    out = J("正文。", "\n```map\n{}\n```")
    assert out.startswith("正文。\n\n")


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

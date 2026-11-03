"""Behavioral regression coverage."""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="ask-stamp-")]
try:
    from feishu_bridge.bridge import fleet_bridge as FB
finally:
    sys.argv = _argv


def _hook_src() -> str:
    """提问钩子那一段源码 (ask 与三条 ask_cancel 都在里面)。"""
    src = Path(FB.__file__).read_text(encoding="utf-8")
    i = src.index("    async def _hook(input_data, tool_use_id, context)")
    # ⚠ 收尾锚点**不能用 `ASK_CUT.add(chat_id)`**: 它在超时分支里先出现一次,
    # 段落会被截在那儿, 漏掉后两条 ask_cancel —— 而测试照样"通过 2 条"。
    # 第一版就是这么写的, 是它在**新代码上也红**才暴露出来 (守门的测试自己先被验一次)。
    # 改用「下一个同缩进的 def」收尾, 整个钩子函数一个不漏。
    j = src.index("\n    def ", i)
    k = src.find("\n    async def ", i + 10)
    end = min(x for x in (j, k) if x > i)
    return src[i:end]


def test_ask_frame_carries_chat_id_explicitly():
    seg = _hook_src()
    i = seg.index('"ev": "ask"')
    assert '"chat_id": ui_chat' in seg[i:i + 200], "ask 帧没显式盖章"


def test_every_ask_cancel_carries_chat_id_explicitly():
    """Behavioral regression coverage."""
    seg = _hook_src()
    spots = [m.start() for m in re.finditer(r'"ev": "ask_cancel"', seg)]
    assert len(spots) == 3, f"这段里应当有 3 条 ask_cancel, 找到 {len(spots)} —— 锚点漂了"
    for i in spots:
        chunk = seg[i:i + 220]
        assert '"chat_id": ui_chat' in chunk, f"这条 ask_cancel 没盖章: {chunk[:90]!r}"
        assert '"reason"' in chunk


def test_reasons_are_all_three_and_distinct():
    """三条各有自己的 reason —— 客户端按 reason 分「已答/已取消」两种画法。"""
    seg = _hook_src()
    assert {m.group(1) for m in re.finditer(r'"reason": "(\w+)"', seg)} == \
        {"timeout", "cancelled", "answered"}


def test_not_relying_on_contextvar_in_the_hook():
    """⚠ 这条是**设计约束**: 钩子里不许再出现靠 CUR_CHAT 兜底的广播。

    不是"现在对了", 是"以后有人加第四条 ask_cancel 时也得显式带上"。
    """
    seg = _hook_src()
    for m in re.finditer(r'APP_FRONT\.broadcast\(\{([^}]*)\}', seg):
        body = m.group(1)
        if '"ask' in body:          # ask / ask_cancel 两族
            assert "chat_id" in body, f"钩子里这条广播没带会话: {body[:80]!r}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="thinking-test-")]
try:
    from feishu_bridge.bridge import fleet_bridge as fb  # noqa: E402
finally:
    sys.argv = _argv


def test_asks_for_summarized_thinking():
    """靶心: 显式要摘要, 而不是留给模型默认值。"""
    assert fb._thinking_opt() == {"thinking": {"type": "adaptive", "display": "summarized"}}


def test_never_omitted():
    """Behavioral regression coverage."""
    assert fb._thinking_opt().get("thinking", {}).get("display") != "omitted"


def test_missing_field_yields_empty_not_crash():
    """Behavioral regression coverage."""
    class _Fake:
        pass

    old = fb.ClaudeAgentOptions
    fb.ClaudeAgentOptions = _Fake          # 不是 dataclass → fields() 抛 TypeError
    try:
        assert fb._thinking_opt() == {}
    finally:
        fb.ClaudeAgentOptions = old


def test_spread_not_hardcoded_kwarg():
    """构造处必须是 `**_thinking_opt()` —— 写成 `thinking=...` 的话, 上面那条
    fail-soft 就白做了 (字段不存在时照样 TypeError)。"""
    src = (Path(__file__).resolve().parent.parent / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("opts = ClaudeAgentOptions("):]
    seg = seg[:seg.index("include_partial_messages")]
    assert "**_thinking_opt()" in seg
    assert "thinking={" not in seg, "硬传了 thinking=, fail-soft 失效"

"""Behavioral regression coverage."""
import sys
import tempfile

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def test_compact_turn_is_marked():
    assert b._merge_reply_meta({}, True) == {"kind": "compact_done"}


def test_normal_turn_has_no_extra_key():
    """⚠ 正常回合**一个字段都不许多** —— 注释写死「正常回合是空 dict ⇒
    三个字段一个都不出现在 reply 里, 客户端据此当正常回复画」。"""
    assert b._merge_reply_meta({}, False) == {}


def test_failure_fields_survive():
    """失败元信息不许被这条盖掉 —— 两者可以同时成立 (压缩回合也可能失败)。"""
    out = b._merge_reply_meta({"failed": True, "retry_hint": "x"}, True)
    assert out["failed"] is True and out["retry_hint"] == "x"
    assert out["kind"] == "compact_done"


def test_does_not_mutate_the_caller_dict():
    """就地改会污染调用方 —— `_fail` 在下游还要被用两次 (stream.finish / send_reply)。"""
    src = {"failed": True}
    b._merge_reply_meta(src, True)
    assert src == {"failed": True}, src


def test_the_confirmation_text_is_one_constant():
    """Behavioral regression coverage."""
    src = Path(b.__file__).read_text(encoding="utf-8")
    assert src.count("上下文压缩完成") == 1, "这句话被抄了不止一处"


def test_the_flag_is_actually_wired_into_the_reply_path():
    """⚠ 供给断言: 上面几条都在测一个纯函数, 证明不了它**真的被接进回合里**。"""
    src = Path(b.__file__).read_text(encoding="utf-8")
    i = src.index('if prompt == "/compact" and not answer_buf.strip():')
    j = src.index("_merge_reply_meta(", i)
    assert j - i < 3000, "标记没接在压缩那条分支和 reply 构造之间"











def test_start_notice_can_carry_a_kind():
    from feishu_bridge import app_frontend

    class _Null:
        def __init__(self): self.got = []
        def send(self, chat, text): self.got.append((chat, text))

    class _Front:
        def __init__(self): self.evs = []
        def broadcast(self, ev): self.evs.append(ev)

    inner, front = _Null(), _Front()
    t = app_frontend.TeeTransport(inner, front)
    t.send("c", "🗜 开始压缩上下文…", kind="compact_start")
    assert front.evs[-1]["kind"] == "compact_start", front.evs[-1]
    # ⚠ 内层 transport 的签名**不许被污染** —— 飞书那侧不认识 kind
    assert inner.got[-1] == ("c", "🗜 开始压缩上下文…")


def test_ordinary_notice_has_no_kind():
    from feishu_bridge import app_frontend

    class _Null:
        def send(self, chat, text): pass

    class _Front:
        def __init__(self): self.evs = []
        def broadcast(self, ev): self.evs.append(ev)

    front = _Front()
    app_frontend.TeeTransport(_Null(), front).send("c", "普通通知")
    assert "kind" not in front.evs[-1], front.evs[-1]


def test_the_start_call_site_passes_the_kind():
    """⚠ 供给断言: 上面两条测的是能力, 证明不了压缩那条路**真的用了**它。"""
    src = Path(b.__file__).read_text(encoding="utf-8")
    i = src.index("🗜 开始压缩上下文")
    seg = src[max(0, i - 200):i + 200]
    assert 'kind="compact_start"' in seg, "开始那句没带标记"

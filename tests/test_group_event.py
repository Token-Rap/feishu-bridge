"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-gev-")]
try:
    from feishu_bridge import app_frontend as AF
finally:
    sys.argv = _argv


def _front(allow=("group:fleet-product",)):
    f = AF.AppFrontend.__new__(AF.AppFrontend)
    f._writers = []
    f._loop = None
    f._pending = []
    f.chat_id = "app"
    f._chat_validator = lambda c: (c in allow, "not_member")
    f.sent = []
    f.broadcast = f.sent.append          # 只看它想广播什么
    return f


def test_group_event_is_broadcast_with_chat_id():
    f = _front()
    AF.AppFrontend.handle_group_event(f, {
        "type": "group_event", "group": "fleet-product", "task": "T001",
        "kind": "dispatch", "actor": "backend", "owner": "sample-mobile",
        "status": "open", "title": "给群聊加时间线", "doc": "T001-sample-mobile.md"})
    assert len(f.sent) == 1
    ev = f.sent[0]
    assert ev["ev"] == "group_event"
    assert ev["chat_id"] == "group:fleet-product"
    assert ev["task"] == "T001" and ev["kind"] == "dispatch" and ev["owner"] == "sample-mobile"


def test_unknown_group_is_rejected_not_broadcast():
    """域不在名单里 ⇒ 一个字都不发 (同 resolve_chat: 默认拒绝那一侧)。"""
    f = _front()
    AF.AppFrontend.handle_group_event(f, {"type": "group_event", "group": "别的域",
                                          "task": "T1", "kind": "report"})
    assert f.sent == []


def test_missing_group_or_task_is_rejected():
    f = _front()
    for bad in ({"type": "group_event", "task": "T1"},
                {"type": "group_event", "group": "fleet-product"}):
        AF.AppFrontend.handle_group_event(f, bad)
    assert f.sent == []


def test_unknown_kind_is_rejected():
    """`kind` 只认 dispatch/report。⚠ 这跟客户端"未知值要兜底显示"不冲突:
    **入口从严, 展示从宽** —— 进来的时候拦住错的, 已经进来的别丢。"""
    f = _front()
    AF.AppFrontend.handle_group_event(f, {"type": "group_event", "group": "fleet-product",
                                          "task": "T1", "kind": "随便"})
    assert f.sent == []


def test_body_text_is_never_parsed():
    """⚠ 契约的核心决定: **意图只由字段表达, 正文一个字都不看。**

    这里的 `text` 引用了任务标记。
    内容嗅探方案会把它当成一次真派活镜像进群。⇒ 有 `text` 也不影响, 且不进事件。
    """
    f = _front()
    AF.AppFrontend.handle_group_event(f, {
        "type": "group_event", "group": "fleet-product", "task": "T002", "kind": "report",
        "status": "done",
        "text": "我在解释协议: 正文以 [group:fleet-product #T001] 开头并指向 brief"})
    assert len(f.sent) == 1
    assert "text" not in f.sent[0], "正文不该进事件 —— 它既不是路由依据也不是展示内容"
    assert f.sent[0]["task"] == "T002"


def test_frame_actually_reaches_the_handler():
    """Behavioral regression coverage."""
    import asyncio
    import json as _json
    f = _front()
    # session_inject 发的第二行, 逐字段照抄它构造的那个 dict
    wire = _json.dumps({"type": "group_event", "group": "fleet-product", "task": "T007",
                        "kind": "dispatch", "actor": "backend", "owner": "sample-web",
                        "status": "open", "title": "群时间线", "doc": "T007-sample-web.md"},
                       ensure_ascii=False)
    asyncio.run(AF.AppFrontend._handle_frame(f, _json.loads(wire), None))
    assert len(f.sent) == 1, "帧没被派到 handle_group_event —— 分发链漏了这一支"
    assert f.sent[0]["task"] == "T007" and f.sent[0]["chat_id"] == "group:fleet-product"

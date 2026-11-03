"""`app_frontend.user_event()` —— `ev:"user"` 帧的唯一构造器。

⚠ 这些断言钉的是**与手写字面量逐字等价**, 不是"看起来合理"。
构造器存在的意义就是收口 6 个产生点; 只要它和其中任何一处不等价,
收口这个动作本身就会改行为, **而改的那一处不会报错**。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.bridge import app_frontend


def test_omits_optional_keys_when_absent():
    ev = app_frontend.user_event("hi", origin="feishu")
    assert ev == {"ev": "user", "origin": "feishu", "text": "hi"}


def test_user_omitted_when_empty():
    assert "user" not in app_frontend.user_event("x", origin="web", user="")
    assert app_frontend.user_event("x", origin="web", user="gzt")["user"] == "gzt"


def test_explicit_empty_chat_id_keeps_the_key():
    """显式空 chat_id 必须保留，避免扇出时走默认会话。"""
    ev = app_frontend.user_event("x", origin="app", chat_id="")
    assert "chat_id" in ev, "显式传空 chat_id 被吞掉了 —— 扇出点会改走兜底分支"
    assert ev["chat_id"] == ""


def test_not_passing_chat_id_leaves_it_to_the_fanout():
    assert "chat_id" not in app_frontend.user_event("x", origin="feishu")


def test_empty_chat_id_survives_the_fanout_stamp():
    """端到端: 显式空 chat_id 经过 `_with_chat` 后**不该**被兜底改写。"""
    front = app_frontend.AppFrontend.__new__(app_frontend.AppFrontend)
    front.chat_id = "oc_fallback"
    ev = front._with_chat(app_frontend.user_event("x", origin="app", chat_id=""))
    assert ev["chat_id"] == "", f"被兜底改成了 {ev['chat_id']!r}"

"""Every model input carries its display route, including mixed-scope queued messages."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-message-route-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


def test_group_stamp_has_machine_readable_route_fields():
    prompt = b._stamp("群里的问题", 1.0, "group:fleet-product")
    assert ('[会话路由 {"is_group":true,"chat_id":"group:fleet-product",'
            '"group":"fleet-product"}]') in prompt
    assert b._msg_label(prompt) == "群里的问题"


def test_private_stamp_is_explicit_not_inferred_from_engine_session():
    prompt = b._stamp("私聊问题", 1.0, "app")
    assert '[会话路由 {"is_group":false,"chat_id":"app","group":""}]' in prompt
    assert b._msg_label(prompt) == "私聊问题"


def test_steering_batch_preserves_each_messages_own_route():
    prompt = b._steering_batch([
        ("先在私聊补一句", 1.0, "app"),
        ("再从群里补一句", 2.0, "group:fleet-product"),
    ])
    assert prompt is not None
    assert prompt.count('"is_group":false') == 1
    assert prompt.count('"is_group":true') == 1
    assert prompt.index("先在私聊补一句") < prompt.index("再从群里补一句")

"""Synthetic managed scope and real frontend non-fallback regressions."""
import asyncio
import json
from pathlib import Path

import pytest

from feishu_bridge.managed.fleet_managed_ingress import Refused, Selection, gateway_message
from feishu_bridge.bridge.app_frontend import AppFrontend


def frame(**changes):
    req = {"type": "user_message", "text": " 原文\n中文👩‍💻\r\n ",
           "work_id": "gateway:actual-request-1", "client": "web.1009a",
           "work_actor": "actual-human",
           "message_origin": {"version": 1, "kind": "gateway_human"}}
    req.update(changes)
    return req


def prepare(req, **changes):
    scope = {"work_chat": "work::codex", "display_chat": "group:fleet-product",
             "engine": "codex", "bot": "shopping", "gateway_connection": True}
    scope.update(changes)
    return gateway_message(req, **scope)


def test_scope_retains_complete_text_and_only_whitelist():
    req = frame(from_ignored="not actor", token="private-not-exported", headers={"secret": "x"})
    req["from"] = "actual-human"
    r = prepare(req)
    p = json.loads(r.payload)
    assert p["text"] == req["text"]
    assert p["actor"] == "actual-human" and p["bot"] == "shopping"
    assert r.work_chat == "work::codex" and r.display_chat == "group:fleet-product"
    assert set(p) == {"version", "kind", "work_id", "work_chat", "display_chat",
                      "engine", "bot", "client", "actor", "display_actor", "text"}
    assert b"private-not-exported" not in r.payload


@pytest.mark.parametrize("changes", [
    {"work_id": None}, {"work_id": 1}, {"work_id": ""}, {"work_id": "bad id"},
    {"work_actor": None}, {"work_actor": ""}, {"work_actor": True},
    {"text": None}, {"text": True}, {"text": "\ud800"}, {"text": " \n "},
    {"internal_group_task": True}, {"internal_group_task": 0},
    {"peer_report": {}}, {"type": "answer"},
    {"message_origin": {"version": True, "kind": "gateway_human"}},
    {"message_origin": {"version": 1, "kind": "gateway_human", "extra": 1}},
])
def test_original_shapes_refuse_without_formatting(changes):
    with pytest.raises(Refused) as exc:
        prepare(frame(**changes))
    assert "bad id" not in str(exc.value) and "extra" not in str(exc.value)


def test_unknown_connection_and_over_capacity_refuse():
    with pytest.raises(Refused, match="managed_origin_unknown"):
        prepare(frame(), gateway_connection=False)
    with pytest.raises(Refused):
        prepare(frame(text="x" * (1024 * 1024)))  # Envelope counts in total cap.


def test_selection_absence_is_legacy_and_partial_never_falls_back(tmp_path):
    assert Selection.from_env({}, tmp_path) is None
    valid = {"FLEET_TARGET_CONTROL_REQUIRED": "1", "FLEET_TARGET_CONTROL_DIR": str(tmp_path),
             "FLEET_TARGET_CONTROL_ID": "shopping-release-1"}
    assert Selection.from_env(valid, tmp_path).target_home == tmp_path
    for key in valid:
        bad = dict(valid); bad.pop(key)
        with pytest.raises(Refused):
            Selection.from_env(bad, tmp_path)
    bad = dict(valid, FLEET_TARGET_CONTROL_REQUIRED="0")
    with pytest.raises(Refused):
        Selection.from_env(bad, tmp_path)


@pytest.mark.parametrize("kind", ["user_message", "answer", "user_image", "set_runtime",
                                 "interrupt", "command", "term_open", "group_event"])
def test_real_frontend_managed_handler_short_circuits_all_legacy_mutations(tmp_path, kind):
    frontend = AppFrontend(tmp_path, lambda _: None)
    called = []

    async def managed(req, writer):
        called.append((req, writer))
        return None  # Returning without admission must not trigger legacy fallback.

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy dispatch or success ACK")

    frontend._managed_ingress = managed
    frontend._inject_ack = forbidden
    frontend._inject = forbidden
    frontend._inject_image = forbidden
    frontend._answer_ask = forbidden
    frontend._set_runtime = forbidden
    frontend._interrupt = forbidden
    frontend._command = forbidden
    frontend._terms = None
    req = frame(type=kind)
    asyncio.run(frontend._handle_frame(req, None))
    assert called == [(req, None)]


def test_real_frontend_managed_exception_cannot_ack_or_dispatch(tmp_path):
    frontend = AppFrontend(tmp_path, lambda _: None)

    async def managed(req, writer):
        raise Refused("admission_closed")

    frontend._managed_ingress = managed
    frontend._inject_ack = lambda *a: pytest.fail("success ACK on refusal")
    frontend._inject = lambda *a: pytest.fail("legacy dispatch on refusal")
    with pytest.raises(Refused, match="admission_closed"):
        asyncio.run(frontend._handle_frame(frame(), None))


@pytest.mark.parametrize("client", [None, True, False, 1, "", " ", "web invalid", "web\nforged", "x" * 129, {}, []])
def test_actual_original_client_shape_is_required_before_preparation(client):
    with pytest.raises(Refused, match="identity_invalid"):
        prepare(frame(client=client))


def test_absent_client_is_not_manufactured_into_unknown():
    req = frame()
    req.pop("client")
    with pytest.raises(Refused, match="identity_invalid"):
        prepare(req)


def test_explicit_client_identity_is_retained_without_normalization():
    req = frame(client="ios.744ccca")
    assert json.loads(prepare(req).payload)["client"] == req["client"]


def test_selected_bridge_startup_refuses_before_legacy_native_or_filesystem(tmp_path):
    import ast
    from pathlib import Path
    from feishu_bridge.managed import fleet_managed_bootstrap
    from types import SimpleNamespace
    source = Path(__file__).parents[1] / "src/feishu_bridge/bridge/fleet_bridge.py"
    tree = ast.parse(source.read_text())
    guard = next(n for n in tree.body if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "_managed_startup_binding")
    env = {"FLEET_TARGET_CONTROL_REQUIRED": "1", "FLEET_TARGET_CONTROL_DIR": str(tmp_path),
           "FLEET_TARGET_CONTROL_ID": "fixture-selector", "FLEET_BRIDGE_HOME": str(tmp_path)}
    ns = {"os": SimpleNamespace(environ=env), "sys": SimpleNamespace(argv=[str(source)]),
          "_managed_startup_binding": fleet_managed_bootstrap.require_startup_binding}
    with pytest.raises(Refused, match="managed_full_lifecycle_unbound"):
        exec(compile(ast.fix_missing_locations(ast.Module(body=[guard], type_ignores=[])), str(source), "exec"), ns)
    guard_index = tree.body.index(guard)
    for n in tree.body[:guard_index]:
        if isinstance(n, ast.ImportFrom):
            assert n.module in {"__future__", "feishu_bridge.managed.fleet_managed_bootstrap"}
        elif isinstance(n, ast.Import):
            assert all(a.name in {"os", "sys"} for a in n.names)
    fleet_managed_bootstrap.require_startup_binding({}, [str(source)])

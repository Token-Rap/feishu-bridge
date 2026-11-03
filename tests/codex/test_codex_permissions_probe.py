"""Probe preconditions and isolation through the real adapter, no live model calls."""
import asyncio
import copy
import json

import pytest

from feishu_bridge.codex import codex_approvals as ca
from tests.codex.test_codex_approvals import bridge, answer


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_default_probe_does_not_change_normal_policy(value):
    assert not ca.permissions_probe(value, "never", {}, "danger-full-access")


@pytest.mark.parametrize("value,policy,reviewer,sandbox", [
    ("typo", "on-request", {"approvalsReviewer": "user"}, "read-only"),
    ("true", "never", {"approvalsReviewer": "user"}, "read-only"),
    ("true", "on-request", {}, "read-only"),
    ("true", "on-request", {"approvalsReviewer": "auto_review"}, "read-only"),
    ("true", "on-request", {"approvalsReviewer": "user"}, "danger-full-access"),
])
def test_invalid_probe_cannot_relax_settings(value, policy, reviewer, sandbox):
    with pytest.raises(ValueError):
        ca.permissions_probe(value, policy, reviewer, sandbox)


@pytest.mark.parametrize("value", ["true", "1", " TRUE "])
def test_probe_requires_explicit_restricted_mode(value):
    assert ca.permissions_probe(value, "on-request", {"approvalsReviewer": "user"}, "read-only")


def run_adapter(monkeypatch, tmp_path, *, thread_change=None, pages=None, probe="true"):
    b = bridge()
    from tests.codex_storage_fixture import make_storage, original_protocol
    fixture=make_storage(monkeypatch,tmp_path)
    monkeypatch.setattr(b._fs,"SESSIONS_FILE",fixture.scope.home.parent/"sessions.json")
    from feishu_bridge.bridge.app_frontend import AppFrontend, DISPLAY_CHAT
    async def run():
        reader = asyncio.StreamReader()
        written, emitted, saved, status, ledger = [], [], [], [], []
        thread = {"thread": {"id": "ephemeral-fixture"}, "approvalPolicy": "on-request",
                  "approvalsReviewer": "user", "sandbox": {"type": "readOnly", "networkAccess": False}}
        if thread_change:
            thread.update(thread_change)
        feature_pages = copy.deepcopy(pages if pages is not None else [
            {"data": [{"name": "request_permissions_tool", "enabled": True}], "nextCursor": None}])
        def enqueue(x): reader.feed_data((json.dumps(x) + "\n").encode())
        class Writer:
            def write(self, raw):
                x = json.loads(raw); written.append(x)
                method = x.get("method")
                if method == "initialize": enqueue({"id": x["id"], "result": {}})
                elif method == "config/read":enqueue({"id":x["id"],"result":{"config":fixture.config}})
                elif method in {"thread/start", "thread/resume"}:
                    enqueue({"id": x["id"], "result": thread})
                elif method == "experimentalFeature/list":
                    page = feature_pages.pop(0) if len(feature_pages) > 1 else feature_pages[0]
                    enqueue({"id": x["id"], "result": page})
                elif method == "turn/start":
                    enqueue({"id": x["id"], "result": {"turn": {"id": "fixture-turn"}}})
                    enqueue({"id": 100, "method": ca.PERMISSIONS_METHOD, "params": {
                        "threadId": "ephemeral-fixture", "turnId": "fixture-turn", "itemId": "item",
                        "environmentId": "fixture", "permissions": {"fileSystem": {"write": ["/fixture/scratch"]}}}})
                elif "method" not in x and x.get("id") == 100:
                    enqueue({"method": "serverRequest/resolved", "params": {
                        "threadId": "ephemeral-fixture", "requestId": 100}})
                    enqueue({"method": "item/agentMessage/delta", "params": {"delta": "fixture complete"}})
                    enqueue({"method": "turn/completed", "params": {
                        "threadId": "ephemeral-fixture", "turn": {"id": "fixture-turn", "status": "completed"}}})
            async def drain(self): pass
            def close(self): pass
        class Proc:
            stdin, stdout, stderr, returncode = Writer(), reader, None, 0
            async def wait(self): return 0
        async def spawn(*args, **kwargs): return Proc()
        async def drop(*args): pass
        class Sender:
            feishu = None
            def send_reply(self, *args, **kwargs): return "reply"
        front = AppFrontend(tmp_path, log=lambda _: None)
        front._approval_answer = b._app_answer_codex_approval
        def emit(event):
            emitted.append(event)
            if event["ev"] == "ask":
                asyncio.create_task(front._handle_frame(answer(event["ask_id"], ca.GRANT), None))
        front.broadcast = emit
        monkeypatch.setattr(b, "APP_FRONT", front)
        monkeypatch.setattr(b, "SENDER", Sender())
        monkeypatch.setattr(b, "CODEX_ENABLED", True)
        monkeypatch.setattr(b, "CODEX_PERMISSIONS_PROBE", probe)
        monkeypatch.setattr(b, "CODEX_APPROVAL_POLICY", "on-request")
        monkeypatch.setattr(b, "CODEX_APPROVALS_REVIEWER", "user")
        monkeypatch.setattr(b, "CODEX_SANDBOX", "read-only")
        monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-6.1-sol", "high", "codex"))
        monkeypatch.setattr(b, "_load_session", lambda _: "normal-preserved-thread")
        monkeypatch.setattr(b, "_read_sessions", lambda: {})
        def forbid(*args, **kwargs): raise AssertionError("probe touched normal rollover state")
        monkeypatch.setattr(b._ro, "peek_pending", forbid)
        monkeypatch.setattr(b._ro, "take_pending", forbid)
        monkeypatch.setattr(b, "_drop_client", drop)
        monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(b, "_record_session", lambda *args, **kwargs: saved.append(args))
        monkeypatch.setattr(b, "_write_codex_status", lambda *args, **kwargs: status.append(args))
        monkeypatch.setattr(b, "_append_codex_ledger", lambda *args, **kwargs: ledger.append(args))
        for name in ("_remember", "_app_pin_last"):
            monkeypatch.setattr(b, name, lambda *args, **kwargs: None)
        token = DISPLAY_CHAT.set("group:fleet-product")
        try: await asyncio.wait_for(b._run_turn_codex("probe-chat", "fixture only"), 2)
        finally: DISPLAY_CHAT.reset(token)
        assert saved == [] and status == []
        assert ledger
        assert "probe-chat" not in b.CODEX_APPROVALS and "probe-chat" not in b.CODEX_TURNS
        return original_protocol(written, fixture), emitted
    return asyncio.run(run())


def test_real_adapter_checks_feature_before_turn_and_preserves_session(monkeypatch, tmp_path):
    written, emitted = run_adapter(monkeypatch, tmp_path)
    methods = [x.get("method") for x in written]
    assert "thread/resume" not in methods
    start = next(x for x in written if x.get("method") == "thread/start")
    assert start["params"]["ephemeral"] is True
    assert start["params"]["config"] == {"features.request_permissions_tool": True}
    assert methods.index("experimentalFeature/list") < methods.index("turn/start")
    query = next(x for x in written if x.get("method") == "experimentalFeature/list")
    assert query["params"]["threadId"] == "ephemeral-fixture"
    notice = next(x for x in emitted if x["ev"] == "notice")
    assert notice["chat_id"] == "group:fleet-product" and "尚未申请或授予" in notice["text"]
    assert next(x for x in written if x.get("id") == 100)["result"] == {
        "permissions": {"fileSystem": {"write": ["/fixture/scratch"]}}, "scope": "turn"}


@pytest.mark.parametrize("change", [
    {"approvalPolicy": "never"}, {"approvalsReviewer": "auto_review"},
    {"sandbox": {"type": "dangerFullAccess"}},
    {"sandbox": {"type": "readOnly", "networkAccess": True}},
    {"sandbox": {"type": "readOnly"}},
])
def test_effective_policy_mismatch_stops_before_model(monkeypatch, tmp_path, change):
    written, emitted = run_adapter(monkeypatch, tmp_path, thread_change=change)
    assert not any(x.get("method") == "turn/start" for x in written)
    assert any(x["ev"] == "error" for x in emitted)
    assert not any(x["ev"] in {"ask", "notice"} for x in emitted)


@pytest.mark.parametrize("pages", [
    [{"data": []}],
    [{"data": [{"name": "request_permissions_tool", "enabled": False}]}],
    [{"data": [{"name": "request_permissions_tool", "enabled": "true"}]}],
    [{"data": [{"name": "request_permissions_tool", "enabled": True}]*2}],
    [{"data": [], "nextCursor": "repeat"}],
    [{"data": {}, "nextCursor": None}],
    [{"data": [], "nextCursor": 9}],
])
def test_unconfirmed_feature_never_starts_model(monkeypatch, tmp_path, pages):
    written, emitted = run_adapter(monkeypatch, tmp_path, pages=pages)
    assert not any(x.get("method") == "turn/start" for x in written)
    assert any(x["ev"] == "error" for x in emitted)
    assert not any(x["ev"] == "ask" for x in emitted)


def test_feature_can_be_found_on_later_page(monkeypatch, tmp_path):
    written, emitted = run_adapter(monkeypatch, tmp_path, pages=[
        {"data": [{"name": "unrelated", "enabled": True}], "nextCursor": "page2"},
        {"data": [{"name": "request_permissions_tool", "enabled": True}]}])
    queries = [x for x in written if x.get("method") == "experimentalFeature/list"]
    assert len(queries) == 2 and queries[1]["params"]["cursor"] == "page2"
    assert any(x["ev"] == "ask" for x in emitted)

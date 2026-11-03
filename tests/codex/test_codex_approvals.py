from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.codex import codex_approvals as ca


def request(rid=10, **params):
    return {"id": rid, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thread", "turnId": "turn", "itemId": "item",
        "command": "echo fixture", "cwd": "/fixture", **params}}


def answer(ask, label=ca.ACCEPT, **extra):
    return {"type": "answer", "ask_id": ask, "chat_id": "group:fleet-product",
            "answers": [{"header": ca.HEADER, "answer": label}], **extra}


def harness(**kwargs):
    sent, events, logs = [], [], []
    async def send(frame):
        sent.append(frame)
    manager = ca.Approvals("thread", "turn", "group:fleet-product", send,
                           events.append, logs.append, enabled=True, **kwargs)
    return manager, sent, events, logs


async def flush():
    # Drain future -> wait_for -> write -> finally, without real external time.
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.parametrize("label,decision", [(ca.ACCEPT, "accept"), (ca.DECLINE, "decline"),
                                            ("好的", "decline"), ("acceptForSession", "decline")])
def test_explicit_decision_only(label, decision):
    async def run():
        m, sent, events, logs = harness()
        assert m.feed(request())
        card = events[0]
        assert card["chat_id"] == m.chat
        assert "echo fixture" in card["questions"][0]["question"]
        assert card["questions"][0]["options"][0]["label"] == ca.DECLINE
        assert sent == []
        assert m.answer(answer(card["ask_id"], label))
        assert not m.answer(answer(card["ask_id"]))
        await flush()
        assert sent == [{"id": 10, "result": {"decision": decision}}]
        assert events[-1]["ev"] == "ask_cancel"
        assert not m.answer(answer(card["ask_id"]))
        assert any("decision=" + decision in line for line in logs)
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"answers": [], "text": ca.ACCEPT},
    {"answers": [{"header": "Other", "answer": ca.ACCEPT}]},
    {"answers": [{"header": ca.HEADER, "answer": ca.ACCEPT, "label": ca.DECLINE}]},
    {"answers": [{"header": ca.HEADER, "answer": ca.ACCEPT}] * 2},
    {"answers": "批准本次"},
])
def test_malformed_answer_declines(change):
    async def run():
        m, sent, events, _ = harness()
        m.feed(request())
        assert m.answer(answer(events[0]["ask_id"], **change))
        await flush()
        assert sent[0]["result"] == {"decision": "decline"}
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("params", [
    {"threadId": "foreign"}, {"turnId": "old"}, {"itemId": []},
    {"command": ""}, {"command": "x" * 25000},
    {"availableDecisions": ["acceptForSession", "decline"]},
])
def test_incomplete_or_unsafe_preview_declines(params):
    async def run():
        m, sent, events, _ = harness()
        m.feed(request(**params))
        await flush()
        assert not any(e["ev"] == "ask" for e in events)
        assert sent[0]["result"] == {"decision": "decline"}
        await m.close()
    asyncio.run(run())


def test_default_never_does_not_display_or_grant():
    async def run():
        m, sent, events, _ = harness()
        m.enabled = False
        m.feed(request())
        await flush()
        assert sent[0]["result"] == {"decision": "decline"}
        assert not any(e["ev"] == "ask" for e in events)
        await m.close()
    asyncio.run(run())


def test_timeout_cancel_choice_and_no_duplicate_request():
    async def run():
        m, sent, events, _ = harness(timeout=0.001)
        event = request(availableDecisions=["accept", "cancel"])
        m.feed(event)
        m.feed(event)
        assert len(m.pending) == 1
        await asyncio.sleep(0.02)
        assert sent == [{"id": 10, "result": {"decision": "cancel"}}]
        assert events[-1]["reason"] == "timeout"
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("finish", ["close", "resolved", "completed"])
def test_provider_resolution_and_shutdown_revoke_card(finish):
    async def run():
        m, sent, events, _ = harness()
        m.feed(request())
        ask = events[0]["ask_id"]
        if finish == "close":
            await m.close()
        elif finish == "resolved":
            m.feed({"method": "serverRequest/resolved", "params": {"threadId": "thread", "requestId": 10}})
        else:
            m.feed({"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "item": {"id": "item"}}})
        assert not m.answer(answer(ask))
        await flush()
        assert not sent
        assert sum(e["ev"] == "ask_cancel" for e in events) == 1
        await m.close()
    asyncio.run(run())


def test_concurrent_cards_and_chat_isolation():
    async def run():
        m, sent, events, _ = harness()
        m.feed(request(10))
        m.feed(request(11, itemId="item2"))
        a, b = [e["ask_id"] for e in events if e["ev"] == "ask"]
        assert not m.answer(answer(a, chat_id="other-chat"))
        assert m.answer(answer(b))
        assert m.answer(answer(a, ca.DECLINE))
        await flush()
        assert {x["id"]: x["result"]["decision"] for x in sent} == {10: "decline", 11: "accept"}
        await m.close()
    asyncio.run(run())


def test_file_preview_requires_matching_item_and_keeps_full_diff():
    async def run():
        m, sent, events, _ = harness()
        ev = request()
        ev["method"] = "item/fileChange/requestApproval"
        m.feed({"method": "item/started", "params": {"threadId": "thread", "turnId": "turn", "item": {
            "id": "item", "type": "fileChange", "changes": [{"path": "/fixture/x", "kind": "update", "diff": "-old\n+new"}]}}})
        m.feed(ev)
        text = events[0]["questions"][0]["question"]
        assert "/fixture/x" in text and "-old\\n+new" in text
        m.answer(answer(events[0]["ask_id"]))
        await flush()
        assert sent[0]["result"]["decision"] == "accept"
        await m.close()
    asyncio.run(run())


def test_network_and_additional_permissions_are_visible():
    async def run():
        m, sent, events, _ = harness()
        m.feed(request(command=None, networkApprovalContext={"host": "example.test", "protocol": "https"},
                       additionalPermissions={"fileSystem": {"write": ["/fixture"]}}))
        text = events[0]["questions"][0]["question"]
        assert "访问网络" in text and "example.test" in text and '"write"' in text
        await m.close()
    asyncio.run(run())


def test_unknown_server_requests_are_answered_without_approval():
    async def run():
        m, sent, events, _ = harness()
        m.feed({"id": "server-1", "method": "unknown/request", "params": {}})
        await flush()
        assert sent[0]["error"]["code"] == -32601
        assert not events
        await m.close()
    asyncio.run(run())


def test_write_failure_has_no_retry_and_closes_card():
    async def run():
        m, sent, events, _ = harness()
        async def fail(frame):
            sent.append(frame)
            raise BrokenPipeError()
        m.send = fail
        m.feed(request())
        m.answer(answer(events[0]["ask_id"]))
        await flush()
        assert len(sent) == 1
        assert any(e["ev"] == "notice" for e in events)
        assert not m.pending
        await m.close()
    asyncio.run(run())


def test_policy_opt_in_and_invalid_config():
    assert ca.approval_policy("") == ca.approval_policy("never") == "never"
    assert ca.approval_policy("on-request") == "on-request"
    with pytest.raises(ValueError):
        ca.approval_policy("auto-accept")


def test_no_supported_refusal_does_not_offer_approval():
    async def run():
        m, sent, events, _ = harness()
        m.feed(request(availableDecisions=["accept"]))
        await flush()
        assert sent[0]["error"]["code"] == -32602
        assert not any(e["ev"] == "ask" for e in events)
        await m.close()
    asyncio.run(run())


def test_resolved_before_pending_answer_is_written_cannot_approve():
    async def run():
        m, sent, events, _ = harness()
        m.feed(request())
        m.answer(answer(events[0]["ask_id"]))
        m.feed({"method": "serverRequest/resolved", "params": {"threadId": "thread", "requestId": 10}})
        await flush()
        assert sent == []
        await m.close()
    asyncio.run(run())


def test_invisible_direction_override_is_not_an_approvable_preview():
    async def run():
        m, sent, events, _ = harness()
        m.feed(request(command="echo \u202eEXE"))
        await flush()
        assert sent[0]["result"]["decision"] == "decline"
        assert not any(e["ev"] == "ask" for e in events)
        await m.close()
    asyncio.run(run())


def bridge():
    argv = sys.argv
    sys.argv = [argv[0], tempfile.mkdtemp(prefix="bridge-approval-test-")]
    try:
        from feishu_bridge.bridge import fleet_bridge
        return fleet_bridge
    finally:
        sys.argv = argv


def test_server_request_id_cannot_resolve_client_steer():
    async def run():
        b = bridge()
        control = b._CodexActiveTurn(None, "thread", "turn")
        future = asyncio.get_running_loop().create_future()
        control._pending[100] = future
        assert not control.resolve(request(100))
        assert not future.done()
        assert control.resolve({"id": 100, "result": {}})
        assert future.result() == {"id": 100, "result": {}}
    asyncio.run(run())


def test_interrupt_revokes_card_before_terminating_process(monkeypatch):
    b = bridge()
    async def run():
        m, sent, events, _ = harness()
        m.feed(request())
        ask = events[0]["ask_id"]
        class Proc:
            returncode = None
            def terminate(self):
                assert m.closed and not m.pending
                assert not m.answer(answer(ask))
            async def wait(self): return 0
        monkeypatch.setattr(b, "CODEX_PROCS", {"work": Proc()})
        monkeypatch.setattr(b, "CODEX_APPROVALS", {"work": m})
        monkeypatch.setattr(b, "ACTIVE", {"work"})
        monkeypatch.setattr(b, "COALESCE", {})
        monkeypatch.setattr(b, "INTERRUPTED", set())
        assert await b._do_interrupt("work") == ("stopped", 0)
        assert not sent
    asyncio.run(run())


def test_stale_reserved_answer_never_becomes_chat(tmp_path):
    from feishu_bridge.bridge.app_frontend import AppFrontend
    async def run():
        f = AppFrontend(tmp_path, log=lambda _: None)
        def unwanted(*_):
            raise AssertionError("approval must not enter generic answer/chat handler")
        f._answer_ask = f._inject = unwanted
        await f._handle_frame(answer(ca.PREFIX + "stale"), None)
    asyncio.run(run())


@pytest.mark.parametrize("before_ack", [True, False])
@pytest.mark.parametrize("reviewer,resume", [("", False), ("user", False), ("auto_review", True)])
@pytest.mark.parametrize("kind", ["command", "permissions"])
def test_full_adapter_approval_roundtrip_does_not_block_stdout(monkeypatch, tmp_path, before_ack, reviewer, resume, kind):
    b = bridge()
    from tests.codex_storage_fixture import make_storage, header, original_protocol
    fixture=make_storage(monkeypatch,tmp_path)
    header(fixture,"thread")
    mapping=fixture.scope.home.parent/"sessions.json"
    mapping.write_text(json.dumps({"work-chat::codex":{"active":"thread","sessions":{"thread":{"fleet_codex_namespace":{"home":str(fixture.scope.home),"namespace":fixture.scope.namespace}}}}}) if resume else '{}')
    monkeypatch.setattr(b._fs,"SESSIONS_FILE",mapping)
    from feishu_bridge.bridge.app_frontend import AppFrontend, DISPLAY_CHAT
    async def run():
        reader = asyncio.StreamReader()
        emitted, written, replies = [], [], []
        def enqueue(frame):
            reader.feed_data((json.dumps(frame) + "\n").encode())
        approval = request(100)
        if kind == "permissions":
            approval["method"] = ca.PERMISSIONS_METHOD
            approval["params"].pop("command")
            approval["params"]["permissions"] = {"fileSystem": {"read": ["/fixture/read"]}}
            approval["params"]["startedAtMs"] = 1
        item = {"method": "item/started", "params": {"threadId": "thread", "turnId": "turn", "item": {
            "id": "item", "type": "commandExecution", "command": "echo fixture", "cwd": "/fixture"}}}
        enqueue({"id": 1, "result": {}})
        enqueue({"id": 21, "result": {"config": fixture.config}})
        enqueue({"id": 2, "result": {"thread": {"id": "thread"}, "approvalsReviewer": reviewer or "user"}})
        if before_ack:
            enqueue(item)
            enqueue(approval)
        enqueue({"id": 3, "result": {"turn": {"id": "turn"}}})
        if not before_ack:
            enqueue(item)
            enqueue(approval)
        # This frame must be read before a user answer is provided below.
        enqueue({"method": "item/agentMessage/delta", "params": {"delta": "waiting"}})
        class Writer:
            def write(self, raw):
                frame = json.loads(raw)
                written.append(frame)
                if "method" not in frame and frame.get("id") == 100:
                    enqueue({"method": "serverRequest/resolved", "params": {"threadId": "thread", "requestId": 100}})
                    enqueue({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}})
            async def drain(self): pass
            def close(self): pass
        class Proc:
            stdin, stdout, returncode = Writer(), reader, 0
            stderr = None
            async def wait(self): return 0
        async def spawn(*_, **__): return Proc()
        async def drop(*_): pass
        class Sender:
            feishu = None
            def begin_busy(self, *_): return None
            def send_reply(self, chat, text, *_, **__):
                replies.append(text)
                return "reply"
        f = AppFrontend(tmp_path, log=lambda _: None)
        f.chat_id = "work-chat"
        f.set_chat_validator(lambda chat: (chat == "group:fleet-product", ""))
        f._approval_answer = b._app_answer_codex_approval
        pending = []
        def emit(event):
            emitted.append(event)
            if event["ev"] == "ask": pending.append(event["ask_id"])
            if event["ev"] == "stream":
                assert pending, "approval precedes stream"
                asyncio.create_task(f._handle_frame(answer(pending[0], ca.GRANT if kind == "permissions" else ca.ACCEPT), None))
        f.broadcast = emit
        monkeypatch.setattr(b, "APP_FRONT", f)
        monkeypatch.setattr(b, "SENDER", Sender())
        monkeypatch.setattr(b, "CODEX_ENABLED", True)
        monkeypatch.setattr(b, "CODEX_APPROVAL_POLICY", "on-request")
        monkeypatch.setattr(b, "CODEX_APPROVALS_REVIEWER", reviewer)
        monkeypatch.setattr(b, "CODEX_PERMISSIONS_PROBE", "")
        monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-6.1-sol", "high", "codex"))
        monkeypatch.setattr(b, "_load_session", lambda _: "thread" if resume else None)
        monkeypatch.setattr(b, "_read_sessions", lambda: {})
        monkeypatch.setattr(b, "_drop_client", drop)
        monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
        for name in ("_record_session", "_write_codex_status", "_append_codex_ledger", "_remember", "_app_pin_last"):
            monkeypatch.setattr(b, name, lambda *_, **__: None)
        tok = DISPLAY_CHAT.set("group:fleet-product")
        try:
            await asyncio.wait_for(b._run_turn_codex("work-chat", "fixture"), 1)
        finally:
            DISPLAY_CHAT.reset(tok)
        expected = ({"permissions": {"fileSystem": {"read": ["/fixture/read"]}}, "scope": "turn"}
                    if kind == "permissions" else {"decision": "accept"})
        assert {"id": 100, "result": expected} in written
        assert any(e["ev"] == "ask_cancel" and e["chat_id"] == "group:fleet-product" for e in emitted)
        assert "work-chat" not in b.CODEX_APPROVALS
        for frame in original_protocol(written, fixture):
            if frame.get("method") in {"thread/start", "thread/resume", "turn/start"}:
                assert frame["params"]["approvalPolicy"] == "on-request"
                assert "config" not in frame["params"] and "ephemeral" not in frame["params"]
                if reviewer:
                    assert frame["params"]["approvalsReviewer"] == reviewer
                else:
                    assert "approvalsReviewer" not in frame["params"]
        assert not any(frame.get("method") == "experimentalFeature/list" for frame in written)
        assert replies and "waiting" in replies[0]
    asyncio.run(run())

"""Synthetic RPC processes and a private temporary registry; no real Codex or archives."""
import asyncio
import contextvars
import json
from types import SimpleNamespace

import pytest

from tests.codex.test_codex_adapter import b
from tests.codex_storage_fixture import storage_fixture, header


OLD, NEW, KEY = "old-thread", "fresh-thread", "chat::codex"
OBSERVED_SID = "11111111-2222-4333-8444-555555555555"
OBSERVED_ARCHIVED_ERROR = (
    "session 11111111-2222-4333-8444-555555555555 is archived. "
    "Run codex unarchive 11111111-2222-4333-8444-555555555555 to unarchive it first."
)


@pytest.fixture
def recovery(monkeypatch, tmp_path, storage_fixture):
    fs = b._fs
    header(storage_fixture, OLD)
    namespace = {"home": str(storage_fixture.scope.home), "namespace": storage_fixture.scope.namespace}
    monkeypatch.setattr(fs, "SESSIONS_FILE", tmp_path / "sessions.json")
    original = {KEY: {"active": OLD, "sessions": {
        OLD: {"name": "original name", "ts": "original time", "archive": {"keep": True},
              "fleet_codex_namespace": namespace},
        "historical": {"name": "another old session", "ts": "unchanged"}}, "custom": [1, 2]},
        "chat": {"active": "claude", "sessions": {"claude": {"name": "unchanged"}}}}
    fs.write_sessions(original)
    initial_bytes, initial_revision = fs.SESSIONS_FILE.read_bytes(), fs.sessions_revision()
    runtime = ["gpt-6.1-sol", "high", "codex"]
    display = ["group:fleet-product"]
    state = SimpleNamespace(error={"code": -32600, "message": f"thread {OLD} not found"},
        error_id=2, start_error=None, start_thread=NEW, hook=lambda stage: None,
        timeline=[], events=[], notices=[], writes=[], pending_takes=[], original=original,
        initial_bytes=initial_bytes, initial_revision=initial_revision, runtime=runtime,
        display=display, fs=fs)

    class Proc:
        returncode = None
        def __init__(self):
            self.stdin = self.stdout = self
            self.stderr = SimpleNamespace(read=self.read)
            self.queue = []
        async def read(self):
            return b""
        def enqueue(self, frame, stage):
            self.queue.append((frame, stage))
        def write(self, raw):
            frame = json.loads(raw)
            state.writes.append(frame)
            method = frame.get("method")
            state.timeline.append(method)
            if method == "initialize":
                self.enqueue({"id": frame["id"], "result": {}}, "initialize")
            elif method == "config/read":
                self.enqueue({"id": frame["id"], "result": {"config": storage_fixture.config}}, "config")
            elif method == "thread/resume":
                response = ({"error": state.error} if state.error is not None else
                            {"result": {"thread": {"id": OLD}}})
                self.enqueue({"id": state.error_id, **response}, "resume")
            elif method == "thread/start":
                response = ({"error": state.start_error} if state.start_error else
                            {"result": {"thread": {"id": state.start_thread}}})
                self.enqueue({"id": frame["id"], **response}, "start_ack")
            elif method == "turn/start":
                self.enqueue({"id": frame["id"], "result": {"turn": {"id": "turn"}}}, "turn_ack")
                self.enqueue({"method": "item/agentMessage/delta", "params": {"delta": "response"}}, "text")
                self.enqueue({"method": "turn/completed", "params": {
                    "threadId": frame["params"]["threadId"],
                    "turn": {"id": "turn", "status": "completed"}}}, "turn_end")
        async def drain(self):
            await asyncio.sleep(0)
        async def readline(self):
            if not self.queue:
                return b""
            frame, stage = self.queue.pop(0)
            state.hook(stage)
            return (json.dumps(frame) + "\n").encode()
        def __aiter__(self):
            return self
        async def __anext__(self):
            raw = await self.readline()
            if not raw:
                raise StopAsyncIteration
            return raw
        def close(self):
            pass
        async def wait(self):
            self.returncode = 0
            return 0
        def terminate(self):
            raise AssertionError("No real child or signal needed")

    proc = Proc()
    state.proc = proc
    async def launch(*args, **kwargs):
        return proc
    async def drop(*args):
        pass
    def broadcast(event):
        state.events.append(event)
        state.timeline.append(event["ev"])
    def send(chat, text, **kwargs):
        state.notices.append(text)
        state.timeline.append("notice")
        state.hook("notice")
    sender = SimpleNamespace(begin_busy=lambda *args: None, end_busy=lambda *args: None,
        send=send, send_reply=lambda *args, **kwargs: "reply", feishu=SimpleNamespace(send=lambda *args: None))
    front = SimpleNamespace(chat_id="chat", broadcast=broadcast,
        resolve_work_display_ex=lambda requested: ("chat", display[0], ""))
    monkeypatch.setattr(b, "APP_FRONT", front)
    monkeypatch.setattr(b, "SENDER", sender)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b, "CODEX_PROCS", {})
    monkeypatch.setattr(b, "CODEX_TURNS", {})
    monkeypatch.setattr(b, "CODEX_APPROVALS", {})
    monkeypatch.setattr(b, "COALESCE", {})
    monkeypatch.setattr(b, "INTERRUPTED", set())
    monkeypatch.setattr(b, "IDLE_ACTIVE", set())
    monkeypatch.setattr(b, "ROLLOVER_HOLD", set())
    monkeypatch.setattr(b, "_QUESTION_CONTINUATION", contextvars.ContextVar("resume-test", default=None))
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: tuple(runtime))
    monkeypatch.setattr(b, "_read_runtime", lambda: {"model": runtime[0], "effort": runtime[1]})
    monkeypatch.setattr(b, "_display_chat_for", lambda chat: display[0])
    monkeypatch.setattr(b, "_load_session", fs.load_session)
    monkeypatch.setattr(b, "_read_sessions", fs.read_sessions)
    monkeypatch.setattr(b, "_record_session", fs.record_session)
    monkeypatch.setattr(b, "_drop_client", drop)
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(b.codex_questions, "selected_protocol", lambda binary: None)
    monkeypatch.setattr(b, "_write_codex_status", lambda *args: None)
    monkeypatch.setattr(b, "_append_codex_ledger", lambda *args: None)
    monkeypatch.setattr(b, "_remember", lambda *args: None)
    monkeypatch.setattr(b, "_app_pin_last", lambda *args: None)
    monkeypatch.setattr(b._ro, "peek_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(b._ro, "take_pending", lambda *args, **kwargs: state.pending_takes.append(args))
    state.run = lambda: asyncio.run(b._run_turn_codex("chat", "question"))
    state.methods = lambda: [frame.get("method") for frame in state.writes]
    return state


@pytest.mark.parametrize("message", [f"thread {OLD} not found", f"session {OLD} does not exist",
    f"session {OLD} is archived. Run codex unarchive {OLD} first", f"no rollout found for thread id {OLD}"])
def test_exact_resume_failure_preserves_history_and_announces_boundary(recovery, message):
    s = recovery
    s.error["message"] = message
    s.run()
    data = s.fs.read_sessions()
    assert data[KEY]["active"] == NEW
    assert data[KEY]["sessions"][NEW]["fleet_codex_namespace"] == s.original[KEY]["sessions"][OLD]["fleet_codex_namespace"]
    for sid, meta in s.original[KEY]["sessions"].items():
        assert data[KEY]["sessions"][sid] == meta
    assert data[KEY]["custom"] == [1, 2] and data["chat"] == s.original["chat"]
    assert s.fs.resolve_session(KEY, OLD) == [OLD]
    assert s.methods().count("thread/start") == s.methods().count("turn/start") == 1
    start = next(f for f in s.writes if f.get("method") == "thread/start")
    assert start["id"] == 4
    assert s.timeline.index("session_reset") < s.timeline.index("turn/start")
    assert s.timeline.index("notice") < s.timeline.index("turn/start")
    assert any(OLD in text and NEW in text and "不包含旧上下文" in text for text in s.notices)


@pytest.mark.parametrize("error", [
    {"code": -32600, "message": "permission denied while reading missing session metadata"},
    {"code": -32600, "message": "permission denied"},
    {"code": -32600, "message": "connection timed out"},
    {"code": -32600, "message": "thread another-thread not found"},
    {"code": -32600, "message": f"thread {OLD}-suffix not found"},
    {"code": -32600, "message": f"thread {OLD} not found; permission denied"},
    {"code": -32600, "message": f"thread {OLD} not found", "data": {"threadId": "another"}},
    {"code": -32600, "message": f"thread {OLD} not found", "data": "unknown"},
    {"message": f"thread {OLD} not found"},
    {"code": 403, "message": f"thread {OLD} not found"},
])
def test_unrelated_or_unqualified_errors_never_mutate_registry(recovery, error):
    s = recovery
    s.error = error
    s.run()
    assert "thread/start" not in s.methods() and "turn/start" not in s.methods()
    assert s.fs.SESSIONS_FILE.read_bytes() == s.initial_bytes
    assert s.fs.sessions_revision() == s.initial_revision


@pytest.mark.parametrize("response_id", [88, "2", 2.0])
def test_unrelated_response_id_is_not_our_resume_error(recovery, response_id):
    s = recovery
    s.error_id = response_id
    s.run()
    assert "thread/start" not in s.methods()
    assert s.fs.SESSIONS_FILE.read_bytes() == s.initial_bytes


@pytest.mark.parametrize("stage", ["resume", "start_ack", "notice", "turn_ack", "turn_end"])
@pytest.mark.parametrize("change", ["active", "aba", "metadata", "runtime", "display", "process"])
def test_binding_drift_at_each_await_or_notice_refuses_further_replacement(recovery, stage, change):
    s = recovery
    other_proc = object()
    changed_bytes = []
    def mutate(at):
        if at != stage:
            return
        if change in {"active", "aba"}:
            old = s.fs.load_session(KEY)
            s.fs.set_active(KEY, "concurrent")
            if change == "aba":
                s.fs.set_active(KEY, old)
        elif change == "metadata":
            s.fs.name_active(KEY, "concurrent metadata")
        elif change == "runtime":
            s.runtime[1] = "medium"
        elif change == "display":
            s.display[0] = "group:other"
        else:
            b.CODEX_PROCS["chat"] = other_proc
        changed_bytes.append(s.fs.SESSIONS_FILE.read_bytes())
    s.hook = mutate
    s.run()
    assert changed_bytes and s.fs.SESSIONS_FILE.read_bytes() == changed_bytes[-1]
    if stage == "resume":
        assert "thread/start" not in s.methods()
    if stage in {"resume", "start_ack", "notice"}:
        assert "turn/start" not in s.methods()
    if change == "process":
        assert b.CODEX_PROCS["chat"] is other_proc
    expected_old = dict(s.original[KEY]["sessions"][OLD])
    if change == "metadata" and stage in {"resume", "start_ack"}:
        expected_old["name"] = "concurrent metadata"
    assert s.fs.read_sessions()[KEY]["sessions"][OLD] == expected_old


@pytest.mark.parametrize("failure", ["start", "write", "old-id", "historical-id", "unknown-revision"])
def test_replacement_failure_never_clears_old_binding(recovery, monkeypatch, failure):
    s = recovery
    if failure == "start":
        s.start_error = {"code": -32000, "message": "service unavailable"}
    elif failure == "write":
        monkeypatch.setattr(s.fs, "write_sessions", lambda data: None)
    elif failure == "old-id":
        s.start_thread = OLD
    elif failure == "historical-id":
        s.start_thread = "historical"
    else:
        monkeypatch.setattr(s.fs, "sessions_revision", lambda: None)
    s.run()
    assert "turn/start" not in s.methods()
    assert s.fs.SESSIONS_FILE.read_bytes() == s.initial_bytes
    assert not s.notices and not any(e["ev"] == "session_reset" for e in s.events)


def test_continuation_resume_failure_has_zero_registry_or_replacement_effects(recovery, monkeypatch):
    from tests.codex.test_codex_questions import make_card
    s = recovery
    reg, origin, _, _, _, _, card = make_card()
    origin.normal = True
    origin.binding = b.codex_questions.Binding("chat", s.display[0], OLD, "previous-turn",
        b._question_runtime_binding(), b.codex_questions.PROTOCOL, "connection")
    card.state, card.deadline = "dispatching", b.time.monotonic() + 600
    monkeypatch.setattr(b, "CODEX_QUESTIONS", reg)
    monkeypatch.setattr(b.codex_questions, "selected_protocol", lambda binary: b.codex_questions.PROTOCOL)
    b._QUESTION_CONTINUATION.set(card)
    s.run()
    assert "thread/resume" in s.methods()
    assert "thread/start" not in s.methods() and "turn/start" not in s.methods()
    assert s.fs.SESSIONS_FILE.read_bytes() == s.initial_bytes
    assert s.fs.sessions_revision() == s.initial_revision
    assert s.pending_takes == []


def test_only_the_correlated_rpc_exception_can_authorize_recovery():
    proc = object()
    raw = {"code": -32600, "message": f"thread {OLD} not found"}
    assert b._codex_unusable_session(b._CodexRpcError(proc, 2, raw, "thread/resume", OLD), proc, OLD)
    for error in (RuntimeError(raw["message"]),
        b._CodexRpcError(object(), 2, raw, "thread/resume", OLD),
        b._CodexRpcError(proc, 3, raw, "thread/resume", OLD),
        b._CodexRpcError(proc, 2, raw, "thread/start", OLD),
        b._CodexRpcError(proc, 2, raw, "thread/resume", "other")):
        assert not b._codex_unusable_session(error, proc, OLD)


@pytest.mark.parametrize("change", [None, "first_sid", "second_sid", "both_sids",
    "prefix", "suffix", "embedded", "missing_period", "unknown_wording"])
def test_observed_archived_error_accepts_only_the_exact_bound_form(change):
    message = OBSERVED_ARCHIVED_ERROR
    if change == "first_sid":
        message = message.replace(OBSERVED_SID, "other-thread", 1)
    elif change == "second_sid":
        first, second = message.split("Run codex unarchive ")
        message = first + "Run codex unarchive " + second.replace(OBSERVED_SID, "other-thread", 1)
    elif change == "both_sids":
        message = message.replace(OBSERVED_SID, "other-thread")
    elif change == "prefix":
        message = "permission denied: " + message
    elif change == "suffix":
        message += " permission denied"
    elif change == "embedded":
        message = "unrelated error\n" + message + "\nmore details"
    elif change == "missing_period":
        message = message[:-1]
    elif change == "unknown_wording":
        message = message.replace("to unarchive it first", "to recover it first")
    proc = object()
    error = b._CodexRpcError(proc, 2, {"code": -32603, "message": message},
                             "thread/resume", OBSERVED_SID)
    assert b._codex_unusable_session(error, proc, OBSERVED_SID) is (change is None)


def test_observed_archived_error_shape_preserves_history_before_new_turn(recovery):
    s = recovery
    s.error = {"code": -32603,
               "message": OBSERVED_ARCHIVED_ERROR.replace(OBSERVED_SID, OLD)}
    s.run()
    data = s.fs.read_sessions()
    assert data[KEY]["active"] == NEW
    for sid, meta in s.original[KEY]["sessions"].items():
        assert data[KEY]["sessions"][sid] == meta
    assert s.timeline.index("notice") < s.timeline.index("turn/start")
    assert s.methods().count("thread/start") == s.methods().count("turn/start") == 1
    assert any(OLD in text and NEW in text and "不包含旧上下文" in text for text in s.notices)

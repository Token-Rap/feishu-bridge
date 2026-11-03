from __future__ import annotations

import asyncio
import json
import sys
import tempfile

import pytest
from codex_storage_fixture import storage_fixture, header, original_protocol

@pytest.fixture(autouse=True)
def storage_admission(storage_fixture, monkeypatch):
    header(storage_fixture, "thread-1")
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", storage_fixture.scope.home.parent/"sessions.json")
    return storage_fixture
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-codex-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def test_codex_session_key_is_engine_scoped():
    assert b._session_chat_key("chat-1", "claude") == "chat-1"
    assert b._session_chat_key("chat-1", "codex") == "chat-1::codex"


def test_codex_usage_total_does_not_double_count_cached_input():
    usage = {"input_tokens": 100, "cached_input_tokens": 80,
             "output_tokens": 20, "reasoning_output_tokens": 7}
    assert b._codex_usage_total(usage) == 120


def test_codex_thread_id_accepts_nested_shape():
    assert b._codex_thread_id({"thread": {"id": "t-nested"}}, "") == "t-nested"
    assert b._codex_thread_id({}, "old") == "old"


def test_codex_stream_limit_handles_large_jsonl_events():
    assert b.CODEX_STREAM_LIMIT >= 1024 * 1024

    src = Path(b.__file__).read_text(encoding="utf-8")
    start = src.index("proc = await asyncio.create_subprocess_exec(")
    segment = src[start:start + 400]
    assert "limit=CODEX_STREAM_LIMIT" in segment


def test_codex_cmd_uses_resume_when_thread_exists():
    old_bin = b.CODEX_BIN
    b.CODEX_BIN = "codex"
    try:
        launch, _out = b._codex_cmd("hi", "gpt-5.6-sol", "xhigh", "thread-1")
    finally:
        b.CODEX_BIN = old_bin
    cmd = launch.argv
    assert launch.env["CODEX_HOME"] == str(launch.scope.home)
    assert cmd[:3] == ["codex", "exec", "resume"]
    assert "thread-1" in cmd
    assert ["-m", "gpt-5.6-sol"] == cmd[cmd.index("-m"):cmd.index("-m") + 2]
    assert f"model_reasoning_effort=xhigh" in cmd
    assert f"model_reasoning_summary={b.CODEX_REASONING_SUMMARY}" in cmd


def test_codex_cmd_new_thread_uses_configured_sandbox():
    old_bin = b.CODEX_BIN
    b.CODEX_BIN = "codex"
    try:
        launch, _out = b._codex_cmd("hi", "gpt-5.6-sol", "high", None)
    finally:
        b.CODEX_BIN = old_bin
    cmd = launch.argv
    assert launch.env["CODEX_HOME"] == str(launch.scope.home)
    assert cmd[:3] == ["codex", "exec", "-s"]
    assert cmd[3] == b.CODEX_SANDBOX
    assert "resume" not in cmd
    assert f"model_reasoning_summary={b.CODEX_REASONING_SUMMARY}" in cmd


def test_codex_default_uses_workspace_sandbox():
    assert b.CODEX_SANDBOX == "workspace-write"


def test_codex_reasoning_text_accepts_exec_completed_item():
    text, is_delta = b._codex_reasoning_text({
        "type": "item.completed",
        "item": {"type": "reasoning", "text": "先核对协议，再改解析器"},
    })
    assert text == "先核对协议，再改解析器"
    assert is_delta is False


def test_codex_reasoning_text_accepts_app_server_delta():
    text, is_delta = b._codex_reasoning_text({
        "method": "item/reasoning/summaryTextDelta",
        "params": {"delta": "正在检查事件流"},
    })
    assert text == "正在检查事件流"
    assert is_delta is True


def test_codex_reasoning_text_ignores_encrypted_reasoning():
    assert b._codex_reasoning_text({
        "type": "item.completed",
        "item": {"type": "reasoning", "encrypted_content": "secret"},
    }) == ("", False)


def test_codex_agent_text_accepts_completed_message_only():
    assert b._codex_agent_text({
        "type": "item.completed",
        "item": {"type": "agent_message", "content": [
            {"type": "output_text", "text": "第一段"},
        ]},
    }) == "第一段"
    assert b._codex_agent_text({
        "type": "item.started",
        "item": {"type": "agent_message", "text": "未完成"},
    }) == ""


def test_codex_error_event_accepts_current_and_legacy_shapes():
    assert b._codex_error_event({
        "method": "error",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "error": {"message": "runner pipe rejected"},
            "willRetry": False,
        },
    }, "thread-1", "turn-1") == ("runner pipe rejected", False)
    assert b._codex_error_event({
        "type": "item.completed",
        "item": {"type": "error", "message": "tool failed"},
    }) == ("tool failed", False)


def test_codex_error_event_ignores_another_active_turn():
    assert b._codex_error_event({
        "method": "error",
        "params": {
            "threadId": "thread-1",
            "turnId": "other-turn",
            "error": {"message": "unrelated"},
            "willRetry": True,
        },
    }, "thread-1", "turn-1") is None


def test_codex_turn_refuses_when_engine_is_disabled(monkeypatch):
    replies = []

    class _Sender:
        def send_reply(self, _chat, text, _thinking, **kwargs):
            replies.append((text, kwargs.get("extra")))
            return "mid-disabled"

    monkeypatch.setattr(b, "CODEX_ENABLED", False)
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "_remember", lambda *_args: None)

    asyncio.run(b._run_turn_codex("chat", "问题"))

    assert replies == [("↩︎ 答:「问题」\n\n⚠ 这台节点暂不开放 Codex 引擎。",
                        {"failed": True, "retryable": False})]


def test_codex_turn_broadcasts_reasoning_separately(monkeypatch, tmp_path, storage_fixture):
    events = []
    replies = []
    ledger = []

    class _Writer:
        def __init__(self):
            self.lines = []

        def write(self, raw):
            self.lines.append(json.loads(raw))

        async def drain(self):
            pass

        def close(self):
            pass

    class _Reader:
        def __init__(self, rows):
            self.rows = [(json.dumps(row, ensure_ascii=False) + "\n").encode()
                         for row in rows]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.rows:
                raise StopAsyncIteration
            return self.rows.pop(0)

        async def read(self):
            return b""

        async def readline(self):
            return self.rows.pop(0) if self.rows else b""

    class _Proc:
        def __init__(self):
            self.stdin = _Writer()
            self.stdout = _Reader([
                {"id": 1, "result": {}},
                {"id": 21, "result": {"config": storage_fixture.config}},
                {"id": 2, "result": {"thread": {"id": "thread-1"}}},
                {"id": 3, "result": {"turn": {"id": "turn-1"}}},
                {"method": "item/reasoning/summaryTextDelta",
                 "params": {"delta": "先检查事件协议"}},
                {"method": "item/agentMessage/delta", "params": {"delta": "第一段"}},
                {"method": "item/agentMessage/delta", "params": {"delta": "第二段"}},
                {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1",
                    "turnId": "turn-1", "tokenUsage": {
                    "total": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
                    "last": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}}}},
                {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1",
                    "turnId": "turn-1", "tokenUsage": {
                    "total": {"inputTokens": 300, "outputTokens": 30, "totalTokens": 330},
                    "last": {"inputTokens": 200, "outputTokens": 20, "totalTokens": 220}}}},
                {"method": "turn/completed", "params": {
                    "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}},
            ])
            self.stderr = _Reader([])
            self.returncode = 0

        async def wait(self):
            return 0

    class _Sender:
        feishu = None

        def __init__(self):
            self.feishu = self

        def begin_busy(self, _message_id):
            return None

        def end_busy(self, _message_id, _reaction_id):
            pass

        def send_reply(self, _chat, text, _thinking, **_kwargs):
            replies.append(text)
            return "mid-1"

        def send(self, _chat, _text):
            pass

    class _Front:
        def broadcast(self, event):
            events.append(event)

        def broadcast_runtime(self, _model=""):
            pass

        def resolve_work_display_ex(self, requested):
            return requested, requested, ""

    fake_proc = _Proc()

    async def fake_exec(*_cmd, **_kwargs):
        return fake_proc

    async def fake_drop(_chat):
        pass

    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "_runtime_snapshot",
                        lambda: ("gpt-5.6-luna", "high", "codex"))
    monkeypatch.setattr(b, "_load_session", lambda _key: None)
    monkeypatch.setattr(b, "_drop_client", fake_drop)
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(b, "_record_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(b, "_write_codex_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(b, "_append_codex_ledger", lambda *args, **kw: ledger.append(args[4]))
    monkeypatch.setattr(b, "_remember", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(b, "_app_pin_last", lambda *_args, **_kwargs: None)

    asyncio.run(b._run_turn_codex("chat", "问题"))

    assert {"ev": "reasoning", "text": "先检查事件协议"} in events
    assert [event["text"] for event in events if event.get("ev") == "stream"] == [
        "↩︎ 答:「问题」\n\n第一段",
        "↩︎ 答:「问题」\n\n第一段第二段",
    ]
    assert replies == ["↩︎ 答:「问题」\n\n第一段第二段"]
    assert "先检查事件协议" not in replies[0]
    start = next(row for row in fake_proc.stdin.lines if row.get("method") == "thread/start")
    assert start["params"]["sandbox"] == b.CODEX_SANDBOX
    assert start["params"]["approvalPolicy"] == "never"
    turn = next(row for row in fake_proc.stdin.lines if row.get("method") == "turn/start")
    supplied = turn["params"]["input"][0]["text"]
    assert "关键结论、当前状态、阻塞" in supplied
    assert "不绕过隐藏目录来源限制" in supplied
    assert supplied.endswith("\n\n问题")
    assert turn["params"]["model"] == "gpt-5.6-luna"
    assert turn["params"]["effort"] == "high"
    assert turn["params"]["approvalPolicy"] == "never"

    assert ledger[0]["totalTokens"] == 330
    assert [e["used"] for e in events if e.get("ev") == "ctx"] == [220]


def test_codex_compact_uses_native_app_server_protocol(monkeypatch, storage_fixture):
    class _Writer:
        def __init__(self):
            self.lines = []
            self.closed = False

        def write(self, raw):
            self.lines.append(json.loads(raw))

        async def drain(self):
            pass

        def close(self):
            self.closed = True

    class _Reader:
        def __init__(self, rows):
            self.rows = [(json.dumps(row) + "\n").encode() for row in rows]

        async def readline(self):
            return self.rows.pop(0) if self.rows else b""

        async def read(self):
            return b""

    class _Proc:
        def __init__(self):
            self.stdin = _Writer()
            self.stdout = _Reader([
                {"id": 1, "result": {}},
                {"id": 21, "result": {"config": storage_fixture.config}},
                {"id": 2, "result": {}},
                {"id": 3, "result": {}},
                {"method": "turn/completed", "params": {
                    "threadId": "thread-1", "turn": {"status": "completed"}}},
            ])
            self.stderr = _Reader([])

        async def wait(self):
            return 0

        def terminate(self):
            raise AssertionError("successful app-server should exit after stdin closes")

    proc = _Proc()
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(b._codex_compact_thread("thread-1"))

    assert seen["cmd"][:3] == (b.CODEX_BIN, "app-server", "--listen")
    proc.stdin.lines = original_protocol(proc.stdin.lines, storage_fixture)
    assert proc.stdin.lines == [
        {"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "fleet-bridge", "version": "1.0"},
            "capabilities": {"experimentalApi": True}}},
        {"method": "notifications/initialized"},
        {"method": "thread/resume", "id": 2,
         "params": {"threadId": "thread-1", "excludeTurns": True}},
        {"method": "thread/compact/start", "id": 3,
         "params": {"threadId": "thread-1"}},
    ]
    assert proc.stdin.closed is True


def test_slash_compact_routes_codex_thread_to_native_compaction(monkeypatch):
    sent = []
    events = []
    compacted = []

    class _Sender:
        def send(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    class _Front:
        def broadcast(self, event):
            events.append(event)

    async def fake_compact(thread):
        compacted.append(thread)

    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "APP_FRONT", _Front())
    monkeypatch.setattr(b, "_runtime_snapshot",
                        lambda: ("gpt-5.6-sol", "high", "codex"))
    monkeypatch.setattr(b, "_load_session",
                        lambda key: "thread-1" if key == "chat::codex" else None)
    monkeypatch.setattr(b, "_read_status_ctx", lambda _chat: {"used": 120, "max": 200})
    monkeypatch.setattr(b, "_codex_compact_thread", fake_compact)
    b.ACTIVE.discard("chat")

    assert asyncio.run(b.handle_command("chat", "/compact")) is True
    assert compacted == ["thread-1"]
    assert [e["phase"] for e in events] == ["start", "done"]
    assert sent[0][2]["kind"] == "compact_start"
    assert sent[-1][2]["kind"] == "compact_done"
    assert "chat" not in b.ACTIVE


def test_codex_ledger_and_status_are_written(tmp_path):
    old_ledger, old_status, old_front = b.CODEX_LEDGER_FILE, b._STATUS_FILE, b.APP_FRONT

    class _Front:
        seen = []

        def broadcast_runtime(self, observed=""):
            self.seen.append(observed)

    front = _Front()
    b.CODEX_LEDGER_FILE = tmp_path / "codex_ledger.jsonl"
    b._STATUS_FILE = tmp_path / "status.json"
    b.APP_FRONT = front
    usage = {"input_tokens": 10, "output_tokens": 3, "reasoning_output_tokens": 2}
    try:
        b._append_codex_ledger("chat", "gpt-5.6-sol", "xhigh", "thread",
                               usage, 0, 1.25)
        rec = json.loads(b.CODEX_LEDGER_FILE.read_text(encoding="utf-8"))
        assert rec["engine"] == "codex"
        assert rec["usage"] == usage

        b._write_codex_status("chat", "gpt-5.6-sol", "xhigh", "thread", usage, 0)
        status = json.loads(b._STATUS_FILE.read_text(encoding="utf-8"))
        assert status["engine"] == "codex"
        assert status["sessions"]["chat"]["context_tokens"] == 13
        assert status["sessions"]["chat"]["thread"] == "thread"
        assert front.seen == ["gpt-5.6-sol"]
    finally:
        b.CODEX_LEDGER_FILE, b._STATUS_FILE, b.APP_FRONT = old_ledger, old_status, old_front

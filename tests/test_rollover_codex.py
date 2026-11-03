"""Codex rollover safety: native markers, independent fork, durable handoff, scoped holds."""
import asyncio
import json
import os
import time

import pytest
from codex_storage_fixture import storage_fixture, header, original_protocol
from feishu_bridge.codex import fleet_codex_storage as storage

@pytest.fixture(autouse=True)
def storage_admission(storage_fixture):
    header(storage_fixture,SID)
    return storage_fixture

from test_rollover_bridge import b, env, SID, _night, _tick, _write_note  # noqa: F401


@pytest.fixture
def codex_env(env, tmp_path, monkeypatch, storage_fixture):
    root = storage_fixture.scope.home
    header(storage_fixture,SID).unlink()
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-other", "low", "codex"))
    t = root / "sessions" / "2026" / "09" / "30" / f"rollout-date-{SID}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text(json.dumps({'type':'session_meta','payload':{'id':SID}})+'\n'+''.join(json.dumps({"type": "compacted", "payload": {}}) + '\n'
                         for _ in range(3)))
    os.utime(t, (_night() - 7200, _night() - 7200))
    b._record_session("group:ops::codex", SID)
    env["transcript"] = t
    return env


def test_codex_count_only_native_markers(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text('\n'.join([
        json.dumps({"type": "compacted"}),
        json.dumps({"type": "compacted", "payload": {"trigger": "manual"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "context_compacted"}}),
        json.dumps({"type": "message", "text": '"compacted"'}),
        json.dumps({"subtype": "compact_boundary"}),
        'broken "compacted"', '[]']))
    assert b._ro.count_compacts(t, "codex") == 2


def test_codex_reset_scoped_and_first_turn_ingestion(codex_env):
    before = b._ro.fingerprint(codex_env["transcript"])
    b._record_session("group:ops", "claude-source")
    _tick()
    assert b._load_session("group:ops::codex") is None
    assert b._load_session("group:ops") == "claude-source"
    assert b._ro.fingerprint(codex_env["transcript"]) == before
    ev = b.APP_FRONT.events[-1]
    assert ev["engine"] == "codex" and ev["chat_id"] == "group:ops"
    prompt = b._rollover_first_turn("group:ops", "早", "codex", consume=False)
    assert "交接笔记正文" in prompt and prompt.endswith("早")
    assert b._ro.peek_pending(codex_env["home"], "group:ops::codex")
    assert b._rollover_first_turn("group:ops", "早") == "早"


@pytest.mark.parametrize("busy", ["ACTIVE", "IDLE_ACTIVE", "ROLLOVER_HOLD",
                                  "ASK_PENDING", "PENDING", "CODEX_PROCS", "CODEX_TURNS",
                                  "FORK_PENDING", "COALESCE"])
def test_codex_guard_uses_unscoped_chat(codex_env, monkeypatch, busy):
    container = getattr(b, busy)
    monkeypatch.setattr(b, busy, {"group:ops"} if isinstance(container, set)
                        else {"group:ops": object()})
    _tick()
    assert not codex_env["calls"]
    assert b._load_session("group:ops::codex") == SID


def test_codex_hold_and_queue_drain(codex_env, monkeypatch):
    async def note(key, sid, path, compacts):
        assert "group:ops" in b.ROLLOVER_HOLD
        assert key == "group:ops::codex"
        b.COALESCE["group:ops"] = [("hello", b.time.time())]
        _write_note(path, "交接" * 100)
        return ""
    monkeypatch.setattr(b, "_rollover_write_note", note)
    _tick()
    assert codex_env["drained"] == ["group:ops"]
    assert "group:ops" not in b.ROLLOVER_HOLD


@pytest.mark.parametrize("failure", ["pending", "save", "short", "stale", "content", "exception", "engine"])
def test_codex_failure_keeps_original(codex_env, monkeypatch, failure):
    original = codex_env["transcript"].read_bytes()
    async def note(key, sid, path, compacts):
        if failure == "exception":
            raise RuntimeError("fork unavailable")
        _write_note(path, "x" if failure == "short" else "交接" * 100)
        if failure == "stale":
            os.utime(path, (1, 1))
        if failure == "content":
            t = codex_env["transcript"]
            st = t.stat()
            t.write_bytes(original.replace(b'compacted', b'xxxxxxxxx'))
            os.utime(t, ns=(st.st_atime_ns, st.st_mtime_ns))
        if failure == "engine":
            monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("m", "high", "claude"))
        return ""
    monkeypatch.setattr(b, "_rollover_write_note", note)
    if failure == "pending":
        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(b._ro, "mark_pending", fail)
    if failure == "save":
        from pathlib import Path
        replace = Path.replace
        def fail_replace(path, target):
            if target == b._fs.SESSIONS_FILE:
                raise OSError("session save failed")
            return replace(path, target)
        monkeypatch.setattr(Path, "replace", fail_replace)
    _tick()
    assert b._load_session("group:ops::codex") == SID
    assert not [ev for ev in b.APP_FRONT.events if ev["ev"] == "session_reset"]
    assert "group:ops" not in b.ROLLOVER_HOLD


class Writer:
    def __init__(self):
        self.messages = []

    def write(self, raw):
        self.messages.append(json.loads(raw))

    async def drain(self):
        pass

    def close(self):
        pass


class Reader:
    def __init__(self, rows):
        self.rows = [(json.dumps(row) + '\n').encode() for row in rows]

    async def readline(self):
        return self.rows.pop(0) if self.rows else b""

    async def read(self):
        return b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        row = await self.readline()
        if not row:
            raise StopAsyncIteration
        return row


class Proc:
    def __init__(self, rows):
        self.stdin = Writer()
        from copy import deepcopy
        rows=deepcopy(rows)
        scope=storage.select()
        self.fixture=type("Fixture",(),{"scope":scope})()
        cfg=dict(sqlite_home=str(scope.home),forced_login_method="chatgpt",cli_auth_credentials_store="file",model_provider="openai",shell_environment_policy={"set":{**{k:str(scope.home) for k in storage._RESERVED},storage.DESKTOP_KEY:str(scope.desktop_home)}})
        for row in rows:
            if row.get("id")==4 and "config" in row.get("result",{}):
                row["result"]["config"].update(cfg)
        if rows:
            rows.insert(1,{"id":21,"result":{"config":cfg}})
        self.stdout = Reader(rows)
        self.stderr = Reader([])
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _rows(fork="fork", status="completed", note="中文交接内容" * 60):
    return [
        {"id": 1, "result": {}},
        {"id": 4, "result": {"config": {"features": {name: False for name in (
             "apps", "plugins", "hooks", "goals", "shell_tool", "unified_exec")},
                                          "mcp_servers": {"test.mcp": {"command": "test",
                                          "tool_timeout_sec": None, "enabled": False}},
                                          "plugins": {"test@plugin": {"enabled": False}}}}},
        {"id": 2, "result": {"thread": {"id": fork}}},
        {"id": 3, "result": {"turn": {"id": "turn"}}},
        {"method": "item/completed", "params": {"threadId": fork, "turnId": "turn",
         "item": {"type": "agentMessage", "text": note}}},
        {"method": "turn/completed", "params": {"threadId": fork,
         "turn": {"id": "turn", "status": status}}}]


def _mock_config_discovery(monkeypatch):
    async def config(on_event):
        return _rows()[1]["result"]["config"]
    monkeypatch.setattr(b, "_rollover_codex_tool_config", config)


@pytest.mark.parametrize("failure", [None, "source", "failed", "short", "eof", "error", "tool"])
def test_private_codex_writer_protocol(tmp_path, monkeypatch, failure):
    _mock_config_discovery(monkeypatch)
    rows = _rows(fork=SID if failure == "source" else "fork",
                 status="failed" if failure == "failed" else "completed",
                 note="x" if failure == "short" else "中文交接内容" * 60)
    if failure == "eof":
        rows.pop()
    if failure == "error":
        rows.insert(4, {"method": "error", "params": {"error": {"message": "denied"}}})
    if failure == "tool":
        rows.insert(4, {"method": "item/started", "params": {"item": {"type": "commandExecution"}}})
    proc = Proc(rows)
    commands = []
    async def spawn(*args, **kwargs):
        commands.append(args)
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note.md"
    why = asyncio.run(b._rollover_write_note_codex(SID, note, 3))
    assert bool(why) == bool(failure)
    assert note.exists() == (failure is None)
    assert proc.killed
    fork = next(m for m in original_protocol(proc.stdin.messages,proc.fixture) if m["method"] == "thread/fork")
    assert fork["params"]["model"] == "gpt-6.1-sol"
    assert fork["params"]["sandbox"] == "read-only"
    assert fork["params"]["ephemeral"] is True and "deferGoalContinuation" not in fork["params"]
    assert fork["params"]["config"] == {"mcp_servers": {"test.mcp": {"command": "test", "enabled": False}},
                                         "plugins": {"test@plugin": {"enabled": False}}}
    assert not [m for m in proc.stdin.messages if m["method"] == "thread/resume"]
    assert "features.shell_tool=false" in commands[0]


def test_pending_is_retained_if_codex_start_fails(env, monkeypatch):
    note = env["home"] / "note.md"
    note.write_text("交接内容" * 100)
    b._ro.mark_pending(env["home"], "app::codex", note, time.time(), SID)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-6.1-sol", "high", "codex"))
    async def spawn(*args, **kwargs):
        raise OSError("not installed")
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "_append_codex_ledger", lambda *args: None)
    asyncio.run(b._run_turn_codex("app", "hi"))
    assert b._ro.peek_pending(env["home"], "app::codex")
    assert b._load_session("app::codex") is None


@pytest.mark.parametrize("failure", [None, "start", "registration"])
def test_pending_consumed_only_after_acceptance_and_registration(env, monkeypatch, failure):
    note = env["home"] / "note.md"
    note.write_text("交接内容" * 100)
    b._ro.mark_pending(env["home"], "app::codex", note, time.time(), SID)
    rows = _rows(fork="fresh")
    if failure == "start":
        rows[3] = {"id": 3, "error": {"message": "turn rejected"}}
    proc = Proc(rows)
    proc.returncode = 0
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b, "SENDER", None)
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-current", "low", "codex"))
    for name in ("_append_codex_ledger", "_write_codex_status", "_remember", "_app_pin_last"):
        monkeypatch.setattr(b, name, lambda *args: None)
    if failure == "registration":
        monkeypatch.setattr(b._fs, "write_sessions", lambda *args, **kwargs: None)
    asyncio.run(b._run_turn_codex("app", "user message"))
    turn = next(m for m in proc.stdin.messages if m["method"] == "turn/start")
    assert note.read_text() in turn["params"]["input"][0]["text"]
    assert turn["params"]["model"] == "gpt-current", "handoff must not change normal runtime model"
    assert bool(b._ro.peek_pending(env["home"], "app::codex")) == bool(failure)
    assert b._load_session("app::codex") == (None if failure else "fresh")


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_cleanup(tmp_path, monkeypatch, cancel):
    proc = Proc([])
    entered = asyncio.Event()
    async def spawn(*args, **kwargs):
        return proc
    async def stuck(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "_codex_rpc_response", stuck)
    monkeypatch.setattr(b, "ROLLOVER_NOTE_TIMEOUT_S", 0.02)
    async def run():
        task = asyncio.create_task(b._rollover_write_note_codex(SID, tmp_path / "note", 3))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert "TimeoutError" in await task
    asyncio.run(run())
    assert proc.killed and not (tmp_path / "note").exists()


@pytest.mark.parametrize("ack", [1, 4, 2, 3])
@pytest.mark.parametrize("event", ["error", "tool", "completed_tool", "server_request"])
def test_pre_ack_failure_cannot_be_hidden_by_success(tmp_path, monkeypatch, ack, event):
    _mock_config_discovery(monkeypatch)
    rows = _rows()
    bad = {
        "error": {"method": "error", "params": {"error": {"message": "denied"}, "willRetry": True}},
        "tool": {"method": "item/started", "params": {"item": {"type": "commandExecution"}}},
        "completed_tool": {"method": "item/completed", "params": {"item": {"type": "fileChange"}}},
        "server_request": {"id": ack, "method": "item/tool/call", "params": {}},
    }[event]
    rows.insert(next(i for i, row in enumerate(rows) if row.get("id") == ack), bad)
    proc = Proc(rows)
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note"
    assert asyncio.run(b._rollover_write_note_codex(SID, note, 3))
    assert proc.killed and not note.exists()


def test_pre_ack_content_and_completion_are_replayed(tmp_path, monkeypatch):
    _mock_config_discovery(monkeypatch)
    rows = _rows()
    rows = rows[:3] + rows[4:] + [rows[3]]  # entire turn precedes the turn/start ack
    proc = Proc(rows)
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note"
    assert asyncio.run(b._rollover_write_note_codex(SID, note, 3)) == ""
    assert note.read_text() == "中文交接内容" * 60
    assert any(m["method"] == "initialized" for m in proc.stdin.messages)


@pytest.mark.parametrize("pre_ack", [False, True])
def test_known_informational_error_does_not_fail_handoff(tmp_path, monkeypatch, pre_ack):
    _mock_config_discovery(monkeypatch)
    rows = _rows()
    rows.insert(3 if pre_ack else 4, {"method": "error", "params": {"error": {
        "message": "Skill descriptions were shortened to fit the skills context budget"}}})
    proc = Proc(rows)
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note"
    assert asyncio.run(b._rollover_write_note_codex(SID, note, 3)) == ""
    assert note.exists()


@pytest.mark.parametrize("failure", ["corrupt", "unreadable", "array", "null", "missing",
    "empty", "entry_null", "entry_array", "entry_string", "entry_missing_path", "entry_relative",
    "entry_bad_prev", "entry_bad_at", "entry_nan", "entry_infinite", "entry_bool_at",
    "note_missing", "note_unreadable", "note_short"])
def test_invalid_required_handoff_never_starts_codex(env, monkeypatch, failure):
    from pathlib import Path
    note = env["home"] / "note.md"
    note.write_text("交接内容" * 100)
    b._ro.mark_pending(env["home"], "app::codex", note, time.time(), SID)
    ledger = b._ro._pending_file(env["home"])
    data = json.loads(ledger.read_text())
    entry = data["app::codex"]
    if failure == "corrupt":
        ledger.write_text('{broken')
    elif failure in ("array", "null", "empty"):
        ledger.write_text(json.dumps({"array": [], "null": None, "empty": {}}[failure]))
    elif failure == "missing":
        ledger.unlink()
    elif failure.startswith("entry_"):
        if failure in ("entry_null", "entry_array", "entry_string"):
            data["app::codex"] = {"entry_null": None, "entry_array": [], "entry_string": "bad"}[failure]
        elif failure == "entry_missing_path":
            entry.pop("path")
        elif failure == "entry_relative":
            entry["path"] = "relative.md"
        elif failure == "entry_bad_prev":
            entry["prev"] = None
        else:
            entry["at"] = {"entry_bad_at": "today", "entry_nan": float("nan"),
                           "entry_infinite": float("inf"), "entry_bool_at": True}[failure]
        ledger.write_text(json.dumps(data))
    elif failure == "note_missing":
        note.unlink()
    elif failure == "note_short":
        note.write_text(" ")
    before = ledger.read_bytes() if ledger.exists() else None
    b._fs.SESSIONS_FILE.write_text(json.dumps({"app::codex": {
        "active": None, "sessions": {}, "rollover_pending": True}}))
    read = Path.read_text
    if failure in ("unreadable", "note_unreadable"):
        target = ledger if failure == "unreadable" else note
        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("probe denied")
            return read(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", denied)
    spawned = []
    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("no thread may start without a valid handoff")
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    monkeypatch.setattr(b, "SENDER", None)
    monkeypatch.setattr(b, "_runtime_snapshot", lambda: ("gpt-current", "low", "codex"))
    monkeypatch.setattr(b, "_append_codex_ledger", lambda *args: None)
    asyncio.run(b._run_turn_codex("app", "hi"))
    assert not spawned and b._load_session("app::codex") is None
    assert any(ev["ev"] == "error" for ev in b.APP_FRONT.events)
    assert (ledger.read_bytes() if ledger.exists() else None) == before
    assert json.loads(b._fs.SESSIONS_FILE.read_text())["app::codex"]["rollover_pending"] is True


def test_broken_pending_ledger_is_not_overwritten(tmp_path):
    ledger = b._ro._pending_file(tmp_path)
    ledger.parent.mkdir()
    ledger.write_text('{broken')
    with pytest.raises(ValueError):
        b._ro.mark_pending(tmp_path, "app::codex", tmp_path / "note", time.time(), SID)
    assert ledger.read_text() == '{broken'


def test_writer_disables_supported_side_effect_surfaces():
    command = b._rollover_codex_command()
    for feature in ("apps", "plugins", "shell_tool", "unified_exec", "hooks", "goals",
                    "computer_use", "browser_use", "image_generation", "multi_agent"):
        assert f"features.{feature}=false" in command
    assert "notify=[]" in command
    assert not any("js_repl" in value or "apply_patch_freeform" in value for value in command)
    assert 'sandbox_mode="read-only"' in command


@pytest.mark.parametrize("ack", [1, 4])
def test_config_discovery_checks_pre_ack_errors(tmp_path, monkeypatch, ack):
    rows = _rows()[:2]
    rows.insert(next(i for i, row in enumerate(rows) if row.get("id") == ack),
                {"method": "error", "params": {"error": {"message": "discovery denied"}}})
    proc = Proc(rows)
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note"
    why = asyncio.run(b._rollover_write_note_codex(SID, note, 3))
    assert "discovery denied" in why and proc.killed and not note.exists()
    assert not any(m["method"].startswith("thread/") for m in proc.stdin.messages)


@pytest.mark.parametrize("failure", ["enabled_mcp", "enabled_plugin", "enabled_hook", "malformed"])
def test_generation_config_must_be_verified_before_fork(tmp_path, monkeypatch, failure):
    _mock_config_discovery(monkeypatch)
    rows = _rows()
    config = rows[1]["result"]["config"]
    if failure == "enabled_mcp":
        config["mcp_servers"]["test.mcp"]["enabled"] = True
    elif failure == "enabled_plugin":
        config["plugins"]["test@plugin"]["enabled"] = True
    elif failure == "enabled_hook":
        config["features"]["hooks"] = True
    else:
        config["mcp_servers"] = []
    proc = Proc(rows)
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(b.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(b, "CODEX_ENABLED", True)
    note = tmp_path / "note"
    assert asyncio.run(b._rollover_write_note_codex(SID, note, 3))
    assert proc.killed and not note.exists()
    assert not any(m["method"] == "thread/fork" for m in proc.stdin.messages)


def test_connector_cli_overrides_preserve_literal_names():
    import tomllib
    override = {"mcp_servers": {"a.b@plugin": {"command": "python", "enabled": False,
        "args": ["-c", "print('hello')"], "env": {"A": "value"}, "tool_timeout_sec": None}}}
    command = b._rollover_codex_command(override)
    encoded = next(arg for arg in command if arg.startswith("mcp_servers="))
    got = tomllib.loads(encoded)["mcp_servers"]["a.b@plugin"]
    assert got["enabled"] is False and got["args"] == override["mcp_servers"]["a.b@plugin"]["args"]
    assert "tool_timeout_sec" not in got


@pytest.mark.skipif(os.getenv("FLEET_RUN_CODEX_ROLLOVER_PROBE") != "1",
                    reason="opt-in live App Server probe; uses isolated home and synthetic thread")
def test_live_rollover_restrictions_and_handoff(monkeypatch):
    """No production threads/config changes. Negative controls have observable markers."""
    import shutil
    import socket
    import subprocess
    import sys
    import tempfile
    import threading
    from pathlib import Path

    # Outside /tmp and CODEX_HOME: those locations can have special sandbox allowances.
    with tempfile.TemporaryDirectory(prefix="codex-rollover-probe-", dir=Path.home()) as folder:
        root = Path(folder)
        work, home = root / "work", root / "codex"
        work.mkdir()
        home.mkdir()
        auth = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        shutil.copyfile(auth, home / "auth.json")
        (home / "auth.json").chmod(0o600)
        mcp_marker, write_marker = root / "mcp-started", root / "write-attempt"
        mcp_code = (
            "import pathlib,sys,json; pathlib.Path(" + repr(str(mcp_marker)) + ").write_text('started');\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            " if 'id' not in r: continue\n"
            " result=({'protocolVersion':'2024-11-05','capabilities':{'tools':{}},"
            "'serverInfo':{'name':'probe','version':'1'}} if r['method']=='initialize' else {'tools':[]})\n"
            " print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n")
        home.joinpath("config.toml").write_text(
            '[features]\nhooks=true\n[plugins."probe@missing"]\nenabled=true\n'
            '[mcp_servers."probe.connector"]\ncommand=' + json.dumps(sys.executable) + '\nargs='
            + json.dumps(["-c", mcp_code]) + '\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(home))
        monkeypatch.setattr(b, "REPO", str(work))
        monkeypatch.setattr(b, "CODEX_ENABLED", True)
        monkeypatch.setattr(b, "ROLLOVER_NOTE_TIMEOUT_S", 180)

        feature_output = subprocess.check_output([b.CODEX_BIN, "features", "list"], text=True)
        features = {row.split()[0]: row.split()[1:-1] for row in feature_output.splitlines()}
        for option in b._rollover_codex_command():
            if option.startswith("features."):
                feature = option.split(".", 1)[1].split("=", 1)[0]
                assert feature in features and "removed" not in features[feature]

        async def run():
            proc = None
            stderr = None
            async def start(overrides=None):
                nonlocal proc, stderr
                proc = await asyncio.create_subprocess_exec(*b._rollover_codex_command(overrides), cwd=work,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=b.CODEX_STREAM_LIMIT)
                stderr = asyncio.create_task(proc.stderr.read())
                await rpc(1, "initialize", {"clientInfo": {"name": "rollover-probe", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
                await b._codex_rpc_send(proc, {"method": "initialized"})
            async def rpc(number, method, params):
                await b._codex_rpc_send(proc, {"id": number, "method": method, "params": params})
                return (await asyncio.wait_for(b._codex_rpc_response(proc, number), 45))["result"]
            async def close():
                if proc and proc.returncode is None:
                    proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                if stderr:
                    stderr.cancel()
                    with b.contextlib.suppress(BaseException):
                        await stderr

            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.1)
            port = listener.getsockname()[1]
            hits, stop = [], threading.Event()
            def accept():
                while not stop.is_set():
                    try:
                        client, _ = listener.accept()
                        hits.append(1)
                        client.close()
                    except socket.timeout:
                        pass
            thread = threading.Thread(target=accept, daemon=True)
            thread.start()
            try:
                # Prove the network endpoint and connector marker are live before negatives.
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    pass
                await start()
                await rpc(2, "thread/start", {"cwd": str(work), "sandbox": "read-only",
                    "approvalPolicy": "never", "config": {"mcp_servers": {"probe.connector": {
                        "command": sys.executable, "args": ["-c", mcp_code], "enabled": True}}}})
                control_status = await rpc(9, "mcpServerStatus/list", {})
                for _ in range(100):
                    if mcp_marker.exists():
                        break
                    await asyncio.sleep(0.1)
                assert mcp_marker.exists(), f"positive MCP startup control must run: {control_status}"
                await close()
                mcp_marker.unlink()
                cfg = await b._rollover_codex_tool_config(lambda ev: None)
                assert not mcp_marker.exists(), "config-only discovery must not initialize tools"
                disabled = {section: {name: {**{k: v for k, v in value.items() if v is not None},
                                             "enabled": False}
                                      for name, value in cfg.get(section, {}).items()}
                            for section in ("mcp_servers", "plugins")}
                await start(disabled)
                cfg = (await rpc(3, "config/read", {"cwd": str(work), "includeLayers": False}))["config"]
                for feature in ("apps", "plugins", "hooks", "goals", "shell_tool", "unified_exec"):
                    assert cfg["features"][feature] is False
                for section in ("mcp_servers", "plugins"):
                    assert all(value["enabled"] is False for value in cfg.get(section, {}).values())
                source = await rpc(4, "thread/start", {"cwd": str(work), "model": b.ROLLOVER_CODEX_MODEL,
                    "sandbox": "read-only", "approvalPolicy": "never", "config": disabled})
                assert not mcp_marker.exists()
                statuses = await rpc(5, "mcpServerStatus/list", {})
                # The status API also lists configured-but-disabled entries. The
                # process marker is the authoritative initialization observation.
                assert all(not row.get("tools") for row in statuses["data"])
                assert not mcp_marker.exists(), "even process-level discovery must not start MCP"
                policy = {"type": "readOnly", "networkAccess": False}
                denied_write = await rpc(6, "command/exec", {"cwd": str(work), "sandboxPolicy": policy,
                    "command": [sys.executable, "-c", "from pathlib import Path; Path(" +
                                repr(str(write_marker)) + ").write_text('unsafe')"]})
                assert denied_write["exitCode"] != 0 and not write_marker.exists()
                # Allow the positive host connection to be counted before taking its watermark.
                while not hits:
                    await asyncio.sleep(0.01)
                previous = len(hits)
                denied_network = await rpc(7, "command/exec", {"cwd": str(work), "sandboxPolicy": policy,
                    "command": [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1'," +
                                str(port) + "),timeout=2)"]})
                assert denied_network["exitCode"] != 0 and len(hits) == previous
                sid = source["thread"]["id"]
                accepted = await rpc(8, "turn/start", {"threadId": sid, "model": b.ROLLOVER_CODEX_MODEL,
                    "effort": "low", "sandboxPolicy": policy, "approvalPolicy": "never",
                    "input": [{"type": "text", "text": "这是隔离验收会话。待办：检查夜间换会话。"
                        "下一步：补安全测试。约定：不动生产会话，不调用工具。等用户批准发布。只回复收到。"}]})
                turn = accepted["turn"]["id"]
                while raw := await asyncio.wait_for(proc.stdout.readline(), 90):
                    ev = json.loads(raw)
                    if ev.get("method") == "turn/completed":
                        got = ev["params"]["turn"]
                        assert got["id"] == turn and got["status"] == "completed"
                        break
                else:
                    raise AssertionError("synthetic source turn did not complete")
                await close()
                transcript = b._ro.codex_transcript_path(sid, home)
                assert transcript is not None
                before = b._ro.fingerprint(transcript)
                note = root / "handoff.md"
                why = await b._rollover_write_note_codex(sid, note, 3)
                assert why == "", why
                assert b._ro.fingerprint(transcript) == before
                assert len(note.read_bytes()) >= b._ro.NOTE_MIN_BYTES
                assert not mcp_marker.exists() and not write_marker.exists() and len(hits) == previous
                print("LIVE_ROLLOVER_OK", json.dumps({"note_bytes": len(note.read_bytes()),
                    "source_unchanged": True, "write_denied": denied_write["exitCode"],
                    "network_denied": denied_network["exitCode"], "mcp_start_control": True,
                    "mcp_disabled": True, "apps_plugins_hooks_disabled": True}))
            finally:
                await close()
                stop.set()
                thread.join(timeout=1)
                listener.close()
        asyncio.run(run())

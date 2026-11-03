"""Actual selected stdout helper with synthetic coordinator/process only."""
import asyncio
import inspect
import ast
import json
from pathlib import Path
import pytest
from feishu_bridge.managed import fleet_managed_child as child
from feishu_bridge.managed import fleet_managed_ingress as ingress
from tests.managed.test_managed_entry_source import bound, deliver
from tests.managed.test_managed_child_source import native, Process, fake_spawn
from tests.managed.test_managed_native_write_source import source_namespace, Sink
from feishu_bridge.managed import fleet_managed_runtime as runtime
from feishu_bridge.managed import fleet_managed_codex as managed_codex


class Lines:
    def __init__(self, rows):
        self.rows = list(rows)
        self.reads = 0

    async def readline(self):
        self.reads += 1
        return self.rows.pop(0) if self.rows else b""

    async def read(self, n=-1):
        return b""

    def at_eof(self):
        return not self.rows


def test_actual_selected_rpc_cannot_borrow_replaced_stdout(native, monkeypatch):
    b = native
    proc = Process()
    proc.stdin = Sink()
    proc.stdout = Lines([])
    fake_spawn(monkeypatch, proc)
    ns, _, _, _ = source_namespace(monkeypatch, b)

    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        foreign = Lines([b'{"id":1,"result":{"synthetic_foreign":true}}\n'])
        p.stdout = foreign
        kw = {'reader': owner} if 'reader' in inspect.signature(ns['_codex_rpc_response']).parameters else {}
        with pytest.raises(ingress.Refused):
            await ns['_codex_rpc_response'](p, 1, **kw)
        assert foreign.reads == 0
        raise ingress.Refused('fixture-final-hold')

    b.e._turn = turn
    result = asyncio.run(deliver(b))
    assert isinstance(result[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert b.e._native_owners[b.frame['work_id']]._process is proc


@pytest.mark.parametrize('draining', [False, True])
def test_actual_owned_rpc_keeps_unicode_notifications_and_canonical_result(native, monkeypatch, draining):
    b = native
    event = {'method': 'synthetic/notification', 'params': {'text': '\ufeff中文e\u0301👩‍💻\r\n'}}
    result = {'id': 21, 'result': {'config': {'synthetic': True}}}
    proc = Process(); proc.stdin = Sink()
    proc.stdout = Lines([(json.dumps(row, ensure_ascii=False) + '\n').encode() for row in [event, result]])
    fake_spawn(monkeypatch, proc)
    ns, _, _, _ = source_namespace(monkeypatch, b)
    seen = []
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        if draining: b.c.begin_drain(b.c.snapshot()['epoch'])
        assert await ns['_codex_rpc_response'](p, 21, on_event=seen.append, reader=owner) == result
        assert seen == [event] and owner._stdout_task is None
        disposition = await owner.finish(p, asyncio.create_task(owner.read_stderr()))
        assert not disposition.native_ready and not disposition.complete_descendant_exclusion
    b.e._turn = turn
    assert asyncio.run(deliver(b)) == [None]


@pytest.mark.parametrize('raw', [
    b'{"id":true,"result":{}}\n', b'{"id":1.0,"result":{}}\n',
    b'{"id":1,"id":1,"result":{}}\n',
    b'{"id":1,"result":{"a":1,"\\u0061":2}}\n',
    b'{"id":1,"result":{"text":"\xff"}}\n',
    b'{"id":1,"result":{"text":"\\ud800"}}\n',
    b'{"id":1,"result":{"n":NaN}}\n',
    b'{"id":1,"result":{"n":1e999}}\n',
    b'{"id":1,"result":{"n":' + b'1' * 65 + b'}}\n',
    b'[]\n', b'{"id":1,"result":{}}',
    b'{"id":1,"result":{}}\n{}\n',
    b'{"id":1,"result":' + b'[' * 65 + b'0' + b']' * 65 + b'}\n',
])
def test_original_malformed_frame_never_redecodes_or_replays(native, monkeypatch, raw):
    b = native; proc = Process(); proc.stdin = Sink(); proc.stdout = Lines([raw, b'{"id":1,"result":{}}\n'])
    fake_spawn(monkeypatch, proc)
    ns, _, _, _ = source_namespace(monkeypatch, b)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        with pytest.raises(ingress.Refused, match='managed_native_frame_unknown'):
            await ns['_codex_rpc_response'](p, 1, reader=owner)
        with pytest.raises(ingress.Refused): await owner.read_frame(p)
        assert proc.stdout.reads == 1 and owner._read_failed
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


@pytest.mark.parametrize('fault', ['limit', 'foreign_process', 'post_stdout', 'post_scope', 'post_action'])
def test_owned_read_refusal_retains_exact_pipe_and_no_second_read(native, monkeypatch, fault):
    b = native; proc = Process(); proc.stdin = Sink()
    original = Lines([b'{"id":1,"result":{}}\n']); proc.stdout = original
    fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        if fault == 'limit': owner._stream_limit = 4
        if fault == 'foreign_process': p = Process()
        if fault.startswith('post_'):
            async def read():
                raw = await Lines.readline(original)
                if fault == 'post_stdout': proc.stdout = Lines([])
                if fault == 'post_scope': monkeypatch.setattr(child.storage_module, 'select', lambda: None)
                if fault == 'post_action': runtime.CURRENT.set(None)
                return raw
            original.readline = read
        with pytest.raises(BaseException): await owner.read_frame(p)
        with pytest.raises(BaseException): await owner.read_frame(p)
        assert original.reads == (0 if fault == 'foreign_process' else 1)
        assert owner._stdout is original and owner._read_failed
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_cancelled_read_retains_actual_task_and_original_owner(native, monkeypatch):
    b = native; proc = Process(); proc.stdin = Sink(); original = Lines([]); proc.stdout = original
    fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        started = asyncio.Event()
        async def wait(): started.set(); await asyncio.Future()
        original.readline = wait
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        task = asyncio.create_task(owner.read_frame(p))
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert owner._stdout_task is task and owner._stdout is original
        with pytest.raises(ingress.Refused): await owner.read_frame(p)
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.e._native_owners[b.frame['work_id']]._read_failed


def test_concurrent_read_holds_without_borrowing_first_read(native, monkeypatch):
    b = native; proc = Process(); proc.stdin = Sink(); original = Lines([]); proc.stdout = original
    fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        started, release = asyncio.Event(), asyncio.Event()
        async def wait():
            started.set(); await release.wait(); return b'{"id":1,"result":{}}\n'
        original.readline = wait
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        first = asyncio.create_task(owner.read_frame(p)); await started.wait()
        with pytest.raises(ingress.Refused): await owner.read_frame(p)
        assert owner._stdout_task is first
        release.set()
        with pytest.raises(BaseException): await first
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


@pytest.mark.parametrize('frame', [
    {'id': 1, 'result': None}, {'id': 1, 'result': [],},
    {'id': 1, 'result': {}, 'error': {}}, {'id': 1, 'result': {}, 'jsonrpc': '2.0'},
    {'id': 1, 'error': 'SYNTHETIC_PRIVATE'},
    {'id': 1, 'error': {'message': 'SYNTHETIC_PRIVATE'}},
])
def test_matched_rpc_requires_original_shape_and_fixed_error(native, monkeypatch, frame):
    b = native; proc = Process(); proc.stdin = Sink(); proc.stdout = Lines([(json.dumps(frame) + '\n').encode()])
    fake_spawn(monkeypatch, proc)
    ns, _, _, _ = source_namespace(monkeypatch, b)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        with pytest.raises(ingress.Refused) as error:
            await ns['_codex_rpc_response'](p, 1, reader=owner)
        assert 'SYNTHETIC_PRIVATE' not in str(error.value)
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_write_post_await_pipe_drift_is_not_accepted(native, monkeypatch):
    b = native; proc = Process(); sink = Sink(); proc.stdin = sink; fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        async def drain(): proc.stdin = Sink()
        sink.drain = drain
        with pytest.raises(ingress.Refused): await owner.write(p, b'{"id":1}\n')
        assert owner._stdin is sink and len(sink.frames) == 1
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['active_leases'] == 2


def test_stderr_post_await_pipe_drift_retains_original_pipe(native, monkeypatch):
    b = native; proc = Process(); proc.stdin = Sink(); original = proc.stderr
    fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        p = await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        async def read(n): proc.stderr = Lines([]); return b''
        original.read = read
        with pytest.raises(ingress.Refused): await owner.read_stderr()
        assert owner._stderr is original and owner._process is p
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_actual_selected_turn_reads_startup_and_live_frames_through_original_owner(native, monkeypatch):
    b = native; del b.children
    ns, events, _, _ = source_namespace(monkeypatch, b)
    ns['_MANAGED_ENTRY'] = b.e; ns['REPO'] = str(b.r.selection.target_home)
    ns['_codex_archived_binding'] = lambda *a, **kw: None
    ns['_load_session'] = lambda *a: None
    monkeypatch.setattr(child.storage_module, 'session_mapping', lambda *a: None)
    registrations = []
    monkeypatch.setattr(child.storage_module, 'record_session', lambda *a, **kw: registrations.append((a, kw)))
    monkeypatch.setattr(ns['codex_questions'], 'selected_protocol', lambda _: None)
    env = b.scope.env()
    config = {'sqlite_home': str(b.scope.home), 'forced_login_method': 'chatgpt',
        'cli_auth_credentials_store': 'file', 'model_provider': 'openai', 'project_root_markers': [],
        'shell_environment_policy': {'set': {**{k: str(b.scope.home) for k in child.storage_module._RESERVED},
            child.storage_module.DESKTOP_KEY: str(b.scope.desktop_home), **managed_codex.bindings(env)}}}
    rows = [{'id': 1, 'result': {}}, {'id': 21, 'result': {'config': config}},
        {'id': 2, 'result': {'thread': {'id': 'synthetic-thread'}}},
        {'id': 3, 'result': {'turn': {'id': 'synthetic-turn'}}},
        {'method': 'item/agentMessage/delta', 'params': {'threadId': 'synthetic-thread',
            'turnId': 'synthetic-turn', 'delta': '\ufeff完整中文👩‍💻\r\n'}},
        {'method': 'turn/completed', 'params': {'threadId': 'synthetic-thread',
            'turn': {'id': 'synthetic-turn', 'status': 'completed'}}}]
    proc = Process(); proc.stdin = Sink(); original = Lines([(json.dumps(r, ensure_ascii=False) + '\n').encode() for r in rows]); proc.stdout = original
    calls = fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid): await ns['_run_turn_codex'](chat, prompt, mid)
    b.e._turn = turn
    assert asyncio.run(deliver(b)) == [None]
    assert original.reads == 6 and len(calls) == 1 and b.e._native_owners == {}
    assert [json.loads(v).get('method') for v in proc.stdin.frames] == [
        'initialize', 'notifications/initialized', 'config/read', 'thread/start', 'turn/start']
    assert any(e.get('ev') == 'stream' and e.get('text') == '\ufeff完整中文👩‍💻\r\n' for e in events)
    assert registrations  # Test-only map stub; this test supplies NO map/native proof.


def test_actual_selected_startup_and_stream_use_one_owned_reader():
    tree = ast.parse((Path(__file__).resolve().parents[2] / 'src/feishu_bridge/bridge/fleet_bridge.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_turn_codex')
    wrapper = next(n for n in ast.walk(node) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'native_response')
    assert 'reader=managed_native' in ast.unparse(wrapper)
    # Resume and start now have separate response sites for main's guarded recovery.
    assert sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'native_response' for n in ast.walk(node)) == 6
    assert all(n in set(ast.walk(wrapper)) for n in ast.walk(node)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == '_codex_rpc_response')
    frames = next(n for n in ast.walk(node) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'frames')
    assert 'managed_native.read_frame(proc)' in ast.unparse(frames)
    assert ast.unparse(frames).index('managed_native.read_frame(proc)') < ast.unparse(frames).index('async for raw in proc.stdout')


@pytest.mark.parametrize('kind,value', [('thread', True), ('thread', 123), ('thread', ''),
    ('thread', ' other '), ('turn', False), ('turn', []), ('turn', '\ud800')])
def test_original_thread_and_turn_identifiers_cannot_be_coerced(native, monkeypatch, kind, value):
    b = native; proc = Process(); proc.stdin = Sink(); fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        with pytest.raises(ingress.Refused, match='managed_native_identifier_unknown'):
            owner.identifier({'id': 2, 'result': {kind: {'id': value}}}, kind)
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


@pytest.mark.parametrize('fault', ['thread', 'bool_id', 'missing_turn', 'status'])
def test_same_turn_terminal_cannot_borrow_another_thread_or_original_shape(native, monkeypatch, fault):
    b = native; proc = Process(); proc.stdin = Sink(); fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        params = {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}
        if fault == 'thread': params['threadId'] = 'another-thread'
        if fault == 'bool_id': params['turn']['id'] = True
        if fault == 'missing_turn': del params['turn']
        if fault == 'status': params['turn']['status'] = True
        with pytest.raises(ingress.Refused): owner.terminal_matches(params, 'thread', 'turn')
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_unselected_rpc_keeps_existing_decode_and_callback_policy(monkeypatch):
    from tests.codex.test_codex_questions import source_namespace as legacy_namespace
    ns, _, _, _ = legacy_namespace(monkeypatch)
    proc = Process(); proc.stdout = Lines([b'invalid\n', b'{"method":"notice","params":{}}\n', b'{"id":1,"result":{}}\n'])
    seen = []
    assert asyncio.run(ns['_codex_rpc_response'](proc, 1, on_event=seen.append)) == {'id': 1, 'result': {}}
    assert seen == [{'method': 'notice', 'params': {}}] and proc.stdout.reads == 3

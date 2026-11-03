"""Actual selected turn with synthetic coordinator, wire and process only."""
import asyncio
import ast
import json
from pathlib import Path

import pytest

from feishu_bridge.managed import fleet_managed_child as child
from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.managed import fleet_managed_codex as managed_codex
from feishu_bridge.sessions import fleet_target_control as control
from tests.managed.test_managed_entry_source import bound, deliver
from tests.managed.test_managed_child_source import native, Process, fake_spawn
from tests.managed.test_managed_native_write_source import source_namespace, Sink
from tests.managed.test_managed_native_read_source import Lines


TEXT = '\ufeff本轮中文e\u0301👩‍💻\r\n'
FOREIGN = 'SYNTHETIC_OTHER_THREAD_CONTENT'


def turn_fixture(b, monkeypatch, notifications):
    ns, events, logs, _ = source_namespace(monkeypatch, b)
    # Bind actual existing display helpers, rather than inherited empty stubs.
    tree = ast.parse((Path(__file__).resolve().parents[2] / 'src/feishu_bridge/bridge/fleet_bridge.py').read_text())
    helpers = {'_codex_text_value', '_codex_reasoning_text', '_codex_tool_brief', '_codex_error_event',
               '_codex_usage_from', '_CodexTurnUsage'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in helpers]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'fleet_bridge.py', 'exec'), ns)
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
        {'id': 3, 'result': {'turn': {'id': 'synthetic-turn'}}}, *notifications,
        {'method': 'turn/completed', 'params': {'threadId': 'synthetic-thread',
            'turn': {'id': 'synthetic-turn', 'status': 'completed'}}}]
    proc = Process(); proc.stdin = Sink()
    proc.stdout = Lines([(json.dumps(r, ensure_ascii=False) + '\n').encode() for r in rows])
    calls = fake_spawn(monkeypatch, proc)
    async def turn(chat, prompt, mid): await ns['_run_turn_codex'](chat, prompt, mid)
    b.e._turn = turn
    return proc, calls, events, logs, registrations


def notification(method='item/agentMessage/delta', **params):
    return {'method': method, 'params': {'threadId': 'synthetic-thread',
        'turnId': 'synthetic-turn', 'itemId': 'synthetic-item', 'delta': TEXT, **params}}


def test_actual_selected_turn_cannot_borrow_other_thread_display(native, monkeypatch):
    b = native
    proc, calls, events, _, _ = turn_fixture(b, monkeypatch,
        [notification(threadId='other-thread', delta=FOREIGN), notification()])
    assert asyncio.run(deliver(b)) == [None]
    streams = [e['text'] for e in events if e.get('ev') == 'stream']
    assert streams == [TEXT]
    assert all(FOREIGN not in str(e) for e in events)
    assert len(calls) == 1 and b.e._native_owners == {}


@pytest.mark.parametrize('method', ['item/agentMessage/delta', 'item/completed', 'item/started',
    'item/reasoning/summaryTextDelta', 'error', 'thread/tokenUsage/updated'])
@pytest.mark.parametrize('field', ['threadId', 'turnId'])
def test_other_owned_wire_scope_cannot_reach_any_existing_display_consumer(native, monkeypatch, method, field):
    b = native
    event = notification(method, **{field: 'other-scope'}, delta=FOREIGN,
        item={'id': 'synthetic-item', 'type': 'agentMessage' if method == 'item/completed' else 'commandExecution',
              'text': FOREIGN, 'command': FOREIGN},
        error={'message': FOREIGN}, willRetry=False,
        tokenUsage={'last': {'inputTokens': 999}, 'total': {'inputTokens': 999}})
    proc, calls, events, logs, _ = turn_fixture(b, monkeypatch, [event, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [TEXT]
    assert all(FOREIGN not in str(e) for e in events + logs)
    assert not any(e.get('ev') in {'reasoning', 'tool'} for e in events)
    assert b.c.snapshot()['state'] == 'OPEN' and len(calls) == 1


@pytest.mark.parametrize('fault', ['missing_thread', 'missing_turn', 'bool_thread', 'number_turn',
    'empty_turn', 'null_thread', 'list_thread', 'delta_bool', 'delta_object', 'item_text_bool',
    'item_shape', 'error_message_bool', 'retry_string', 'usage_bool', 'server_request_id',
    'mixed_type_alias', 'mixed_event_alias', 'numeric_replay', 'legacy_only'])
def test_original_notification_shape_refuses_without_body_coercion_or_parent_settlement(native, monkeypatch, fault):
    b = native; event = notification()
    p = event['params']
    if fault == 'missing_thread': del p['threadId']
    elif fault == 'missing_turn': del p['turnId']
    elif fault == 'bool_thread': p['threadId'] = True
    elif fault == 'number_turn': p['turnId'] = 123
    elif fault == 'empty_turn': p['turnId'] = ''
    elif fault == 'null_thread': p['threadId'] = None
    elif fault == 'list_thread': p['threadId'] = []
    elif fault == 'delta_bool': p['delta'] = True
    elif fault == 'delta_object': p['delta'] = {'synthetic_private': FOREIGN}
    elif fault == 'item_text_bool': event = notification('item/completed', item={'type': 'agentMessage', 'text': True})
    elif fault == 'item_shape': event = notification('item/started', item=True)
    elif fault == 'error_message_bool': event = notification('error', error={'message': True}, willRetry=False)
    elif fault == 'retry_string': event = notification('error', error={'message': FOREIGN}, willRetry='false')
    elif fault == 'usage_bool': event = notification('thread/tokenUsage/updated', tokenUsage=True)
    elif fault == 'server_request_id': event['id'] = 101
    elif fault == 'mixed_type_alias': event['type'] = 'item.completed'
    elif fault == 'mixed_event_alias': event['event'] = 'item.completed'
    elif fault == 'numeric_replay': event['replay'] = 1
    elif fault == 'legacy_only': event = {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': FOREIGN}}
    proc, calls, events, logs, _ = turn_fixture(b, monkeypatch, [event, notification()])
    result = asyncio.run(deliver(b))
    assert isinstance(result[0], (ingress.Refused, control.Refused))
    assert str(result[0]) in {'managed_native_notification_unknown', 'admission_closed'}
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert b.e._native_owners[b.frame['work_id']]._stderr_task is not None
    assert not any(e.get('ev') in {'stream', 'reasoning', 'tool'} for e in events)
    assert all(FOREIGN not in str(e) for e in events + logs)
    assert len(calls) == 1 and proc.stdout.reads == 5


def test_matching_unicode_and_reasoning_keep_exact_bytes_during_parent_drain(native, monkeypatch):
    b = native
    proc, calls, events, _, registrations = turn_fixture(b, monkeypatch,
        [notification('item/reasoning/summaryTextDelta'), notification()])
    read = proc.stdout.readline
    async def draining_read():
        if proc.stdout.reads == 4:
            b.c.begin_drain(b.c.snapshot()['epoch'])
        return await read()
    proc.stdout.readline = draining_read
    assert asyncio.run(deliver(b)) == [None]
    assert [e['text'].encode('utf-8') for e in events if e.get('ev') == 'stream'] == [TEXT.encode('utf-8')]
    assert [e['text'].encode('utf-8') for e in events if e.get('ev') == 'reasoning'] == [TEXT.encode('utf-8')]
    assert b.c.snapshot()['state'] == 'DRAINING' and b.c.snapshot()['active_leases'] == 0
    assert len(calls) == 1 and registrations  # Stub-only; not runtime/map proof.


def test_replayed_body_is_omitted_without_reconstruction_or_duplicate_surface(native, monkeypatch):
    b = native; replay = notification(delta=FOREIGN); replay['replay'] = True
    _, calls, events, _, _ = turn_fixture(b, monkeypatch, [replay, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [TEXT]
    assert len(calls) == 1


def test_terminal_other_turn_does_not_end_or_attribute_current_body(native, monkeypatch):
    b = native
    foreign = {'method': 'turn/completed', 'params': {'threadId': 'other-thread',
        'turn': {'id': 'other-turn', 'status': 'completed'}, 'tokenUsage': {'last': {'inputTokens': 999}}}}
    _, _, events, _, _ = turn_fixture(b, monkeypatch, [foreign, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [TEXT]


def test_original_stderr_task_survives_private_failure_and_turn_unwind(native, monkeypatch):
    b = native; proc = Process(); proc.stdin = Sink(); fake_spawn(monkeypatch, proc)
    async def fail(n): raise ValueError('SYNTHETIC_PRIVATE_STDERR_FAILURE')
    proc.stderr.read = fail
    owned = []
    async def turn(chat, prompt, mid):
        owner = b.e.native_child(chat, prompt, None, False)
        await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        task = owner.start_stderr()
        owned.append(task)
        with pytest.raises(ValueError): await task
        await asyncio.sleep(0)  # Observe private done state, never export it.
        assert owner._stderr_task is task and owner._stderr_observed
        assert type(owner._stderr_failure) is ValueError
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._stderr_task is owned[0] and owner._process is proc
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_original_stderr_task_cannot_be_replaced_by_another_reader(native, monkeypatch):
    b = native; proc = Process(); proc.stdin = Sink(); fake_spawn(monkeypatch, proc)
    ready = None
    async def turn(chat, prompt, mid):
        nonlocal ready
        ready = asyncio.Event()
        async def blocked(n): await ready.wait(); return b''
        proc.stderr.read = blocked
        owner = b.e.native_child(chat, prompt, None, False)
        await owner.spawn(b.scope, '/synthetic/codex', str(b.r.selection.target_home), 1024)
        original = owner.start_stderr()
        await asyncio.sleep(0)
        second = asyncio.create_task(owner.read_stderr())
        with pytest.raises(ingress.Refused, match='managed_native_stderr_owner_unknown'): await second
        assert owner._stderr_task is original
        ready.set()
        await asyncio.gather(original, return_exceptions=True)
        raise ingress.Refused('fixture-final-hold')
    b.e._turn = turn
    assert isinstance(asyncio.run(deliver(b))[0], ingress.Refused)
    assert b.e._native_owners[b.frame['work_id']]._stderr_observed
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_auxiliary_unscoped_payload_cannot_enter_current_turn_usage(native, monkeypatch):
    b = native
    auxiliary = {'method': 'synthetic/auxiliary', 'params': {'tokenUsage': {
        'last': {'inputTokens': 999, 'outputTokens': 999},
        'total': {'inputTokens': 999, 'outputTokens': 999}}}}
    _, _, events, _, _ = turn_fixture(b, monkeypatch, [auxiliary, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert not any(event.get('ev') == 'ctx' for event in events)
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [TEXT]


def test_matching_owned_usage_still_reaches_actual_usage_tracker(native, monkeypatch):
    b = native
    usage = notification('thread/tokenUsage/updated', tokenUsage={
        'last': {'inputTokens': 7, 'outputTokens': 3},
        'total': {'inputTokens': 7, 'outputTokens': 3}})
    _, _, events, _, _ = turn_fixture(b, monkeypatch, [usage, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert any(event.get('ev') == 'ctx' for event in events)
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [TEXT]

"""Actual selected file helpers, fake native process and fresh store only."""
import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os
import pytest

import fleet_managed_ingress as ingress
import fleet_target_control as control
from test_managed_child_source import native
from test_managed_entry_source import bound, deliver
import test_managed_native_notification_source as previous

PRIVATE = "SYNTHETIC_PRIVATE_PERSISTENCE_FAILURE"


def actual_helpers(b, monkeypatch):
    captured = {}
    original = previous.source_namespace
    def namespace(*args):
        ns, events, logs, sessions = original(*args)
        tree = ast.parse((Path(__file__).parents[1] / 'fleet_bridge.py').read_bytes())
        names = {'_write_codex_status', '_append_codex_ledger'}
        nodes = [x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name in names]
        ns.update(datetime=datetime, timezone=timezone, DEFAULT_MODEL='synthetic-default',
                  DEFAULT_EFFORT='xhigh', _STATUS_FILE=b.r.selection.target_home/'status.json',
                  CODEX_LEDGER_FILE=b.r.selection.target_home/'codex_ledger.jsonl')
        ns['APP_FRONT'].broadcast_runtime = lambda *_: None
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'fleet_bridge.py', 'exec'), ns)
        captured['ns'] = ns
        return ns, events, logs, sessions
    monkeypatch.setattr(previous, 'source_namespace', namespace)
    result = previous.turn_fixture(b, monkeypatch, [previous.notification()])
    return (*result, captured['ns'])


def assert_unknown(b, proc, calls, logs, result):
    assert isinstance(result[0], (ingress.Refused, control.Refused)), 'selected file helper swallowed uncertainty'
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['unresolved'] == 1
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._terminal.status == 'completed'
    assert len(calls) == 1 and PRIVATE not in str(logs)


def test_actual_status_failure_is_not_swallowed_or_followed_by_ledger(native, monkeypatch):
    b = native; del b.children
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    import fleet_managed_file_io as file_io
    original = os.write
    writes = []
    def fail(fd, value):
        pins = [p for k, p in file_io._READERS.items()
                if p.fd == fd and file_io._STATES[k] == 'OPEN' and p.name == 'status.tmp']
        if pins:
            assert len(pins) == 1
            writes.append(ns['_STATUS_FILE'].with_suffix('.tmp')); raise OSError(PRIVATE)
        return original(fd, value)
    monkeypatch.setattr(os, 'write', fail)
    result = asyncio.run(deliver(b))
    assert_unknown(b, proc, calls, logs, result)
    assert len(writes) == 1 and not ns['CODEX_LEDGER_FILE'].exists()
    before = list(writes)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert writes == before and len(calls) == 1


def test_actual_ledger_failure_is_not_swallowed_after_status_replace(native, monkeypatch):
    b = native; del b.children
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    original = os.open
    attempts = []
    def fail(path, *args, **kwargs):
        if path == ns['CODEX_LEDGER_FILE'].name and kwargs.get('dir_fd') is not None:
            attempts.append(ns['CODEX_LEDGER_FILE']); raise OSError(PRIVATE)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', fail)
    result = asyncio.run(deliver(b))
    assert_unknown(b, proc, calls, logs, result)
    assert ns['_STATUS_FILE'].exists() and len(attempts) == 1
    before = list(attempts)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert attempts == before and len(calls) == 1


def test_real_returned_helpers_retain_pending_tails_without_claiming_file_settlement(native, monkeypatch):
    b = native; del b.children
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    import json
    preserved = {'engine': 'claude', 'text': 'synthetic-other-row e\u0301\r\n', 'count': True}
    ns['_STATUS_FILE'].write_text(json.dumps({'sessions': {'other-native-chat': preserved}}))
    result = asyncio.run(deliver(b))
    # Actual file effects occurred, but two unresolved helper tails prohibit parent settlement.
    assert isinstance(result[0], control.Refused)
    value = json.loads(ns['_STATUS_FILE'].read_bytes())
    assert value['sessions']['other-native-chat'] == preserved
    assert value['sessions'][b.e.route.native_chat]['thread'] == 'synthetic-thread'
    row = json.loads(ns['CODEX_LEDGER_FILE'].read_bytes())
    assert row['chat_id'] == b.e.route.native_chat and row['thread'] == 'synthetic-thread'
    records = b.e._persistence_owners
    assert set(records) == {(b.frame['work_id'], 'status'), (b.frame['work_id'], 'ledger')}
    for record in records.values():
        assert record.body_returned and not record.file_settlement_verified and not record.parent_ready
        assert record.original.native._process is proc and record.original.action.request.work_id == b.frame['work_id']
        assert record.grant.work_scope == record.original.action.scope.values()
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 3
    assert len(calls) == 1


@pytest.mark.parametrize('shape', ['bad_json', 'duplicate', 'missing_sessions', 'list_sessions', 'extra_header', 'linked', 'stale_tmp', 'overflow', 'huge_integer', 'surrogate', 'depth'])
def test_unknown_status_or_temporary_file_holds_before_overwrite(native, monkeypatch, shape):
    b = native
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    path = ns['_STATUS_FILE']
    content = {'bad_json': b'{"sessions":', 'duplicate': b'{"sessions":{},"sessions":{}}',
               'missing_sessions': b'{"ts":"old"}', 'list_sessions': b'{"sessions":[]}',
                'extra_header': b'{"sessions":{},"synthetic-extra":true}',
               'overflow': b'{"sessions":{"other":{"tokens":1e400}}}',
               'huge_integer': b'{"sessions":{"other":{"tokens":'+b'1'*65+b'}}}',
               'surrogate': b'{"sessions":{"other":{"text":"\\ud800"}}}',
               'depth': b'{"sessions":{"other":'+b'['*65+b'0'+b']'*65+b'}}'}
    if shape in content:
        path.write_bytes(content[shape]); before = path.read_bytes()
    elif shape == 'linked':
        original = path.parent/'synthetic-original-status'; original.write_bytes(b'{"sessions":{}}')
        path.symlink_to(original); before = original.read_bytes()
    else:
        path.with_suffix('.tmp').write_bytes(b'SYNTHETIC_RETAINED_TEMP'); before = path.with_suffix('.tmp').read_bytes()
    result = asyncio.run(deliver(b))
    assert_unknown(b, proc, calls, logs, result)
    assert not ns['CODEX_LEDGER_FILE'].exists()
    if shape == 'stale_tmp': assert path.with_suffix('.tmp').read_bytes() == before and not path.exists()
    else: assert path.read_bytes() == before
    record = b.e._persistence_owners[(b.frame['work_id'], 'status')]
    assert record.unknown and not record.body_returned and record.grant is not None


@pytest.mark.parametrize('fault', ['broadcast', 'broadcast_interrupt', 'action_drift'])
def test_actual_replace_followed_by_callback_uncertainty_is_not_a_positive_tail(native, monkeypatch, fault):
    b = native
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    def callback(*_):
        if fault == 'action_drift':
            import fleet_managed_runtime as runtime
            runtime.CURRENT.set(None)
        elif fault == 'broadcast_interrupt': raise KeyboardInterrupt(PRIVATE)
        else: raise OSError(PRIVATE)
    ns['APP_FRONT'].broadcast_runtime = callback
    result = asyncio.run(deliver(b))
    assert_unknown(b, proc, calls, logs, result)
    assert ns['_STATUS_FILE'].exists() and not ns['CODEX_LEDGER_FILE'].exists()
    record = b.e._persistence_owners[(b.frame['work_id'], 'status')]
    assert record.unknown and not record.body_returned and not record.file_settlement_verified
    assert b.c.snapshot()['active_leases'] == 3


def test_existing_dispatch_can_enter_pending_file_tails_while_draining(native, monkeypatch):
    b = native
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    original = ns['_write_codex_status']
    def drain(*args, **kwargs):
        b.c.begin_drain(b.c.snapshot()['epoch'])
        return original(*args, **kwargs)
    ns['_write_codex_status'] = drain
    result = asyncio.run(deliver(b))
    assert isinstance(result[0], control.Refused)
    assert ns['_STATUS_FILE'].exists() and ns['CODEX_LEDGER_FILE'].exists()
    assert all(r.body_returned for r in b.e._persistence_owners.values())
    assert b.c.snapshot()['active_leases'] == 3 and b.c.snapshot()['unresolved'] == 1
    assert len(calls) == 1


@pytest.mark.parametrize('raw', [True, {}, 'synthetic-raw-writer'])
def test_raw_writer_cannot_select_a_file_helper_lease(native, monkeypatch, raw):
    b = native
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    with pytest.raises(ingress.Refused, match='managed_native_persistence_unknown'):
        ns['_write_codex_status'](b.e.route.native_chat, 'gpt-synthetic', 'xhigh', 'synthetic-thread', {}, 0, writer=raw)
    assert not ns['_STATUS_FILE'].exists() and not ns['CODEX_LEDGER_FILE'].exists()
    assert not b.e._persistence_owners and b.c.snapshot()['active_leases'] == 0


def test_default_helpers_keep_their_original_fail_soft_behavior_without_writer(native, monkeypatch):
    b = native
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    original = Path.write_text
    def fail(path, *args, **kwargs):
        if path == ns['_STATUS_FILE'].with_suffix('.tmp'): raise OSError(PRIVATE)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail)
    assert ns['_write_codex_status']('default-chat', 'gpt-synthetic', 'xhigh', 'legacy-thread', {}, 0) is None
    assert any(PRIVATE in row for row in logs)
    assert not b.e._persistence_owners and b.c.snapshot()['active_leases'] == 0

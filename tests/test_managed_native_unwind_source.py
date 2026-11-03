"""Actual selected exception unwind; fake wire/process and fresh stores only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from feishu_bridge import codex_archived_resume as archived
from feishu_bridge import fleet_managed_child as child
from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_target_control as control
from test_managed_child_source import native
from test_managed_entry_source import bound, deliver
import test_managed_native_notification_source as previous


PRIVATE = 'SYNTHETIC_PRIVATE_UNCERTAIN_WRITE'


def turn_fixture(b, monkeypatch):
    namespace = previous.source_namespace
    captured = {'replies': [], 'legacy': [], 'statuses': [], 'ledgers': [],
                'remember': [], 'archive': [], 'ns': None}

    def capture(*args):
        ns, events, logs, sessions = namespace(*args)
        captured['ns'] = ns
        ns['SENDER'].send_reply = lambda *a, **kw: captured['replies'].append((a, kw))
        ns['SENDER'].feishu.send = lambda *a, **kw: captured['legacy'].append((a, kw))
        ns['_write_codex_status'] = lambda *a, **kw: captured['statuses'].append(a)
        ns['_append_codex_ledger'] = lambda *a, **kw: captured['ledgers'].append(a)
        ns['_remember'] = lambda *a: captured['remember'].append(a)
        return ns, events, logs, sessions

    monkeypatch.setattr(previous, 'source_namespace', capture)
    proc, calls, events, logs, registrations = previous.turn_fixture(
        b, monkeypatch, [previous.notification()])
    return proc, calls, events, logs, registrations, captured


def assert_held(b, proc, calls, events, logs, result):
    assert isinstance(result[0], (ingress.Refused, control.Refused))
    assert b.c.snapshot()['state'] == 'UNKNOWN'
    assert b.c.snapshot()['unresolved'] == 1 and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._stderr_task is not None
    assert b.e._native_settlements == {} and len(calls) == 1
    assert PRIVATE not in str(events + logs)


def test_possible_final_reply_write_cannot_return_normally_or_issue_legacy_followup(native, monkeypatch):
    b = native
    del b.children  # Retention is the actual Entry, not an external fixture list.
    proc, calls, events, logs, registrations, captured = turn_fixture(b, monkeypatch)

    def uncertain(*a, **kw):
        captured['replies'].append((a, kw))  # Possible first effect, never replayed.
        raise OSError(PRIVATE)

    captured['ns']['SENDER'].send_reply = uncertain
    result = asyncio.run(deliver(b))
    assert_held(b, proc, calls, events, logs, result)
    assert len(captured['replies']) == 1
    assert not captured['legacy'] and not captured['ledgers'] and not registrations
    assert not captured['statuses'] and not captured['remember']
    assert b.e._native_owners[b.frame['work_id']]._terminal.status == 'completed'


def test_owned_archived_resume_refusal_cannot_write_legacy_cache_or_settle_parent(native, monkeypatch):
    b = native
    del b.children
    proc, calls, events, logs, registrations, captured = turn_fixture(b, monkeypatch)
    ns = captured['ns']
    binding = archived.Binding(b.e.route.work_chat, 'synthetic-thread', 'a' * 64, 'b' * 64)
    ns['_load_session'] = lambda *_: binding.sid
    ns['_codex_archived_binding'] = lambda *a, **kw: binding
    ns['_CODEX_ARCHIVED'] = SimpleNamespace(blocked=lambda _: False, record=captured['archive'].append)
    monkeypatch.setattr(child.storage_module.Scope, 'transcript', lambda *a: None)
    message = f'session {binding.sid} is archived. Run `codex unarchive {binding.sid}` to unarchive it first.'
    proc.stdout.rows[2:] = [(json.dumps({'id': 2, 'error': {
        'code': -32000, 'message': message}}) + '\n').encode()]
    result = asyncio.run(deliver(b))
    assert_held(b, proc, calls, events, logs, result)
    assert not captured['archive'] and not captured['replies'] and not captured['legacy']
    assert not captured['ledgers'] and not captured['statuses'] and not registrations
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._terminal is None
    assert message not in str(events + logs)
    methods = [json.loads(frame).get('method') for frame in proc.stdin.frames]
    assert methods == ['initialize', 'notifications/initialized', 'config/read', 'thread/resume']


@pytest.mark.parametrize('stage', ['reply', 'remember', 'map', 'status', 'ledger'])
@pytest.mark.parametrize('draining', [False, True])
def test_actual_post_turn_failure_preserves_exact_effect_prefix_and_refuses_redelivery(
        native, monkeypatch, stage, draining):
    b = native
    del b.children
    proc, calls, events, logs, registrations, captured = turn_fixture(b, monkeypatch)
    ns = captured['ns']
    effects = []

    def effect(kind):
        def perform(*a, **kw):
            effects.append(kind)  # Call observation only; no real output/store.
            if kind == stage:
                if draining:
                    b.c.begin_drain(b.c.snapshot()['epoch'])
                raise OSError(PRIVATE)
        return perform

    ns['SENDER'].send_reply = effect('reply')
    ns['_remember'] = effect('remember')
    monkeypatch.setattr(child.storage_module, 'record_session', effect('map'))
    ns['_write_codex_status'] = effect('status')
    ns['_append_codex_ledger'] = effect('ledger')
    result = asyncio.run(deliver(b))
    assert_held(b, proc, calls, events, logs, result)
    order = ['reply', 'remember', 'map', 'status', 'ledger']
    assert effects == order[:order.index(stage) + 1]
    assert not captured['legacy'] and not captured['archive']
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [previous.TEXT]
    assert not any(e.get('ev') == 'error' for e in events)
    original_owner = b.e._native_owners[b.frame['work_id']]
    original_frames = list(proc.stdin.frames)
    original_calls = list(effects)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert b.e._native_owners[b.frame['work_id']] is original_owner
    assert original_owner._process is proc and original_owner._terminal.status == 'completed'
    assert proc.stdin.frames == original_frames and effects == original_calls
    assert len(calls) == 1 and b.c.snapshot()['active_leases'] == 2


@pytest.mark.parametrize('replacement', ['missing', 'foreign'])
def test_exception_guard_cannot_borrow_a_changed_global_entry_or_release_original_child(
        native, monkeypatch, replacement):
    b = native
    del b.children
    proc, calls, events, logs, registrations, captured = turn_fixture(b, monkeypatch)
    ns = captured['ns']
    foreign_holds = []

    def uncertain(*a, **kw):
        ns['_MANAGED_ENTRY'] = (None if replacement == 'missing' else
            SimpleNamespace(runtime=SimpleNamespace(hold_unknown=lambda: foreign_holds.append(True))))
        raise OSError(PRIVATE)

    ns['SENDER'].send_reply = uncertain
    result = asyncio.run(deliver(b))
    assert_held(b, proc, calls, events, logs, result)
    assert not foreign_holds and not captured['legacy'] and not captured['ledgers']
    assert not captured['statuses'] and not registrations
    assert b.e._native_owners[b.frame['work_id']]._terminal.status == 'completed'

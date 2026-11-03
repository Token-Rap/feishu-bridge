"""Owned native terminal SOURCE fixtures; fake wire/process and fresh stores."""
import asyncio
import json

import pytest

import fleet_managed_child as child
import fleet_managed_ingress as ingress
import fleet_target_control as control
from test_managed_child_source import native
from test_managed_entry_source import bound, deliver
import test_managed_native_notification_source as previous


def turn_fixture(b, monkeypatch, *, ending='completed'):
    namespace = previous.source_namespace
    captured = {'replies': [], 'statuses': [], 'ledgers': []}

    def capture(*args):
        ns, events, logs, sessions = namespace(*args)
        ns['SENDER'].send_reply = lambda *a, **kw: captured['replies'].append((a, kw))
        ns['_write_codex_status'] = lambda *a, **kw: captured['statuses'].append(a)
        ns['_append_codex_ledger'] = lambda *a, **kw: captured['ledgers'].append(a)
        return ns, events, logs, sessions

    monkeypatch.setattr(previous, 'source_namespace', capture)
    proc, calls, events, logs, registrations = previous.turn_fixture(b, monkeypatch, [previous.notification()])
    if ending in {'eof', 'foreign'}:
        proc.stdout.rows.pop()  # Remove only the fixture's matching terminal.
        if ending == 'foreign':
            proc.stdout.rows.append((json.dumps({'method': 'turn/completed', 'params': {
                'threadId': 'synthetic-thread', 'turn': {'id': 'other-turn', 'status': 'completed'}}}) + '\n').encode())
    else:
        frame = json.loads(proc.stdout.rows[-1])
        frame['params']['turn']['status'] = ending
        proc.stdout.rows[-1] = (json.dumps(frame) + '\n').encode()
    return proc, calls, events, logs, registrations, captured


@pytest.mark.parametrize('ending', ['eof', 'foreign'])
def test_selected_eof_without_current_terminal_keeps_parent_unknown_and_owner(native, monkeypatch, ending):
    b = native
    del b.children  # Real Entry ownership, no external fixture retention.
    proc, calls, events, logs, registrations, captured = turn_fixture(b, monkeypatch, ending=ending)
    result = asyncio.run(deliver(b))
    assert isinstance(result[0], (ingress.Refused, control.Refused))
    assert b.c.snapshot()['state'] == 'UNKNOWN'
    assert b.c.snapshot()['unresolved'] == 1 and b.c.snapshot()['active_leases'] == 2
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert len(calls) == 1 and not captured['replies'] and not captured['statuses']
    assert not registrations
    assert [event['text'] for event in events if event.get('ev') == 'stream'] == [previous.TEXT]


@pytest.mark.parametrize('ending', ['completed', 'failed', 'interrupted'])
def test_actual_matching_wire_and_physical_settlement_stay_bound_after_turn_unwind(native, monkeypatch, ending):
    b = native
    del b.children
    proc, calls, _, _, _, captured = turn_fixture(b, monkeypatch, ending=ending)
    assert asyncio.run(deliver(b)) == [None]
    saved = b.e._native_settlements[b.frame['work_id']]
    assert type(saved) is child.NativeSettlement and saved.owner.entry is b.e
    assert saved.process is proc and saved.owner._process is proc
    assert saved.action is saved.owner.action and saved.action.request.work_id == b.frame['work_id']
    assert saved.scope is b.scope and saved.owner._scope is b.scope
    assert saved.terminal is saved.owner._terminal and saved.terminal.owner is saved.owner
    assert saved.terminal.action is saved.action
    assert (saved.terminal.thread, saved.terminal.turn, saved.terminal.status) == (
        'synthetic-thread', 'synthetic-turn', ending)
    assert saved.physical.pid == proc.pid and saved.physical.returncode == 0
    assert saved.physical.owned_group_absent and saved.physical.stdio_eof
    assert not saved.parent_ready and not saved.physical.native_ready
    assert not saved.physical.complete_descendant_exclusion
    assert b.e._native_owners == {} and b.c.snapshot()['active_leases'] == 0
    assert len(calls) == 1
    assert captured['replies'][0][1]['extra'].get('failed', False) is (ending != 'completed')
    assert captured['statuses'][0][-1] == (0 if ending == 'completed' else 1)


def test_matching_terminal_and_physical_facts_remain_available_during_parent_drain(native, monkeypatch):
    b = native
    proc, _, _, _, _, _ = turn_fixture(b, monkeypatch)
    read = proc.stdout.readline

    async def draining_read():
        if len(proc.stdout.rows) == 1:
            b.c.begin_drain(b.c.snapshot()['epoch'])
        return await read()

    proc.stdout.readline = draining_read
    assert asyncio.run(deliver(b)) == [None]
    assert b.c.snapshot()['state'] == 'DRAINING'
    assert b.e._native_settlements[b.frame['work_id']].terminal.status == 'completed'
    assert b.c.snapshot()['unresolved'] == 0


def test_matching_wire_cannot_manufacture_group_settlement_after_cleanup_refusal(native, monkeypatch):
    b = native
    del b.children
    proc, _, _, _, _, _ = turn_fixture(b, monkeypatch)

    def unknown_group(pid, sig):
        assert sig == 0
        raise PermissionError('SYNTHETIC_PRIVATE_GROUP')

    monkeypatch.setattr(child.os, 'killpg', unknown_group)
    assert isinstance(asyncio.run(deliver(b))[0], BaseException)
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._terminal.status == 'completed'
    assert b.e._native_settlements == {}
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2


def test_scope_drift_after_original_terminal_prevents_final_reply_and_settlement(native, monkeypatch):
    b = native
    proc, _, _, _, _, captured = turn_fixture(b, monkeypatch)
    observe = child.NativeChild.observe_terminal

    def drift(owner, *args):
        observe(owner, *args)
        monkeypatch.setattr(child.storage_module, 'select', lambda: None)

    monkeypatch.setattr(child.NativeChild, 'observe_terminal', drift)
    assert isinstance(asyncio.run(deliver(b))[0], BaseException)
    assert not captured['replies'] and not captured['statuses']
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert b.e._native_settlements == {} and b.c.snapshot()['active_leases'] == 2


def test_existing_settlement_slot_cannot_be_overwritten_or_release_child(native, monkeypatch):
    b = native
    turn_fixture(b, monkeypatch)
    observe = child.NativeChild.observe_terminal
    original = object()

    def conflict(owner, *args):
        observe(owner, *args)
        b.e._native_settlements[b.frame['work_id']] = original

    monkeypatch.setattr(child.NativeChild, 'observe_terminal', conflict)
    assert isinstance(asyncio.run(deliver(b))[0], BaseException)
    assert b.e._native_settlements[b.frame['work_id']] is original
    assert b.e._native_owners[b.frame['work_id']]._terminal.status == 'completed'
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2

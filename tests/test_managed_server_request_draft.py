"""Actual selected turn/source reader, fake Process and fresh synthetic store only."""
import asyncio
import inspect
import json
from pathlib import Path
import sys

import pytest

DRAFT = Path(__file__).resolve().parents[1]
PARENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DRAFT), str(PARENT / 'tests'), str(PARENT)]

from feishu_bridge import codex_approvals
from feishu_bridge import fleet_managed_child as child
from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_target_control as control
from feishu_bridge import fleet_managed_persistence  # Pin draft dependency before parent fixture path insertion.
import test_codex_questions as questions_fixture
import test_managed_native_notification_source as original
from test_managed_entry_source import bound, deliver
from test_managed_child_source import native


def require_bound_refusal(exc, reason='managed_native_server_request_unbound'):
    assert isinstance(exc, (ingress.Refused, control.Refused))
    reasons, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc)); reasons.append(str(exc)); exc = exc.__context__
    assert reason in reasons, 'owned reader refusal must precede the final coordinator refusal'


def request(method='synthetic/unknownRequest', rid=91):
    return {'id': rid, 'method': method, 'params': {
        'threadId': 'synthetic-thread', 'turnId': 'synthetic-turn',
        'itemId': 'synthetic-item', 'command': 'SYNTHETIC_PRIVATE_COMMAND',
        'availableDecisions': ['accept', 'decline']}}


def actual_turn(b, monkeypatch, events):
    del b.children  # The application Entry, never an external fixture list, owns the child.
    monkeypatch.setattr(questions_fixture, 'ROOT', DRAFT)
    proc, launches, output, logs, registrations = original.turn_fixture(b, monkeypatch, events)
    ns = inspect.getclosurevars(b.e._turn).nonlocals['ns']
    assert ns['_run_turn_codex'].__code__.co_filename == str(DRAFT / 'src/feishu_bridge/fleet_bridge.py')
    tasks = []
    spawn = codex_approvals.Approvals._spawn
    def observe(owner, coro):
        tasks.append((owner, coro))
        return spawn(owner, coro)
    monkeypatch.setattr(codex_approvals.Approvals, '_spawn', observe)
    return proc, launches, output, logs, registrations, ns, tasks


@pytest.mark.parametrize('method', ['synthetic/unknownRequest', 'item/commandExecution/requestApproval'])
def test_actual_selected_server_request_never_creates_legacy_response_task(native, monkeypatch, method):
    b = native
    proc, launches, output, logs, _, ns, tasks = actual_turn(b, monkeypatch, [request(method)])
    result = asyncio.run(deliver(b))
    assert tasks == [], 'actual selected native request reached an unleased legacy approval response task'
    require_bound_refusal(result[0])
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._stdin is proc.stdin and owner._stdout is proc.stdout
    assert owner._stderr is proc.stderr and owner._stderr_task is not None
    assert owner._read_failed and owner._stdout_task is not None
    assert not owner._terminal and not b.e._native_settlements
    assert len(launches) == 1
    assert len(proc.stdin.frames) == 5 and not any('result' in json.loads(f) or 'error' in json.loads(f) for f in proc.stdin.frames)
    assert not any(e.get('ev') in {'ask', 'ask_cancel', 'stream', 'reasoning', 'tool'} for e in output)
    assert all('SYNTHETIC_PRIVATE_COMMAND' not in str(e) for e in output + logs)
    assert not ns['CODEX_QUESTIONS'].cards
    reads, writes, snapshot = proc.stdout.reads, list(proc.stdin.frames), b.c.snapshot()
    with pytest.raises(BaseException):
        asyncio.run(deliver(b))
    assert len(launches) == 1 and tasks == [] and proc.stdout.reads == reads and proc.stdin.frames == writes
    assert b.c.snapshot() == snapshot and b.e._native_owners[b.frame['work_id']] is owner


@pytest.mark.parametrize('stage', [0, 1, 2, 3])
def test_actual_startup_server_request_holds_before_another_rpc_or_accumulation(native, monkeypatch, stage):
    b = native
    proc, launches, output, _, _, ns, tasks = actual_turn(b, monkeypatch, [])
    proc.stdout.rows.insert(stage, (json.dumps(request()) + '\n').encode())
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0])
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._read_failed and owner._process is proc and owner._stdout_task is not None
    assert proc.stdout.reads == stage + 1 and len(proc.stdin.frames) == [1, 3, 4, 5][stage]
    assert tasks == [] and not ns['CODEX_QUESTIONS'].cards and len(launches) == 1
    assert not b.e._native_settlements and not any(e.get('ev') == 'ask' for e in output)


@pytest.mark.parametrize('fault', ['string_id', 'foreign_scope', 'missing_params', 'mixed_result', 'canonical_notification'])
def test_owned_stream_does_not_route_any_request_variant_to_legacy_tasks(native, monkeypatch, fault):
    b = native; event = request()
    if fault == 'string_id': event['id'] = 'synthetic-id'
    if fault == 'foreign_scope': event['params']['threadId'] = 'other-thread'
    if fault == 'missing_params': del event['params']
    if fault == 'mixed_result': event['result'] = {}
    if fault == 'canonical_notification': event['method'] = 'item/agentMessage/delta'; event['params']['delta'] = 'SYNTHETIC_PRIVATE_COMMAND'
    proc, launches, output, logs, _, _, tasks = actual_turn(b, monkeypatch, [event])
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_notification_unknown' if fault == 'canonical_notification' else 'managed_native_server_request_unbound')
    assert tasks == []
    assert b.e._native_owners[b.frame['work_id']]._read_failed and len(launches) == 1
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert len(proc.stdin.frames) == 5 and proc.stdout.reads == 5
    assert all('SYNTHETIC_PRIVATE_COMMAND' not in str(e) for e in output + logs)
    assert not any(e.get('ev') in {'ask', 'stream'} for e in output)


def test_original_response_and_unknown_notification_still_reach_actual_selected_turn(native, monkeypatch):
    b = native
    proc, launches, output, _, _, _, tasks = actual_turn(b, monkeypatch,
        [{'method': 'synthetic/notification', 'params': {'private': 'SYNTHETIC_PRIVATE_COMMAND'}}, original.notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert len(launches) == 1 and tasks == [] and b.e._native_owners == {}
    assert [e['text'] for e in output if e.get('ev') == 'stream'] == [original.TEXT]
    assert not any(e.get('ev') == 'ask' for e in output)
    assert all('SYNTHETIC_PRIVATE_COMMAND' not in str(e) for e in output)
    assert b.e._native_settlements[b.frame['work_id']].parent_ready is False


@pytest.mark.parametrize('method', ['synthetic/unknownRequest', 'item/commandExecution/requestApproval'])
def test_default_legacy_negative_response_behavior_remains_unchanged(method):
    replies, output, logs = [], [], []
    async def run():
        async def send(frame): replies.append(frame)
        owner = codex_approvals.Approvals('synthetic-thread', 'synthetic-turn', 'group:fixture', send,
            output.append, logs.append, enabled=False)
        assert owner.feed(request(method)) is True and len(owner.tasks) == 1
        await asyncio.gather(*owner.tasks)
        await owner.close()
    asyncio.run(run())
    assert len(replies) == 1 and replies[0]['id'] == 91
    assert not any(e.get('ev') == 'ask' for e in output)

"""Original selected turn/review consumer, fake Process and fresh synthetic store only."""
import asyncio
from pathlib import Path
import sys

import pytest

DRAFT = Path(__file__).resolve().parents[1]
PARENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DRAFT), str(PARENT / 'tests'), str(PARENT)]

from feishu_bridge import fleet_managed_persistence
from feishu_bridge import codex_approvals
from test_managed_entry_source import bound, deliver
from test_managed_child_source import native
from test_managed_server_request_draft import actual_turn, require_bound_refusal

PRIVATE = 'SYNTHETIC_PRIVATE_AUTO_REVIEW_RATIONALE'
METHODS = ['item/autoApprovalReview/started', 'item/autoApprovalReview/completed']


def review_event(method, **params):
    return {'method': method, 'params': {'threadId': 'synthetic-thread',
        'turnId': 'synthetic-turn', 'reviewId': 'synthetic-review',
        'review': {'status': 'inProgress' if method.endswith('/started') else 'approved',
                   'rationale': PRIVATE}, **params}}


def review_turn(b, monkeypatch, events):
    proc, launches, output, logs, registrations, ns, tasks = actual_turn(b, monkeypatch, events)
    feed = codex_approvals.Approvals.feed
    observed = []
    def inspect(owner, event):
        answer = feed(owner, event)
        if event.get('method') in METHODS:
            observed.append((owner, event, set(owner.reviews)))
        return answer
    monkeypatch.setattr(codex_approvals.Approvals, 'feed', inspect)
    return proc, launches, output, logs, registrations, ns, tasks, observed


@pytest.mark.parametrize('method', METHODS)
def test_actual_selected_review_cannot_export_unbound_approval_notice(native, monkeypatch, method):
    b = native
    proc, launches, output, logs, registrations, ns, tasks, observed = review_turn(
        b, monkeypatch, [review_event(method)])
    result = asyncio.run(deliver(b))
    assert all(PRIVATE not in str(e) for e in output + logs), 'actual selected turn exported unbound review rationale'
    assert observed == [] and registrations == [] and tasks == []
    require_bound_refusal(result[0], 'managed_native_approval_review_lifecycle_unbound')
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._stdin is proc.stdin and owner._stdout is proc.stdout
    assert owner._stderr is proc.stderr and owner._stderr_task is not None
    assert owner._terminal is None and not b.e._native_settlements
    assert not any(e.get('ev') in {'ask', 'ask_cancel', 'stream', 'reasoning', 'tool'} for e in output)
    assert len(launches) == 1 and len(proc.stdin.frames) == 5 and proc.stdout.reads == 5
    assert not ns['CODEX_QUESTIONS'].cards
    before = b.c.snapshot(); reads = proc.stdout.reads; frames = list(proc.stdin.frames)
    with pytest.raises(BaseException): asyncio.run(deliver(b))
    assert b.c.snapshot() == before and len(launches) == 1 and tasks == []
    assert proc.stdout.reads == reads and proc.stdin.frames == frames and observed == []
    assert b.e._native_owners[b.frame['work_id']] is owner and registrations == []


@pytest.mark.parametrize('params', [
    {'review': None}, {'review': []}, {'review': {'status': True, 'rationale': PRIVATE}},
    {'review': {'status': 'approved', 'rationale': {'private': PRIVATE}}},
    {'reviewId': False}, {'review': {'status': 'unknown', 'rationale': PRIVATE}}])
def test_current_review_body_is_not_interpreted_coerced_or_exported(native, monkeypatch, params):
    b = native
    proc, calls, output, logs, registrations, ns, tasks, observed = review_turn(
        b, monkeypatch, [review_event(METHODS[1], **params)])
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_approval_review_lifecycle_unbound')
    assert all(PRIVATE not in str(e) for e in output + logs) and observed == []
    assert tasks == [] and registrations == [] and not ns['CODEX_QUESTIONS'].cards
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert b.e._native_owners[b.frame['work_id']]._process is proc and not b.e._native_settlements
    assert len(calls) == 1 and len(proc.stdin.frames) == 5 and proc.stdout.reads == 5


@pytest.mark.parametrize('fault', ['params', 'missing_thread', 'bool_thread', 'null_turn'])
def test_malformed_original_review_scope_holds_before_any_auxiliary_consumer(native, monkeypatch, fault):
    b = native; event = review_event(METHODS[0])
    if fault == 'params': event['params'] = None
    elif fault == 'missing_thread': del event['params']['threadId']
    elif fault == 'bool_thread': event['params']['threadId'] = True
    elif fault == 'null_turn': event['params']['turnId'] = None
    proc, calls, output, logs, _, _, tasks, observed = review_turn(b, monkeypatch, [event])
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_notification_unknown')
    assert tasks == [] and observed == [] and all(PRIVATE not in str(e) for e in output + logs)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert b.e._native_owners[b.frame['work_id']]._process is proc and not b.e._native_settlements
    assert len(calls) == 1 and proc.stdout.reads == 5


@pytest.mark.parametrize('method', METHODS)
@pytest.mark.parametrize('fault', ['foreign_thread', 'foreign_turn', 'replay'])
def test_foreign_or_replayed_review_cannot_publish_current_notice_or_mutate_approval_state(native, monkeypatch, method, fault):
    b = native; event = review_event(method)
    if fault == 'foreign_thread': event['params']['threadId'] = 'other-thread'
    elif fault == 'foreign_turn': event['params']['turnId'] = 'other-turn'
    else: event['replay'] = True
    proc, calls, output, logs, registrations, ns, tasks, observed = review_turn(b, monkeypatch, [event])
    assert asyncio.run(deliver(b)) == [None]
    assert all(PRIVATE not in str(e) for e in output + logs) and observed == [] and tasks == []
    assert not any(e.get('ev') in {'ask', 'ask_cancel', 'stream', 'reasoning', 'tool'} for e in output)
    assert len(calls) == 1 and len(registrations) == 1  # Existing synthetic post-turn mapping only.
    assert b.e._native_owners == {} and b.e._native_settlements[b.frame['work_id']].parent_ready is False
    assert not ns['CODEX_QUESTIONS'].cards


@pytest.mark.parametrize('method', METHODS)
def test_early_turn_review_reaches_same_guard_after_original_ids_are_bound(native, monkeypatch, method):
    import json
    b = native; event = review_event(method)
    proc, calls, output, logs, registrations, _, tasks, observed = review_turn(b, monkeypatch, [])
    proc.stdout.rows.insert(3, (json.dumps(event) + '\n').encode())
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_approval_review_lifecycle_unbound')
    assert tasks == [] and observed == [] and registrations == []
    assert all(PRIVATE not in str(e) for e in output + logs)
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._turn_binding == ('synthetic-thread', 'synthetic-turn') and owner._process is proc
    assert not owner._terminal and not b.e._native_settlements
    assert len(calls) == 1 and proc.stdout.reads == 5 and len(proc.stdin.frames) == 5


def test_unrelated_original_notification_and_plain_body_remain_in_existing_selected_path(native, monkeypatch):
    from test_managed_native_notification_source import notification, TEXT
    b = native
    proc, calls, output, logs, _, _, tasks, observed = review_turn(b, monkeypatch,
        [{'method': 'synthetic/notification', 'params': {'private': PRIVATE}}, notification()])
    assert asyncio.run(deliver(b)) == [None]
    assert tasks == [] and observed == [] and len(calls) == 1
    assert [e['text'] for e in output if e.get('ev') == 'stream'] == [TEXT]
    assert all(PRIVATE not in str(e) for e in output + logs) and b.e._native_owners == {}
    assert b.e._native_settlements[b.frame['work_id']].physical.native_ready is False
    assert b.e._native_settlements[b.frame['work_id']].parent_ready is False


@pytest.mark.parametrize('method', METHODS)
def test_default_legacy_review_notice_stays_unchanged_information_only(method):
    output, logs, replies = [], [], []
    async def run():
        async def send(frame): replies.append(frame)
        owner = codex_approvals.Approvals('synthetic-thread', 'synthetic-turn', 'group:fixture', send,
            output.append, logs.append, enabled=False)
        assert owner.feed(review_event(method)) is False
        assert len(owner.reviews) == 1 and owner.tasks == set() and owner.pending == {}
        await owner.close()
    asyncio.run(run())
    assert len(output) == 1 and output[0]['ev'] == 'notice' and PRIVATE in output[0]['text']
    assert replies == [] and len(logs) == 1

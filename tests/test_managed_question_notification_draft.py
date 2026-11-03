"""Actual selected turn, in-memory cards, fake Process and fresh coordinator only."""
import asyncio
from pathlib import Path
import sys

import pytest

DRAFT = Path(__file__).resolve().parents[1]
PARENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(DRAFT), str(PARENT / 'tests'), str(PARENT)]

from feishu_bridge.managed import fleet_managed_persistence  # Keep draft dependency pinned before old fixture path insertion.
from feishu_bridge.questions import codex_questions
from test_managed_entry_source import bound, deliver
from test_managed_child_source import native
from test_managed_server_request_draft import actual_turn, require_bound_refusal


QUESTIONS = [{'title': 'SYNTHETIC_SCOPE_AND_RISKS', 'options': ['Inspect ONE', 'Decline']}]


def question_event(questions=QUESTIONS, **params):
    return {'method': 'item/completed', 'params': {
        'threadId': 'synthetic-thread', 'turnId': 'synthetic-turn',
        'item': {'id': 'synthetic-question-item', 'type': 'agentMessage',
            'text': 'SYNTHETIC_QUESTION_BODY', 'questions': questions}, **params}}


def question_turn(b, monkeypatch, event, *, defaults=False, protocol=True):
    proc, launches, output, logs, registrations, ns, approval_tasks = actual_turn(b, monkeypatch, [event])
    sessions, observations, attempts = {}, [], []
    ns['_load_session'] = lambda key: sessions.get(key, '')
    ns['_display_chat_for'] = lambda _: 'group:fixture'
    ns['APP_FRONT'].resolve_work_display_ex = lambda display: (b.e.route.native_chat, display, '')
    monkeypatch.setattr(codex_questions, 'selected_protocol', lambda _: codex_questions.PROTOCOL if protocol else None)
    def register(fs, key, sid, scope, **kwargs):
        registrations.append((key, sid, scope)); sessions[key] = sid
    monkeypatch.setattr(ns['_codex_storage'], 'record_session', register)
    real_observe = ns['_observe_question_event']
    def observe(*args, **kwargs):
        observations.append((args, kwargs)); return real_observe(*args, **kwargs)
    ns['_observe_question_event'] = observe
    if defaults:
        class DefaultsProbe:
            def try_event(self, *args, **kwargs):
                attempts.append((args, kwargs)); return False
        ns['_QUESTION_DEFAULT_INTENTS'] = DefaultsProbe()
        ns['_QUESTION_DEFAULT_PRODUCER'] = object()
        ns['_QUESTION_DEFAULT_OWNER'] = object()
    return proc, launches, output, logs, registrations, ns, approval_tasks, sessions, observations, attempts


@pytest.mark.parametrize('defaults', [False, True])
def test_actual_selected_question_cannot_publish_unbound_card_or_register_early(native, monkeypatch, defaults):
    b = native
    proc, launches, output, logs, registrations, ns, tasks, sessions, observations, attempts = question_turn(
        b, monkeypatch, question_event(), defaults=defaults)
    result = asyncio.run(deliver(b))
    assert not any(e.get('ev') == 'ask' for e in output), 'actual selected turn published an unleased question card'
    assert registrations == [] and sessions == {} and observations == [] and attempts == []
    require_bound_refusal(result[0], 'managed_native_question_lifecycle_unbound')
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    owner = b.e._native_owners[b.frame['work_id']]
    assert owner._process is proc and owner._stdin is proc.stdin and owner._stdout is proc.stdout
    assert owner._stderr is proc.stderr and owner._stderr_task is not None
    assert owner._terminal is None and not b.e._native_settlements
    assert len(launches) == 1 and len(proc.stdin.frames) == 5 and proc.stdout.reads == 5
    assert tasks == [] and not ns['CODEX_QUESTIONS'].cards
    assert all('SYNTHETIC_SCOPE_AND_RISKS' not in str(e) and 'SYNTHETIC_QUESTION_BODY' not in str(e) for e in output + logs)
    reads, writes, snapshot = proc.stdout.reads, list(proc.stdin.frames), b.c.snapshot()
    with pytest.raises(BaseException): asyncio.run(deliver(b))
    assert len(launches) == 1 and proc.stdout.reads == reads and proc.stdin.frames == writes
    assert b.c.snapshot() == snapshot and b.e._native_owners[b.frame['work_id']] is owner
    assert registrations == [] and observations == [] and attempts == []


@pytest.mark.parametrize('questions', [[], False, {}, 'SYNTHETIC_PRIVATE_VALUE',
    [{'title': '', 'options': ['Inspect ONE', 'Decline']}],
    [{'title': 'SYNTHETIC_SCOPE_AND_RISKS', 'options': ['same', 'same']}],
    [{'title': 'SYNTHETIC_SCOPE_AND_RISKS'}]])
def test_actual_selected_question_payload_is_not_read_coerced_or_defaulted(native, monkeypatch, questions):
    b = native
    proc, launches, output, logs, registrations, ns, tasks, sessions, observations, attempts = question_turn(
        b, monkeypatch, question_event(questions), defaults=True, protocol=False)
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_question_lifecycle_unbound')
    assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
    assert len(launches) == 1 and len(proc.stdin.frames) == 5 and proc.stdout.reads == 5
    assert registrations == [] and sessions == {} and observations == [] and attempts == [] and tasks == []
    assert not ns['CODEX_QUESTIONS'].cards and not b.e._native_settlements
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert not any(e.get('ev') in {'ask', 'ask_cancel', 'stream'} for e in output)
    assert all('SYNTHETIC_SCOPE_AND_RISKS' not in str(e) and 'SYNTHETIC_PRIVATE_VALUE' not in str(e) for e in output + logs)


@pytest.mark.parametrize('fault', ['other_thread', 'other_turn', 'replay'])
def test_foreign_or_replayed_question_cannot_mutate_current_question_state(native, monkeypatch, fault):
    b = native; event = question_event()
    if fault == 'other_thread': event['params']['threadId'] = 'other-thread'
    if fault == 'other_turn': event['params']['turnId'] = 'other-turn'
    if fault == 'replay': event['replay'] = True
    _, launches, output, _, registrations, ns, tasks, _, observations, attempts = question_turn(
        b, monkeypatch, event, defaults=True)
    assert asyncio.run(deliver(b)) == [None]
    assert not any(e.get('ev') in {'ask', 'ask_cancel', 'stream'} for e in output)
    assert observations == [] and attempts == [] and tasks == [] and not ns['CODEX_QUESTIONS'].cards
    assert len(launches) == 1 and len(registrations) == 1  # Existing post-turn stub, no early question registration.
    assert b.c.snapshot()['state'] == 'OPEN' and b.c.snapshot()['active_leases'] == 0


@pytest.mark.parametrize('fault', ['missing', 'null'])
def test_original_plain_completion_remains_unchanged_with_question_protocol_selected(native, monkeypatch, fault):
    b = native; event = question_event(None)
    if fault == 'missing': del event['params']['item']['questions']
    _, launches, output, _, registrations, ns, tasks, _, _, attempts = question_turn(b, monkeypatch, event, defaults=True)
    assert asyncio.run(deliver(b)) == [None]
    assert [e['text'] for e in output if e.get('ev') == 'stream'] == ['SYNTHETIC_QUESTION_BODY']
    assert not any(e.get('ev') in {'ask', 'ask_cancel'} for e in output)
    assert attempts == [] and tasks == [] and not ns['CODEX_QUESTIONS'].cards
    assert len(launches) == 1 and len(registrations) == 1 and b.e._native_owners == {}
    assert b.e._native_settlements[b.frame['work_id']].parent_ready is False


def test_refused_managed_question_does_not_cancel_or_reemit_existing_other_card(native, monkeypatch):
    b = native
    proc, launches, output, _, _, ns, _, _, observations, attempts = question_turn(b, monkeypatch, question_event(), defaults=True)
    registry = ns['CODEX_QUESTIONS']; other = object()
    origin = codex_questions.Origin(codex_questions.Binding('synthetic-other-work', 'group:fixture',
        'other-thread', 'other-turn', 'synthetic-runtime', codex_questions.PROTOCOL, 'synthetic-connection'), other)
    event = question_event(threadId='other-thread', turnId='other-turn')
    card = registry.observe(origin, event, process=other, current=True)
    assert card is not None
    output.clear(); original = (card.state, card.answer, card.user, card.deadline, card.origin)
    result = asyncio.run(deliver(b))
    require_bound_refusal(result[0], 'managed_native_question_lifecycle_unbound')
    assert registry.cards == {card.ask_id: card}
    assert (card.state, card.answer, card.user, card.deadline, card.origin) == original
    assert observations == [] and attempts == [] and not any(e.get('ev') in {'ask', 'ask_cancel'} for e in output)
    assert len(launches) == 1 and b.e._native_owners[b.frame['work_id']]._process is proc


def test_default_legacy_selectable_card_keeps_full_scope_and_options():
    output, logs = [], []; process = object()
    registry = codex_questions.Registry(output.append, logs.append)
    origin = codex_questions.Origin(codex_questions.Binding('synthetic-work', 'group:fixture',
        'synthetic-thread', 'synthetic-turn', 'synthetic-runtime', codex_questions.PROTOCOL, 'synthetic-connection'), process)
    card = registry.observe(origin, question_event(), process=process, current=True)
    assert card is not None and card.state == 'pending' and card.answer == '' and card.user == ''
    assert output[0]['questions'][0]['question'] == QUESTIONS[0]['title']
    assert [o['label'] for o in output[0]['questions'][0]['options']] == QUESTIONS[0]['options']
    assert len(output) == 1 and output[0]['ev'] == 'ask'

"""Actual selected error consumption with synthetic owned wire/process only."""
import ast
import asyncio
from pathlib import Path

import pytest

from test_managed_child_source import native
from test_managed_entry_source import bound, deliver
import test_managed_native_notification_source as previous


FATAL = 'synthetic-fatal-error'
ABSENT = object()
INFORMATIONAL = 'skill descriptions were shortened to fit the skills context budget'


def error_fixture(b, monkeypatch, *, retry=False, alias=ABSENT, message=FATAL, error_extra=None):
    source_namespace = previous.source_namespace
    captured = {'replies': [], 'statuses': [], 'ledgers': [], 'notices': []}

    def capture(*args):
        ns, events, logs, sessions = source_namespace(*args)
        ns['SENDER'].send_reply = lambda *a, **kw: captured['replies'].append((a, kw))
        ns['SENDER'].send = lambda *a, **kw: captured['notices'].append((a, kw))
        ns['_write_codex_status'] = lambda *a, **kw: captured['statuses'].append(a)
        ns['_append_codex_ledger'] = lambda *a, **kw: captured['ledgers'].append(a)
        source = ast.parse((Path(__file__).parents[1] / 'src/feishu_bridge/fleet_bridge.py').read_text())
        nodes = [node for node in source.body if
                 isinstance(node, ast.FunctionDef) and node.name == '_codex_is_informational'
                 or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                    and target.id == '_CODEX_INFO_ERRORS' for target in node.targets)]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'fleet_bridge.py', 'exec'), ns)
        return ns, events, logs, sessions

    monkeypatch.setattr(previous, 'source_namespace', capture)
    params = {'threadId': 'synthetic-thread', 'turnId': 'synthetic-turn',
              'error': {'message': message, **(error_extra or {})}, 'willRetry': retry}
    if alias is not ABSENT:
        params['will_retry'] = alias
    proc, calls, events, logs, registrations = previous.turn_fixture(b, monkeypatch,
        [{'method': 'error', 'params': params}, previous.notification()])
    return captured, proc, calls, events, logs, registrations


def test_actual_selected_canonical_false_cannot_be_overridden_by_true_retry_alias(native, monkeypatch):
    captured, proc, calls, events, logs, _ = error_fixture(native, monkeypatch, alias=True)
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'].get('failed') is True
    assert captured['statuses'][0][-1] == 1
    assert captured['ledgers'][0][-1] == FATAL
    assert len(calls) == 1 and native.e._native_owners == {}


@pytest.mark.parametrize('alias', ['false', 1, {'synthetic': True}, [True], None])
def test_nonboolean_retry_alias_cannot_change_canonical_fatal_disposition(native, monkeypatch, alias):
    captured, _, _, _, _, _ = error_fixture(native, monkeypatch, alias=alias)
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'].get('failed') is True
    assert captured['replies'][0][1]['extra'].get('retryable') is True
    assert captured['statuses'][0][-1] == 1
    assert captured['ledgers'][0][-1] == FATAL


def test_canonical_false_alone_preserves_failed_flags_after_matching_completed(native, monkeypatch):
    captured, _, _, events, _, _ = error_fixture(native, monkeypatch)
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'].get('failed') is True
    assert captured['statuses'][0][-1] == 1
    assert captured['ledgers'][0][-1] == FATAL
    assert [e['text'] for e in events if e.get('ev') == 'stream'] == [previous.TEXT]


def test_canonical_retry_true_keeps_existing_successful_later_completion_policy(native, monkeypatch):
    captured, _, _, _, _, _ = error_fixture(native, monkeypatch, retry=True, alias=False)
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'] == {}
    assert captured['statuses'][0][-1] == 0
    assert captured['ledgers'][0][-1] == ''
    assert '正在自动重试' in captured['notices'][0][0][1]


@pytest.mark.parametrize('message,expected', [(FATAL, FATAL), ('', 'Codex reported an error')])
def test_original_native_message_cannot_be_replaced_by_informational_body_alias(native, monkeypatch, message, expected):
    captured, _, _, _, logs, _ = error_fixture(native, monkeypatch, message=message,
        error_extra={'text': INFORMATIONAL, 'content': INFORMATIONAL})
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'].get('failed') is True
    assert captured['statuses'][0][-1] == 1
    assert captured['ledgers'][0][-1] == expected
    assert expected in captured['notices'][0][0][1]
    assert not any('提示 (不计为失败)' in log for log in logs)


def test_original_informational_message_retains_existing_whitelist_policy(native, monkeypatch):
    captured, _, _, _, logs, _ = error_fixture(native, monkeypatch, message=INFORMATIONAL,
        error_extra={'text': FATAL})
    assert asyncio.run(deliver(native)) == [None]
    assert captured['replies'][0][1]['extra'] == {}
    assert captured['statuses'][0][-1] == 0
    assert any('提示 (不计为失败)' in log for log in logs)

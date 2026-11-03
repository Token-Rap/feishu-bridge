"""Actual same-helper IO-end evidence, not a supervisor/fence/settlement test."""
import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest
from tests.managed.test_managed_file_io_draft import (native, bound, deliver, actual_helpers, unknown,
                                      private_descriptor_cleanup)
from feishu_bridge.managed import fleet_managed_file_io as file_io
from feishu_bridge.managed import fleet_managed_persistence as persistence
from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.sessions import fleet_target_control as control


@pytest.fixture(autouse=True)
def synthetic_end_cleanup():
    yield
    # Only temporary fixture registry cleanup; no production retirement API.
    file_io._END_ORIGINALS.clear()
    file_io._END_ATTEMPTS.clear()


def completed_setup(native, monkeypatch):
    b = native
    del b.children
    proc, calls, events, logs, registrations, ns = actual_helpers(b, monkeypatch)
    return b, proc, calls, logs, ns


def test_original_both_complete_readbacks_pins_scope_and_closed_RAM_only(native, monkeypatch):
    b, proc, calls, logs, ns = completed_setup(native, monkeypatch)
    prefix = b'{"prior":"e\\u0301"}\n'
    ns['CODEX_LEDGER_FILE'].write_bytes(prefix)
    captured = []
    original = file_io.observed_io

    def verify(observed):
        result = original(observed)
        # No fstat/close of already reusable CLOSED descriptor integers.
        def forbidden(*_):
            raise AssertionError('CLOSED IO end attempted OS descriptor use')
        with monkeypatch.context() as patch:
            patch.setattr(file_io.os, 'fstat', forbidden)
            patch.setattr(file_io.os, 'close', forbidden)
            end = file_io.original_io_end(observed)
        assert end.result is result and end.original.observation is observed
        assert end.authority is persistence._REFUSAL_ORIGINALS[id(observed)]
        assert end.authority.action is end.original.helper_original.action
        assert all(file_io._STATES[k] == 'CLOSED' for k, pin in end.pins)
        assert end.readback_identity[5] == len(end.readback)
        assert end.readback == (end.written if end.original.kind == 'status'
                                else prefix + end.written)
        assert result.sha256 == hashlib.sha256(end.written).hexdigest()
        assert not result.file_settlement_verified and not result.parent_ready
        captured.append(end)
        return result

    monkeypatch.setattr(file_io, 'observed_io', verify)
    result = asyncio.run(deliver(b))
    unknown(b, proc, calls, logs, result, natural=True)
    assert len(captured) == 2 and b.c.snapshot()['active_leases'] == 3
    for end in captured:
        assert file_io._END_ORIGINALS[id(end.session)] is end
        assert file_io._END_ATTEMPTS[id(end.session)] == 'UNKNOWN'
        assert end.result is file_io._DONE[id(end.session)]
    before = dict(file_io._END_ORIGINALS)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert file_io._END_ORIGINALS == before and len(calls) == 1


@pytest.mark.parametrize('fault', ['equivalent_result', 'bool_count', 'wrong_digest', 'int_false_flag'])
def test_original_end_refuses_replaced_or_mutated_public_IO_result(native, monkeypatch, fault):
    b, proc, calls, logs, ns = completed_setup(native, monkeypatch)
    finish = file_io.Files._finish
    seen = []

    def tamper(session, *args):
        result = finish(session, *args)
        if not seen:
            end = file_io._END_ORIGINALS[id(session)]
            seen.append(end)
            if fault == 'equivalent_result':
                file_io._DONE[id(session)] = replace(result)
            elif fault == 'bool_count':
                object.__setattr__(result, 'bytes_written', True)
            elif fault == 'wrong_digest':
                object.__setattr__(result, 'sha256', '0' * 64)
            else:
                object.__setattr__(result, 'file_settlement_verified', 0)
        return result

    monkeypatch.setattr(file_io.Files, '_finish', tamper)
    result = asyncio.run(deliver(b))
    unknown(b, proc, calls, logs, result)
    assert len(seen) == 1 and file_io._END_ORIGINALS[id(seen[0].session)] is seen[0]
    assert file_io._END_ATTEMPTS[id(seen[0].session)] == 'UNKNOWN'
    assert not seen[0].original.observation.body_returned
    assert b.c.snapshot()['active_leases'] >= 3
    with seen[0].authority.control.tx() as db:
        seen[0].authority.control.verify_grant(db, seen[0].authority.parent, {'work_dispatch'})
        grant = persistence._REFUSAL_GRANTS[id(seen[0].original.observation)][0]
        seen[0].authority.control.verify_grant(db, grant, {'helper'})
    with pytest.raises(ingress.Refused, match='managed_native_persistence_unknown'):
        file_io.original_io_end(seen[0].original.observation)
    before = dict(file_io._END_ORIGINALS)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert file_io._END_ORIGINALS == before and len(calls) == 1


def test_closed_pin_replacement_cannot_redirect_end_to_foreign_fd(native, monkeypatch, tmp_path):
    b, proc, calls, logs, ns = completed_setup(native, monkeypatch)
    finish = file_io.Files._finish
    seen = []
    foreign = tmp_path / 'synthetic-unrelated'
    foreign.write_bytes(b'unchanged')
    fd = os.open(foreign, os.O_RDONLY)
    real_close = os.close
    closed = []

    def close(number):
        closed.append(number)
        return real_close(number)

    def tamper(session, *args):
        result = finish(session, *args)
        if not seen:
            end = file_io._END_ORIGINALS[id(session)]
            seen.append(end)
            k, pin = end.pins[0]
            file_io._READERS[k] = replace(pin, fd=fd)
        return result

    monkeypatch.setattr(file_io.Files, '_finish', tamper)
    monkeypatch.setattr(file_io.os, 'close', close)
    try:
        result = asyncio.run(deliver(b))
        unknown(b, proc, calls, logs, result)
        assert fd not in closed and os.fstat(fd).st_size == 9
        assert seen[0].pins[0][1] is not file_io._READERS[seen[0].pins[0][0]]
        assert file_io._END_ORIGINALS[id(seen[0].session)] is seen[0]
        with pytest.raises(ingress.Refused):
            file_io.original_io_end(seen[0].original.observation)
        assert fd not in closed
    finally:
        real_close(fd)  # Known unrelated synthetic fixture FD only.


@pytest.mark.parametrize('stage', ['final_sync', 'post_publish_guard'])
def test_final_stage_refusal_retains_original_inputs_and_no_stage_replay(native, monkeypatch, stage):
    b, proc, calls, logs, ns = completed_setup(native, monkeypatch)
    seen = []
    if stage == 'final_sync':
        sync = file_io.Files._sync
        def stop(session, owner):
            if id(session) in file_io._END_ORIGINALS and not seen:
                seen.append(file_io._END_ORIGINALS[id(session)])
                raise OSError('synthetic-end-sync')
            return sync(session, owner)
        monkeypatch.setattr(file_io.Files, '_sync', stop)
    else:
        guard = file_io.Files._guard
        def stop(session):
            end = file_io._END_ORIGINALS.get(id(session))
            if end is not None and end.result is not None and not seen:
                seen.append(end)
                raise ingress.Refused('synthetic-after-end-publication')
            return guard(session)
        monkeypatch.setattr(file_io.Files, '_guard', stop)
    result = asyncio.run(deliver(b))
    unknown(b, proc, calls, logs, result)
    assert len(seen) == 1 and file_io._END_ORIGINALS[id(seen[0].session)] is seen[0]
    assert file_io._END_ATTEMPTS[id(seen[0].session)] == 'UNKNOWN'
    assert seen[0].readback == seen[0].written
    assert not seen[0].original.observation.body_returned
    assert bool(seen[0].result is not None) == (stage == 'post_publish_guard')
    before = dict(file_io._END_ORIGINALS)
    with pytest.raises(control.Refused, match='admission_closed'):
        asyncio.run(deliver(b))
    assert file_io._END_ORIGINALS == before and len(calls) == 1


def test_unissued_observation_cannot_construct_or_select_original_end():
    for value in (None, True, {}, object()):
        with pytest.raises(ingress.Refused, match='managed_native_persistence_unknown'):
            file_io.original_io_end(value)


def test_end_is_exact_changed_draft_module_not_parent_fixture_module():
    assert Path(file_io.__file__).resolve() == Path(__file__).resolve().parents[2] / 'src/feishu_bridge/managed/fleet_managed_file_io.py'
    assert file_io.Files._finish.__code__.co_argcount == 4

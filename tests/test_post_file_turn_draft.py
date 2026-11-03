"""Entire selected turn unwind with control drift left in place; fake native."""
import asyncio
import os

import pytest
import test_codex_questions as question_fixture
import codex_archived_resume as archived
import test_managed_file_io_draft as old
import test_file_refusal_runtime_draft as prior
from test_managed_child_source import native
from test_managed_entry_source import bound, deliver
import fleet_managed_persistence as persistence
import fleet_managed_file_io as file_io
import fleet_target_control as control
import fleet_managed_ingress as ingress


@pytest.fixture(autouse=True)
def descriptors(monkeypatch):
    # New full-turn cases compile the submitted draft turn handlers themselves.
    # Old44 fixture definitions/assertions remain byte-equal and untouched.
    monkeypatch.setattr(question_fixture,'ROOT',old.DRAFT)
    fixture=old.private_descriptor_cleanup.__wrapped__();next(fixture)
    yield
    try:next(fixture)
    except StopIteration:pass


@pytest.mark.parametrize('kind',['status','ledger'])
def test_possible_write_control_replacement_stays_through_handler_finally_and_repeat(native,monkeypatch,tmp_path,kind):
    b=native;del b.children
    original_control=b.r.control  # Exact bound instance, distinct from b.c's view.
    f,_,_,hooks=prior.foreign_target(tmp_path,monkeypatch)
    # Even a foreign queue with the exact duplicate WORK/scope/payload cannot
    # make the already uncertain original Entry ACK or dispatch another time.
    req,scope=b.r.prepare(b.frame,work_chat=b.e.route.work_chat,display_chat='group:fixture',
        engine='codex',bot='shopping',gateway_connection=True)
    f.c.admit(req.work_id,scope,req.payload)
    foreign_before=f.c.snapshot();foreign_bytes=f.c.db.read_bytes()
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    real=os.write;seen=[]
    def write(fd,value):
        result=real(fd,value);k,p=old.current_pin(fd)
        if p.name==('status.tmp' if kind=='status' else 'codex_ledger.jsonl') and not seen:
            original=file_io._ORIGINALS[id(p.session)]
            seen.append((original,p.session,result))
            b.r.control=f.c
        return result
    monkeypatch.setattr(file_io.os,'write',write)
    try:
        result=asyncio.run(deliver(b))
        assert len(seen)==1 and seen[0][2]>0 and b.r.control is f.c
        # NO injected field restoration before the entire handler AND finally.
        prior.assert_foreign_unchanged(f,foreign_before,foreign_bytes,hooks)
        old.unknown(b,proc,calls,logs,result)
        assert str(result[0])=='managed_native_turn_unknown'
        original,session,count=seen[0]
        assert original.observation.unknown and not original.observation.body_returned
        assert persistence._REFUSAL_ORIGINALS[id(original.observation)].control is original_control
        assert file_io._ISSUED[id(original.observation)] is session
        assert b.e._persistence_owners[(req.work_id,kind)] is original.observation
        assert original.helper_original.native._process is proc
        assert id(session) not in file_io._DONE
        assert all(file_io._STATES[k] in {'CLOSED','UNKNOWN'} for k,p in file_io._READERS.items() if p.session is session)
        assert b.c.snapshot()['active_leases']>=3 and b.c.snapshot()['unresolved']==1
        assert not original.observation.parent_ready and not original.observation.file_settlement_verified
        frames=list(b.writer.frames);owners=dict(file_io._READERS);attempts=dict(persistence._REFUSAL_ATTEMPTS)
        files={p.name:p.read_bytes() for p in original.home.iterdir() if p.is_file()}
        with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
        assert b.r.control is f.c and b.writer.frames==frames and len(calls)==1
        assert file_io._READERS==owners and persistence._REFUSAL_ATTEMPTS==attempts
        assert files=={p.name:p.read_bytes() for p in original.home.iterdir() if p.is_file()}
        prior.assert_foreign_unchanged(f,foreign_before,foreign_bytes,hooks)
    finally:
        # Known synthetic substitution only, AFTER complete original+repeat
        # invocations, for teardown. No production restore/retry is supplied.
        b.r.control=original_control


@pytest.mark.parametrize('kind',['status','ledger'])
def test_archived_handler_after_possible_write_uses_same_original_entry(native,monkeypatch,tmp_path,kind):
    actual=persistence.Observation.refuse
    def archive_after_original_hold(record):
        try:actual(record)
        except ingress.Refused:
            binding=archived.Binding(native.e.route.work_chat,'synthetic-thread','a'*64,'b'*64)
            raise archived.ArchivedResumeError(binding,1,'synthetic-archive') from None
    monkeypatch.setattr(persistence.Observation,'refuse',archive_after_original_hold)
    called=[];held=persistence.hold_entry_original
    def entry_hold(entry):
        assert entry is native.e;called.append(entry);return held(entry)
    monkeypatch.setattr(persistence,'hold_entry_original',entry_hold)
    test_possible_write_control_replacement_stays_through_handler_finally_and_repeat(native,monkeypatch,tmp_path,kind)
    assert called==[native.e,native.e]  # Actual archive handler AND uncertain finally.


def test_new_turn_source_uses_actual_draft_handlers_before_execution(native,monkeypatch):
    proc,calls,events,logs,registrations,ns=old.actual_helpers(native,monkeypatch)
    assert question_fixture.ROOT==old.DRAFT
    assert 'hold_entry_original' in ns['_run_turn_codex'].__code__.co_names


def test_completed_turn_diagnostic_attempt_is_not_replayed_after_control_drift(native,monkeypatch,tmp_path):
    b=native
    test_possible_write_control_replacement_stays_through_handler_finally_and_repeat(b,monkeypatch,tmp_path,'status')
    record=b.e._persistence_owners[(b.frame['work_id'],'status')]
    authority=persistence._REFUSAL_ORIGINALS[id(record)]
    key=id(authority.action)
    assert persistence._TURN_REFUSAL_ATTEMPTS[key]=='ORIGINAL_TURN_DIAGNOSTIC_COMMITTED'
    before=b.c.snapshot();raw=b.c.db.read_bytes();attempts=dict(persistence._TURN_REFUSAL_ATTEMPTS)
    # Missing CURRENT cannot manufacture another diagnostic or fresh holder.
    assert persistence.hold_entry_original(b.e) is True
    assert b.r.hold_unknown() is True
    assert b.c.snapshot()==before and b.c.db.read_bytes()==raw
    assert persistence._TURN_REFUSAL_ATTEMPTS==attempts

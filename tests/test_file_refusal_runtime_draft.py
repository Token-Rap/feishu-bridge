"""New original-refusal cases: private synthetic stores, fake native only."""
import asyncio
import copy
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest
import test_managed_file_io_draft as old
from test_managed_entry_source import bound, deliver
from test_managed_child_source import native
import fleet_managed_persistence as persistence
import fleet_managed_file_io as file_io
import fleet_managed_ingress as ingress
import fleet_target_control as control


@pytest.fixture(autouse=True)
def descriptors(old=old):
    # Invoke only the existing fixture teardown, not its passing suite.
    fixture=old.private_descriptor_cleanup.__wrapped__()
    next(fixture)
    yield
    try:next(fixture)
    except StopIteration:pass


def foreign_target(tmp_path,monkeypatch):
    root=tmp_path/'foreign';root.mkdir(mode=0o700)
    b=bound.__wrapped__(root)
    before=b.c.snapshot();raw=b.c.db.read_bytes();hooks=[]
    original=b.r.hold_unknown
    def hold():
        hooks.append('foreign-runtime');return original()
    monkeypatch.setattr(b.r,'hold_unknown',hold)
    return b,before,raw,hooks


def replace_target(record,foreign,shape):
    original=record.original
    if shape=='foreign':
        native=copy.copy(original.native)
        native.runtime=foreign.r
        record.original=replace(original,native=native)
    elif shape=='null':record.original=None
    else:record.original=object()


def assert_foreign_unchanged(f,before,raw,hooks):
    assert f.c.snapshot()==before
    assert f.c.db.read_bytes()==raw
    assert not hooks


def assert_fixed_retained(b,proc,calls,record,session,refusals):
    assert record.unknown and not record.body_returned
    assert not record.file_settlement_verified and not record.parent_ready
    assert file_io._ISSUED[id(record)] is session
    original=file_io._ORIGINALS[id(session)]
    assert original.observation is record and original.helper_original.native._process is proc
    assert original.grant is record.grant
    assert session is record.files
    assert b.e._persistence_owners[(b.frame['work_id'],original.kind)] is record
    assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert id(session) not in file_io._DONE
    assert any(p.session is session for p in file_io._READERS.values())
    assert all(file_io._STATES[k] in {'CLOSED','UNKNOWN'} for k,p in file_io._READERS.items() if p.session is session)
    assert refusals and all(type(e) is ingress.Refused and str(e)=='managed_native_persistence_unknown' for e in refusals)
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['unresolved']==1
    assert b.c.snapshot()['active_leases']>=3
    writes=dict(file_io._READERS)
    with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
    assert writes==file_io._READERS and len(calls)==1
    with pytest.raises(ingress.Refused,match='managed_native_persistence_unknown'):
        session.status('{}') if original.kind=='status' else session.ledger('{}\n')
    assert writes==file_io._READERS and len(calls)==1


def watch_refusal(monkeypatch):
    failures=[];actual=persistence.Observation.refuse
    def refuse(record):
        try:return actual(record)
        except BaseException as e:failures.append(e);raise
    monkeypatch.setattr(persistence.Observation,'refuse',refuse)
    return failures


@pytest.mark.parametrize('kind',['status','ledger'])
@pytest.mark.parametrize('shape',['foreign','null','malformed'])
def test_possible_original_write_then_replaced_helper_cannot_touch_foreign_store(native,monkeypatch,tmp_path,kind,shape):
    b=native;del b.children
    f,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    refusals=watch_refusal(monkeypatch);real=os.write;seen=[]
    def write(fd,value):
        result=real(fd,value)
        k,p=old.current_pin(fd)
        if p.name==('status.tmp' if kind=='status' else 'codex_ledger.jsonl') and not seen:
            original=file_io._ORIGINALS[id(p.session)]
            seen.append((original.observation,p.session,result))
            replace_target(original.observation,f,shape)
        return result
    monkeypatch.setattr(file_io.os,'write',write)
    result=asyncio.run(deliver(b))
    assert len(seen)==1 and seen[0][2]>0
    assert_foreign_unchanged(f,before,raw,hooks)
    old.unknown(b,proc,calls,logs,result)
    assert_fixed_retained(b,proc,calls,seen[0][0],seen[0][1],refusals)


@pytest.mark.parametrize('shape',['foreign','null','malformed'])
def test_direct_helper_refusal_uses_original_authority_even_without_public_original(native,monkeypatch,tmp_path,shape):
    b=native;del b.children
    f,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    refusals=watch_refusal(monkeypatch);actual=persistence.begin;seen=[]
    def begin(*a,**kw):
        record=actual(*a,**kw)
        session=file_io.capture(record);session._root()
        seen.append((record,session))
        replace_target(record,f,shape)
        record.refuse()
    monkeypatch.setattr(persistence,'begin',begin)
    result=asyncio.run(deliver(b))
    assert len(seen)==1
    assert_foreign_unchanged(f,before,raw,hooks)
    old.unknown(b,proc,calls,logs,result)
    assert_fixed_retained(b,proc,calls,*seen[0],refusals)


@pytest.mark.parametrize('kind',['status','ledger'])
def test_original_native_runtime_replacement_after_write_never_selects_foreign_control(native,monkeypatch,tmp_path,kind):
    b=native;del b.children
    f,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    refusals=watch_refusal(monkeypatch);real=os.write;seen=[]
    def write(fd,value):
        result=real(fd,value);k,p=old.current_pin(fd)
        if p.name==('status.tmp' if kind=='status' else 'codex_ledger.jsonl') and not seen:
            original=file_io._ORIGINALS[id(p.session)]
            seen.append((original.observation,p.session))
            original.helper_original.native.runtime=f.r
        return result
    monkeypatch.setattr(file_io.os,'write',write)
    result=asyncio.run(deliver(b))
    assert len(seen)==1
    assert_foreign_unchanged(f,before,raw,hooks)
    old.unknown(b,proc,calls,logs,result)
    assert_fixed_retained(b,proc,calls,*seen[0],refusals)


def test_public_observation_constructor_cannot_enroll_diagnostic_or_file_authority(native,monkeypatch):
    b=native
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    actual=persistence.begin;seen=[]
    def begin(*a,**kw):
        record=actual(*a,**kw);raw=persistence.Observation(record.original)
        before=b.c.snapshot();bytes_before=b.c.db.read_bytes()
        with pytest.raises(ingress.Refused,match='managed_native_persistence_unknown'):raw.refuse()
        assert b.c.snapshot()==before and b.c.db.read_bytes()==bytes_before
        assert id(raw) not in persistence._REFUSAL_ORIGINALS
        assert id(raw) not in file_io._ISSUED
        with pytest.raises(ingress.Refused):file_io.capture(raw)
        assert not raw.body_returned and raw.unknown
        seen.append(raw);return record
    monkeypatch.setattr(persistence,'begin',begin)
    result=asyncio.run(deliver(b))
    old.unknown(b,proc,calls,logs,result,natural=True)
    assert len(seen)==2 and len(calls)==1


@pytest.mark.parametrize('drift',['runtime_control','physical_control','epoch'])
def test_direct_diagnostic_pin_or_epoch_drift_cannot_select_foreign_or_retry(native,monkeypatch,tmp_path,drift):
    b=native;del b.children
    f,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    proc,calls,events,logs,registrations,ns=old.actual_helpers(b,monkeypatch)
    actual=persistence.begin;seen=[]
    def begin(*a,**kw):
        record=actual(*a,**kw);session=file_io.capture(record);session._root()
        authority=persistence._REFUSAL_ORIGINALS[id(record)]
        fields=persistence._control_fields(authority.control)
        if drift=='runtime_control':b.r.control=f.c
        elif drift=='physical_control':
            for name in ('directory','target','selector','db','selector_file','dir_identity','db_identity','selector_identity','selector_bytes'):
                setattr(authority.control,name,getattr(f.c,name))
        else:b.c.begin_drain(b.c.snapshot()['epoch'])
        try:
            with pytest.raises(ingress.Refused,match='managed_native_persistence_unknown'):record.refuse()
            assert_foreign_unchanged(f,before,raw,hooks)
            assert record.unknown and not record.body_returned
            assert all(file_io._STATES[k]=='UNKNOWN' for k,p in file_io._READERS.items() if p.session is session)
            # Native cleanup must use the same enrolled original diagnostic,
            # and must not fall back to a foreign runtime even if it cannot hold.
            real_native=authority.helper.native
            old_runtime=real_native.runtime;real_native.runtime=f.r
            try:real_native._hold()
            finally:real_native.runtime=old_runtime
            assert_foreign_unchanged(f,before,raw,hooks)
        finally:
            # Explicitly known synthetic mutation only; no production reset or
            # unknown cleanup permission. Restore so fixture can observe its DB.
            b.r.control=authority.control
            for name,value in zip(('directory','target','selector','db','selector_file','dir_identity','db_identity','selector_identity','selector_bytes'),fields):
                setattr(authority.control,name,value)
        before_retry=b.c.snapshot();original_bytes=b.c.db.read_bytes()
        with pytest.raises(ingress.Refused,match='managed_native_persistence_unknown'):record.refuse()
        assert b.c.snapshot()==before_retry and b.c.db.read_bytes()==original_bytes
        state=persistence._REFUSAL_ATTEMPTS[id(record)]
        assert state==('ORIGINAL_DIAGNOSTIC_COMMITTED' if drift=='runtime_control' else 'UNKNOWN')
        seen.append(record)
        raise ingress.Refused('managed_native_persistence_unknown')
    monkeypatch.setattr(persistence,'begin',begin)
    result=asyncio.run(deliver(b));old.unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and b.c.snapshot()['active_leases']==3
    assert_foreign_unchanged(f,before,raw,hooks)
    with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
    assert len(calls)==1

"""Private SOURCE draft: real temp-file IO, original turn/Entry, fake native.

Not an official owner NORMAL gate or production/native-home fence observation.
"""
import ast
import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

PARENT = Path(__file__).resolve().parents[2]
DRAFT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DRAFT), str(PARENT/'tests'), str(PARENT)]

import pytest
from feishu_bridge.managed import fleet_managed_file_io as file_io
from feishu_bridge.managed import fleet_managed_persistence as draft_persistence
assert Path(draft_persistence.__file__).resolve() == DRAFT/'src/feishu_bridge/managed/fleet_managed_persistence.py'
from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.sessions import fleet_target_control as control
from tests.managed.test_managed_child_source import native
from tests.managed.test_managed_entry_source import bound, deliver
import tests.managed.test_managed_native_notification_source as previous

PRIVATE = 'SYNTHETIC_PRIVATE_FILE_IO_ERROR'
CLOSED_BY_FIXTURE = set()


@pytest.fixture(autouse=True)
def private_descriptor_cleanup():
    yield
    # Known synthetic owners only; no production UNKNOWN cleanup/reset authority.
    for k, pin in tuple(file_io._READERS.items()):
        if file_io._STATES[k] != 'CLOSED' and k not in CLOSED_BY_FIXTURE:
            try:
                s = os.fstat(pin.fd)
                if pin.identity is None or file_io.key(s)[:len(pin.identity)] == pin.identity:
                    os.close(pin.fd)
            except OSError:
                pass
    for name in ('_ORIGINALS','_ISSUED','_ROOTS','_READERS','_STATES','_PRIORS','_DONE'):
        getattr(file_io,name).clear()
    file_io._CLAIMS.clear();CLOSED_BY_FIXTURE.clear()


def actual_helpers(b, monkeypatch):
    assert Path(draft_persistence.__file__).resolve() == DRAFT/'src/feishu_bridge/managed/fleet_managed_persistence.py'
    assert Path(file_io.__file__).resolve() == DRAFT/'src/feishu_bridge/managed/fleet_managed_file_io.py'
    original = previous.source_namespace
    captured = {}
    def namespace(*args):
        ns, events, logs, sessions = original(*args)
        tree = ast.parse((DRAFT/'src/feishu_bridge/bridge/fleet_bridge.py').read_bytes())
        nodes = [n for n in tree.body if isinstance(n,ast.FunctionDef)
                 and n.name in {'_write_codex_status','_append_codex_ledger'}]
        ns.update(datetime=datetime,timezone=timezone,DEFAULT_MODEL='synthetic-default',
                  DEFAULT_EFFORT='xhigh',_STATUS_FILE=b.r.selection.target_home/'status.json',
                  CODEX_LEDGER_FILE=b.r.selection.target_home/'codex_ledger.jsonl')
        ns['APP_FRONT'].broadcast_runtime=lambda *_:None
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),
                     'private-draft-helper-source','exec'),ns)
        captured['ns']=ns
        return ns,events,logs,sessions
    monkeypatch.setattr(previous,'source_namespace',namespace)
    proc,calls,events,logs,registrations=previous.turn_fixture(b,monkeypatch,[previous.notification()])
    return proc,calls,events,logs,registrations,captured['ns']


def unknown(b,proc,calls,logs,result,*,natural=False):
    assert isinstance(result[0],(ingress.Refused,control.Refused))
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['unresolved']==1
    if natural:
        assert b.frame['work_id'] not in b.e._native_owners
        settlement=b.e._native_settlements[b.frame['work_id']]
        assert settlement.process is proc and settlement.terminal.status=='completed'
        assert not settlement.parent_ready and not settlement.physical.native_ready
    else:
        assert b.e._native_owners[b.frame['work_id']]._process is proc
    assert len(calls)==1 and PRIVATE not in str(logs)


def current_pin(fd):
    values=[(k,p) for k,p in file_io._READERS.items()
            if p.fd==fd and file_io._STATES[k] in {'OPEN','CLOSE_ATTEMPTED'}]
    assert len(values)==1
    return values[0]


def test_actual_both_helpers_sync_close_and_readback_preserve_rows_without_settlement(native,monkeypatch):
    b=native;del b.children
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    preserved={'engine':'claude','text':'e\u0301 中文\r\n','count':True}
    ns['_STATUS_FILE'].write_text(json.dumps({'sessions':{'other-native':preserved}}))
    prefix=b'{"synthetic_prior":"e\\u0301"}\n'
    ns['CODEX_LEDGER_FILE'].write_bytes(prefix)
    synced=[];closed=[];real_sync=os.fsync;real_close=os.close
    def sync(fd):
        k,p=current_pin(fd);synced.append((k,p.name));return real_sync(fd)
    def close(fd):
        values=[(k,p) for k,p in file_io._READERS.items()
                if p.fd==fd and file_io._STATES[k]=='CLOSE_ATTEMPTED']
        if values:closed.append(values[0][0])
        return real_close(fd)
    monkeypatch.setattr(file_io.os,'fsync',sync);monkeypatch.setattr(file_io.os,'close',close)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result,natural=True)
    assert json.loads(ns['_STATUS_FILE'].read_bytes())['sessions']['other-native']==preserved
    assert ns['CODEX_LEDGER_FILE'].read_bytes().startswith(prefix)
    assert len(synced)==4 and len(set(closed))==len(closed)
    assert all(x=='CLOSED' for x in file_io._STATES.values())
    assert b.c.snapshot()['active_leases']==3
    for record in b.e._persistence_owners.values():
        assert record.body_returned and not record.file_settlement_verified and not record.parent_ready
        observed=file_io._DONE[id(record.files)]
        assert observed.descriptors_closed and observed.readback_equal and observed.fsync_calls_returned
        assert not observed.all_writer_exclusion_verified and not observed.blocking_io_deadline_verified
        assert not observed.cross_restart_verified and not observed.file_settlement_verified and not observed.parent_ready
    before=dict(file_io._DONE)
    with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
    assert file_io._DONE==before and len(calls)==1


@pytest.mark.parametrize('stage',['status_write','status_sync','ledger_write','ledger_sync'])
def test_actual_write_or_sync_uncertainty_retains_original_fd_and_tail(native,monkeypatch,stage):
    b=native;del b.children
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    real_write=os.write;real_sync=os.fsync;seen=[]
    def inject(fd,*args):
        k,p=current_pin(fd)
        if p.name==('status.tmp' if stage.startswith('status') else 'codex_ledger.jsonl'):
            seen.append(k);raise OSError(PRIVATE)
        return (real_write if stage.endswith('write') else real_sync)(fd,*args)
    monkeypatch.setattr(file_io.os,'write' if stage.endswith('write') else 'fsync',inject)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and file_io._STATES[seen[0]]=='UNKNOWN'
    pin=file_io._READERS[seen[0]]
    assert pin.session is b.e._persistence_owners[(b.frame['work_id'], 'status' if stage.startswith('status') else 'ledger')].files
    assert os.fstat(pin.fd).st_ino==pin.identity[1]
    with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
    assert len(seen)==1


@pytest.mark.parametrize('name',['status.tmp','status.json','codex_ledger.jsonl'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt])
def test_actual_close_then_exception_never_recloses_reusable_number(native,monkeypatch,name,fault):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    real_close=os.close;seen=[]
    def close(fd):
        values=[(k,p) for k,p in file_io._READERS.items()
                if p.fd==fd and file_io._STATES[k]=='CLOSE_ATTEMPTED']
        if values and values[0][1].name==name and not seen:
            k,p=values[0];real_close(fd);CLOSED_BY_FIXTURE.add(k);seen.append(k);raise fault(PRIVATE)
        return real_close(fd)
    monkeypatch.setattr(file_io.os,'close',close)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and file_io._STATES[seen[0]]=='UNKNOWN'
    pin=file_io._READERS[seen[0]]
    with monkeypatch.context() as context:
        # Isolate the actual file-close boundary from coordinator's distinct
        # diagnostic descriptors; state/lease UNKNOWN is already established.
        def no_diagnostic(session):
            file_io.mark_unknown(session)
            raise ingress.Refused('managed_native_persistence_unknown')
        context.setattr(file_io.Files,'_fail',no_diagnostic)
        called=[];context.setattr(file_io.os,'close',lambda fd:called.append(fd))
        with pytest.raises((ingress.Refused,control.Refused)):pin.session._close(pin.owner)
        assert not called
    assert file_io._STATES[seen[0]]=='UNKNOWN' and id(pin.session) not in file_io._DONE


@pytest.mark.parametrize('fault',[ingress.Refused,KeyboardInterrupt])
@pytest.mark.parametrize('name',['status.tmp','status.json','codex_ledger.jsonl'])
def test_actual_caller_retains_pre_close_refusal_with_no_os_close(native,monkeypatch,fault,name):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    original=file_io.Files._close;seen=[]
    def refusal(session,owner):
        p=file_io._READERS[id(owner)]
        if p.name==name and not seen:
            seen.append(id(owner));raise fault(PRIVATE)
        return original(session,owner)
    monkeypatch.setattr(file_io.Files,'_close',refusal)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and file_io._STATES[seen[0]]=='UNKNOWN'
    pin=file_io._READERS[seen[0]]
    assert os.fstat(pin.fd).st_ino==pin.identity[1]
    before=dict(file_io._READERS)
    with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
    assert file_io._READERS==before


@pytest.mark.parametrize('fault',['content','same_bytes_new_inode'])
def test_actual_replace_readback_cannot_accept_changed_content_or_same_bytes_new_inode(native,monkeypatch,fault):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    original=os.replace;seen=[]
    def replace_file(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[:2]==('status.tmp','status.json'):
            raw=ns['_STATUS_FILE'].read_bytes();seen.append(raw)
            if fault=='content':ns['_STATUS_FILE'].write_bytes(b'X'+raw[1:])
            else:
                alternate=ns['_STATUS_FILE'].parent/'synthetic-other'
                alternate.write_bytes(raw);original(alternate,ns['_STATUS_FILE'])
        return result
    monkeypatch.setattr(file_io.os,'replace',replace_file)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and not ns['CODEX_LEDGER_FILE'].exists()
    assert not file_io._DONE


@pytest.mark.parametrize('fault',['action','original_path','files_pointer'])
def test_original_action_path_or_owner_drift_after_possible_write_retains_originals(native,monkeypatch,fault):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    original=os.write;seen=[]
    def write(fd,raw):
        result=original(fd,raw);k,p=current_pin(fd)
        if p.name=='status.tmp' and not seen:
            o=file_io._ORIGINALS[id(p.session)];seen.append(o)
            if fault=='action':
                from feishu_bridge.managed import fleet_managed_runtime as runtime
                runtime.CURRENT.set(None)
            elif fault=='original_path':o.observation.original=replace(o.helper_original,path=o.home/'synthetic-foreign')
            else:o.observation.files=object()
        return result
    monkeypatch.setattr(file_io.os,'write',write)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and not (seen[0].home/'synthetic-foreign').exists()
    assert not file_io._DONE and all(v in {'CLOSED','UNKNOWN'} for v in file_io._STATES.values())
    assert file_io._ORIGINALS and b.c.snapshot()['active_leases']>=3


def test_returned_root_fd_and_real_entry_owner_survive_first_fstat_refusal(native,monkeypatch):
    b=native;del b.children
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    original=os.fstat;seen=[]
    def stat_fd(fd):
        values=[(k,p) for k,p in file_io._READERS.items() if p.fd==fd
                and file_io._STATES[k]=='OPEN' and p.identity is None]
        if values and not seen:seen.append(values[0][0]);raise OSError(PRIVATE)
        return original(fd)
    monkeypatch.setattr(file_io.os,'fstat',stat_fd)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and file_io._STATES[seen[0]]=='UNKNOWN'
    pin=file_io._READERS[seen[0]]
    assert pin.identity is None and original(pin.fd).st_ino==b.r.selection.target_home.stat().st_ino
    assert pin.session is b.e._persistence_owners[(b.frame['work_id'],'status')].files


def test_closed_owner_is_memory_only_and_descriptor_pin_has_no_mutable_number(native,monkeypatch):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result,natural=True)
    k,p=next(iter(file_io._READERS.items()));assert file_io._STATES[k]=='CLOSED'
    with pytest.raises(FrozenInstanceError):p.fd=999
    with pytest.raises(AttributeError):p.owner.fd=999
    with monkeypatch.context() as context:
        def no_diagnostic(session):
            file_io.mark_unknown(session)
            raise ingress.Refused('managed_native_persistence_unknown')
        context.setattr(file_io.Files,'_fail',no_diagnostic)
        close=[];fstat=[]
        context.setattr(file_io.os,'close',lambda fd:close.append(fd))
        context.setattr(file_io.os,'fstat',lambda fd:fstat.append(fd))
        with pytest.raises((ingress.Refused,control.Refused)):p.session._close(p.owner)
        assert not close and not fstat
    assert file_io._STATES[k]=='CLOSED'


def test_incomplete_prior_ledger_is_retained_without_append_or_repair(native,monkeypatch):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    prior=b'SYNTHETIC_UNFINISHED_LINE';ns['CODEX_LEDGER_FILE'].write_bytes(prior)
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert ns['CODEX_LEDGER_FILE'].read_bytes()==prior
    assert (b.frame['work_id'],'ledger') in b.e._persistence_owners
    assert not b.e._persistence_owners[(b.frame['work_id'],'ledger')].body_returned


def test_unbound_raw_values_or_constructor_cannot_select_file_io(native,monkeypatch):
    b=native
    with pytest.raises(ingress.Refused):file_io.Files()
    for raw in (None,True,{},'synthetic'):
        with monkeypatch.context() as context:
            calls=[];context.setattr(file_io.os,'open',lambda *a,**k:calls.append(a))
            with pytest.raises(ingress.Refused):file_io.capture(raw)
            assert not calls
    assert not b.e._persistence_owners and b.c.snapshot()['active_leases']==0


def test_private_module_identity_precedes_inherited_fixture_path_mutation():
    assert sys.modules['feishu_bridge.managed.fleet_managed_persistence'] is draft_persistence
    assert Path(draft_persistence.__file__).resolve()==DRAFT/'src/feishu_bridge/managed/fleet_managed_persistence.py'
    assert Path(file_io.__file__).resolve()==DRAFT/'src/feishu_bridge/managed/fleet_managed_file_io.py'


@pytest.mark.parametrize('replacement',[None,object.__new__(file_io.Files)])
def test_after_io_callback_cannot_reset_original_file_session_and_recapture(native,monkeypatch,replacement):
    b=native
    proc,calls,events,logs,registrations,ns=actual_helpers(b,monkeypatch)
    seen=[]
    def callback(*_):
        record=b.e._persistence_owners[(b.frame['work_id'],'status')]
        original=record.files
        assert id(original) in file_io._DONE
        record.files=replacement
        with monkeypatch.context() as context:
            opened=[];context.setattr(file_io.os,'open',lambda *a,**k:opened.append(a))
            with pytest.raises(ingress.Refused):file_io.capture(record)
            assert not opened
        assert file_io._ISSUED[id(record)] is original
        seen.append(original)
    ns['APP_FRONT'].broadcast_runtime=callback
    result=asyncio.run(deliver(b));unknown(b,proc,calls,logs,result)
    assert len(seen)==1 and not ns['CODEX_LEDGER_FILE'].exists()
    assert file_io._ORIGINALS[id(seen[0])].observation is b.e._persistence_owners[(b.frame['work_id'],'status')]

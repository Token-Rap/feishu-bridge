"""Original pre-file diagnostic authority; actual selected turn, fake child only."""
import asyncio
import pytest
import fleet_managed_child as child
import fleet_managed_ingress as ingress
import fleet_target_control as control
from test_managed_entry_source import bound, deliver
from test_managed_child_source import native
import test_managed_server_request_draft as server
from test_file_refusal_runtime_draft import foreign_target, assert_foreign_unchanged


@pytest.mark.parametrize('stage', ['write', 'read'])
def test_actual_pre_file_control_drift_cannot_diagnose_foreign_store(native, monkeypatch, tmp_path, stage):
    b = native
    original_runtime, original_control = b.r, b.r.control
    foreign, before, raw, hooks = foreign_target(tmp_path, monkeypatch)
    proc, launches, output, logs, registrations, ns, tasks = server.actual_turn(b, monkeypatch, [])
    seen = []
    real = proc.stdin.drain if stage == 'write' else proc.stdout.readline
    async def changed():
        result = await real()
        if not seen:
            owner = b.e._native_owners[b.frame['work_id']]
            seen.append(owner)
            original_runtime.control = foreign.c
        return result
    monkeypatch.setattr(proc.stdin if stage == 'write' else proc.stdout,
                        'drain' if stage == 'write' else 'readline', changed)
    try:
        result = asyncio.run(deliver(b))
        assert len(seen) == 1 and original_runtime.control is foreign.c
        assert_foreign_unchanged(foreign, before, raw, hooks)
        assert isinstance(result[0], (ingress.Refused, control.Refused))
        assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
        assert b.c.snapshot()['unresolved'] == 1
        owner = seen[0]
        assert b.e._native_owners[b.frame['work_id']] is owner and owner._process is proc
        assert owner._stdin is proc.stdin and owner._stdout is proc.stdout and owner._stderr is proc.stderr
        assert not b.e._persistence_owners and not b.e._native_settlements
        assert tasks == [] and not ns['CODEX_QUESTIONS'].cards and len(launches) == 1
        assert not registrations and not any(e.get('ev') in {'reply','ask','ask_cancel'} for e in output)
        reads, frames, snapshot = proc.stdout.reads, list(proc.stdin.frames), b.c.snapshot()
        with pytest.raises(BaseException):
            asyncio.run(deliver(b))
        assert original_runtime.control is foreign.c
        assert_foreign_unchanged(foreign, before, raw, hooks)
        assert len(launches) == 1 and reads == proc.stdout.reads and frames == proc.stdin.frames
        assert b.c.snapshot() == snapshot and b.e._native_owners[b.frame['work_id']] is owner
    finally:
        # Only after the COMPLETE actual handler/finally/outer Runtime/repeat.
        # This known fixture substitution is not a production restore authority.
        original_runtime.control = original_control


@pytest.mark.parametrize('pointer', ['native_runtime', 'entry_runtime'])
@pytest.mark.parametrize('stage', ['write', 'read'])
def test_actual_pre_file_pointer_drift_kept_through_unwind_and_matching_foreign_delivery(native, monkeypatch, tmp_path, pointer, stage):
    b = native; original_runtime = b.r
    foreign, _, _, hooks = foreign_target(tmp_path, monkeypatch)
    req, scope = b.r.prepare(b.frame,work_chat=b.e.route.work_chat,display_chat='group:fixture',
        engine='codex',bot='shopping',gateway_connection=True)
    foreign.c.admit(req.work_id,scope,req.payload)
    before, raw = foreign.c.snapshot(), foreign.c.db.read_bytes()
    proc, launches, output, logs, registrations, ns, tasks = server.actual_turn(b,monkeypatch,[])
    real = proc.stdin.drain if stage == 'write' else proc.stdout.readline
    seen = []
    async def changed():
        result = await real()
        if not seen:
            owner = b.e._native_owners[b.frame['work_id']]; seen.append(owner)
            if pointer == 'native_runtime':owner.runtime = foreign.r
            else:b.e.runtime = foreign.r
        return result
    monkeypatch.setattr(proc.stdin if stage == 'write' else proc.stdout,
        'drain' if stage == 'write' else 'readline',changed)
    try:
        result = asyncio.run(deliver(b))
        assert len(seen) == 1
        owner = seen[0]
        assert (owner.runtime if pointer == 'native_runtime' else b.e.runtime) is foreign.r
        assert_foreign_unchanged(foreign,before,raw,hooks)
        assert isinstance(result[0],(ingress.Refused,control.Refused))
        assert b.c.snapshot()['state'] == 'UNKNOWN' and b.c.snapshot()['active_leases'] == 2
        assert owner._process is proc and not b.e._persistence_owners and not b.e._native_settlements
        assert len(launches) == 1 and tasks == [] and not registrations
        frames, reads, snapshot = list(proc.stdin.frames), proc.stdout.reads, b.c.snapshot()
        with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
        assert (owner.runtime if pointer == 'native_runtime' else b.e.runtime) is foreign.r
        assert len(launches) == 1 and frames == proc.stdin.frames and reads == proc.stdout.reads
        assert b.c.snapshot() == snapshot and b.e._native_owners[b.frame['work_id']] is owner
        assert_foreign_unchanged(foreign,before,raw,hooks)
    finally:
        b.e.runtime = original_runtime
        if seen:seen[0].runtime = original_runtime


@pytest.mark.parametrize('fault',['physical_control','scope','request','epoch','io'])
def test_original_qualification_or_io_failure_retains_unknown_without_foreign_fallback_or_retry(native,monkeypatch,tmp_path,fault):
    import fleet_managed_diagnostic as diagnostic
    import fleet_managed_runtime as runtime
    b = native; original_control = b.r.control
    foreign,before,raw,hooks = foreign_target(tmp_path,monkeypatch)
    proc,launches,output,logs,registrations,ns,tasks = server.actual_turn(b,monkeypatch,[])
    real = proc.stdin.drain
    saved = diagnostic.control_fields(original_control)
    actual_tx = control.Control.tx; io_calls = []; seen = []
    def failed_tx(c):
        if c is original_control and seen and id(seen[0].action) in diagnostic._ATTEMPTS:
            io_calls.append(c);raise control.Refused('database_unknown')
        return actual_tx(c)
    if fault == 'io':monkeypatch.setattr(control.Control,'tx',failed_tx)
    async def changed():
        result = await real()
        if not seen:
            owner = b.e._native_owners[b.frame['work_id']];seen.append(owner)
            original = diagnostic._NATIVES[id(owner)].dispatch
            if fault == 'physical_control':
                for name in ('directory','target','selector','db','selector_file','dir_identity','db_identity','selector_identity','selector_bytes'):
                    setattr(original_control,name,getattr(foreign.c,name))
            elif fault == 'scope':object.__setattr__(original.scope,'client','changed.synthetic')
            elif fault == 'request':object.__setattr__(original.action,'request',foreign.r.prepare(foreign.frame,
                work_chat=b.e.route.work_chat,display_chat='group:fixture',engine='codex',bot='shopping',gateway_connection=True)[0])
            elif fault == 'epoch':
                with actual_tx(original_control) as db:db.execute('UPDATE control SET epoch=epoch+2')
            owner._hold()
            raise OSError('SYNTHETIC_PRIVATE_REFUSAL')
        return result
    monkeypatch.setattr(proc.stdin,'drain',changed)
    try:
        result = asyncio.run(deliver(b));assert len(seen) == 1
        assert isinstance(result[0],(ingress.Refused,control.Refused))
        owner = seen[0];original = diagnostic._NATIVES[id(owner)].dispatch
        assert original.control is original_control and diagnostic._ATTEMPTS[id(original.action)] == 'UNKNOWN_NO_RETRY'
        assert b.e._native_owners[b.frame['work_id']] is owner and owner._process is proc
        assert not b.e._persistence_owners and not b.e._native_settlements and not registrations
        assert len(launches) == 1 and tasks == [] and (len(io_calls)==1 if fault=='io' else not io_calls)
        assert_foreign_unchanged(foreign,before,raw,hooks)
        frames,reads,attempts = list(proc.stdin.frames),proc.stdout.reads,dict(diagnostic._ATTEMPTS)
        owner._hold();assert b.r.hold_unknown() is False
        assert diagnostic.hold_entry(b.e) is True
        with pytest.raises(control.Refused,match='admission_closed'):asyncio.run(deliver(b))
        assert diagnostic._ATTEMPTS == attempts and len(launches) == 1
        assert frames == proc.stdin.frames and reads == proc.stdout.reads
        assert (len(io_calls)==1 if fault=='io' else not io_calls)
        assert_foreign_unchanged(foreign,before,raw,hooks)
    finally:
        # Restore only the explicitly injected synthetic fields after ALL calls.
        for name,value in zip(('directory','target','selector','db','selector_file','dir_identity','db_identity','selector_identity','selector_bytes'),saved):
            setattr(original_control,name,value)
        monkeypatch.setattr(control.Control,'tx',actual_tx)
        if seen:
            o=diagnostic._NATIVES[id(seen[0])].dispatch
            object.__setattr__(o.scope,'client',o.scope_values[4]);object.__setattr__(o.action,'request',o.request)
    snapshot=b.c.snapshot()
    assert snapshot['unresolved']==1 and snapshot['active_leases']==2


def test_archived_pre_file_handler_and_finally_use_original_dispatch(native,monkeypatch,tmp_path):
    import codex_archived_resume as archived
    import fleet_managed_persistence as persistence
    b=native;original_control=b.r.control
    foreign,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    proc,launches,output,logs,registrations,ns,tasks=server.actual_turn(b,monkeypatch,[])
    real=proc.stdin.drain;seen=[];held=[];hold=persistence.hold_entry_original
    def entry_hold(e):held.append(e);return hold(e)
    monkeypatch.setattr(persistence,'hold_entry_original',entry_hold)
    async def changed():
        result=await real()
        if not seen:
            seen.append(b.e._native_owners[b.frame['work_id']]);b.r.control=foreign.c
            binding=archived.Binding(b.e.route.work_chat,'synthetic-thread','a'*64,'b'*64)
            raise archived.ArchivedResumeError(binding,1,'synthetic-archive')
        return result
    monkeypatch.setattr(proc.stdin,'drain',changed)
    try:
        result=asyncio.run(deliver(b))
        assert isinstance(result[0],(ingress.Refused,control.Refused)) and str(result[0])=='managed_native_turn_unknown'
        assert held==[b.e,b.e]  # Actual archive handler AND uncertain finally.
        assert_foreign_unchanged(foreign,before,raw,hooks)
        assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2
        assert b.e._native_owners[b.frame['work_id']]._process is proc
        assert not b.e._persistence_owners and not registrations and len(launches)==1 and tasks==[]
    finally:b.r.control=original_control


def test_original_dispatch_diagnostic_is_not_replayed_after_current_unwinds(native,monkeypatch,tmp_path):
    import fleet_managed_diagnostic as diagnostic
    test_actual_pre_file_control_drift_cannot_diagnose_foreign_store(native,monkeypatch,tmp_path,'write')
    b=native;owner=b.e._native_owners[b.frame['work_id']]
    original=diagnostic._NATIVES[id(owner)].dispatch
    assert diagnostic._ATTEMPTS[id(original.action)]=='ORIGINAL_DIAGNOSTIC_COMMITTED'
    before,raw,attempts=b.c.snapshot(),b.c.db.read_bytes(),dict(diagnostic._ATTEMPTS)
    owner._hold();assert diagnostic.hold_entry(b.e) is True and b.r.hold_unknown() is True
    assert b.c.snapshot()==before and b.c.db.read_bytes()==raw and diagnostic._ATTEMPTS==attempts


def test_unregistered_native_or_constructed_original_has_no_diagnostic_authority(native,monkeypatch,tmp_path):
    import fleet_managed_diagnostic as diagnostic
    b=native;foreign,before,raw,hooks=foreign_target(tmp_path,monkeypatch)
    raw_native=object.__new__(child.NativeChild);raw_native.runtime=foreign.r
    raw_native.entry=b.e;raw_native.action=object()
    assert diagnostic.hold_native(raw_native) is False
    raw_native._hold()
    with pytest.raises(ingress.Refused,match='managed_native_diagnostic_unknown'):
        diagnostic.validate_native(raw_native)
    assert id(raw_native) not in diagnostic._NATIVES
    assert_foreign_unchanged(foreign,before,raw,hooks)

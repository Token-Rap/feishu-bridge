"""Owned synthetic stdout/public paths; no application/SDK/native process import."""
from __future__ import annotations
import ast
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_archived_resume as a
import test_codex_questions as public

SID = '22222222-3333-4444-8555-666666666666'
RUNTIME = 'a'*64

def binding():
    return a.Binding('SYNTHETIC-WORK',SID,RUNTIME,'b'*64)

def error(b=None):
    b=b or binding()
    return {'id':2,'error':{'code':-32600,'message':f'session {b.sid} is archived. Run `codex unarchive {b.sid}` to unarchive it first.'}}

@pytest.mark.parametrize('change',[
    lambda f:f.update(id=True), lambda f:f.update(id=3),
    lambda f:f.update(method='thread/resume'),lambda f:f.update(params={}),
    lambda f:f.update(result={}),lambda f:f['error'].update(code=True),
    lambda f:f['error'].update(data={}),lambda f:f['error'].update(message='archived'),
    lambda f:f['error'].update(message=f['error']['message']+' extra'),
    lambda f:f['error'].update(message=f['error']['message'].replace(SID,'OTHER')),
    lambda f:f['error'].update(extra='owner'),lambda f:f.update(jsonrpc=2),
])
def test_no_prose_type_extra_or_cross_request_authority(change):
    f=error();change(f)
    assert a.classify(f,2,binding()) is None


def test_exact_native_literal_and_unknown_windows_namespace(monkeypatch,tmp_path):
    b=binding();refused=a.classify(error(b),2,b)
    assert type(refused) is a.ArchivedResumeError and refused.binding==b
    assert a.namespace(str(tmp_path.resolve()),None) is not None
    monkeypatch.setattr(a.os,'name','nt')
    assert a.namespace(str(tmp_path),None) is None


def test_receipt_restart_idempotence_and_namespace_separation(tmp_path):
    store=a.Store(tmp_path.resolve());b=binding()
    assert not store.blocked(b) and not store.path.exists()
    store.record(a.classify(error(b),2,b));original=store.path.read_bytes()
    store.record(a.classify(error(b),2,b))
    assert store.path.read_bytes()==original and a.Store(tmp_path).blocked(b)
    for different in [a.Binding('other',SID,RUNTIME,'b'*64),a.Binding(b.work,'different',RUNTIME,b.namespace),a.Binding(b.work,SID,'c'*64,b.namespace),a.Binding(b.work,SID,RUNTIME,'c'*64)]:
        assert not a.Store(tmp_path).blocked(different)
    assert (store.path.stat().st_mode & 0o777)==0o600

@pytest.mark.parametrize('raw',[b'',b'{',b'{}\n',b'{"binding":{},"binding":{},"code":0,"message":"x"}\n'])
def test_partial_duplicate_empty_unknown_refuses_without_repair(tmp_path,raw):
    p=tmp_path/'codex_archived_resume.jsonl';p.write_bytes(raw);p.chmod(0o600)
    with pytest.raises(a.StateUnknown):a.Store(tmp_path).blocked(binding())
    assert p.read_bytes()==raw


def test_possible_write_is_latched_without_retry(monkeypatch,tmp_path):
    store=a.Store(tmp_path);real=a.os.write;calls=[]
    def partial(fd,raw):
        calls.append(raw);return real(fd,raw[:4])
    monkeypatch.setattr(a.os,'write',partial)
    with pytest.raises(a.StateUnknown):store.record(a.classify(error(),2,binding()))
    assert len(calls)==1 and store.blocked(binding())
    with pytest.raises(a.StateUnknown):a.Store(tmp_path).blocked(binding())
    assert store.path.read_bytes()==calls[0][:4]


def test_missing_root_and_linked_receipt_stay_unknown(tmp_path):
    store=a.Store(tmp_path/'absent')
    with pytest.raises(a.StateUnknown):store.record(a.classify(error(),2,binding()))
    assert store.blocked(binding()) and not store.path.exists()
    p=tmp_path/'codex_archived_resume.jsonl';p.write_text('{}\n');p.chmod(0o600)
    os.link(p,tmp_path/'linked')
    with pytest.raises(a.StateUnknown):a.Store(tmp_path).blocked(binding())
    assert p.read_text()=='{}\n'


def path_namespace(monkeypatch,tmp_path):
    image = tmp_path / 'synthetic-codex'
    image.write_bytes(b'synthetic reviewed image')
    monkeypatch.setattr(a, 'PIN_PATH', str(image.resolve()))
    monkeypatch.setattr(a, 'PIN_BYTES', image.stat().st_size)
    monkeypatch.setattr(a, 'PIN_SHA256', hashlib.sha256(image.read_bytes()).hexdigest())
    ns,events,logs,sessions=public.source_namespace(monkeypatch)
    fixture=ns['_storage_fixture']
    # The admitted dedicated Scope remains actual; the inherited personal home
    # is deliberately different and receives no writes from any child path.
    home=fixture.scope.desktop_home;home.mkdir(mode=0o700)
    receipt=(tmp_path/'owned-receipt');receipt.mkdir();receipt.chmod(0o700)
    ns.update(Path=Path,_codex_archived=a,_CODEX_ARCHIVED=a.Store(receipt.resolve()),CODEX_BIN=a.PIN_PATH)
    tree=ast.parse((public.ROOT/'fleet_bridge.py').read_text())
    defs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'_codex_archived_binding','_codex_archived_reply'}]
    exec(compile(ast.Module(defs,type_ignores=[]),'ACTUAL_ARCHIVE_HELPERS','exec'),ns)
    # Bind the mandatory real fixture Scope for old direct helper controls.
    # Production calls explicitly supply their same Scope and override this
    # partial keyword; no scope admission or image reader is stubbed.
    import functools
    ns['_codex_archived_binding']=functools.partial(ns['_codex_archived_binding'],storage=fixture.scope)
    sessions['work::codex']=SID
    rows={'work::codex':{'active':SID,'sessions':{SID:{'fleet_codex_namespace':
        {'home':str(fixture.scope.home),'namespace':fixture.scope.namespace}}}}}
    fixture.fs.SESSIONS_FILE.write_text(json.dumps(rows))
    from codex_storage_fixture import header
    header(fixture,SID)
    replies=[];ns['SENDER'].send_reply=lambda *args,**kw:replies.append((args,kw))
    return ns,events,logs,sessions,replies,home

class Refused(public.SyntheticProcess):
    def __init__(self,frame):super().__init__([],thread=SID);self.frame=frame
    def write(self,raw):
        msg=json.loads(raw)
        if msg.get('method')=='thread/resume':
            self.writes.append(msg);self.put(self.frame)
        else:super().write(raw)


def test_actual_owned_resume_first_refusal_then_zero_dispatch_and_restart(monkeypatch,tmp_path):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    original=copy.deepcopy(sessions);proc=Refused(error());launches=[]
    async def launch(*args,**kwargs):launches.append((args,kwargs));return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC first input'))
    assert len(launches)==1
    scope=ns['_storage_fixture'].scope
    assert launches[0][1]['env']['CODEX_HOME']==str(scope.home)
    assert launches[0][1]['env']['CODEX_SQLITE_HOME']==str(scope.home)
    assert str(home)!=str(scope.home) and os.environ['CODEX_HOME']==str(home)
    assert launches[0][0][0]==a.PIN_PATH and launches[0][0][1:4]==('app-server','--listen','stdio://')
    assert next(m for m in proc.writes if m.get('method')=='thread/resume')['params']['config']==scope.config()
    assert [m['method'] for m in proc.writes if m.get('method') in {'thread/resume','thread/start','turn/start'}]==['thread/resume']
    assert len(replies)==1 and replies[0][1]['extra']=={'failed':True,'retryable':False}
    assert not any(ev.get('ev')=='error' for ev in events)
    assert sessions==original
    state=ns['_CODEX_ARCHIVED'];raw=state.path.read_bytes()
    ns['_CODEX_ARCHIVED']=a.Store(state.path.parent)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC subsequent input'))
    assert len(launches)==1 and len(replies)==2 and sessions==original
    assert state.path.read_bytes()==raw
    assert ns['CODEX_PROCS']=={} and ns['CODEX_TURNS']=={}
    # Native home/config/model context is never changed by the handler.
    assert list(home.iterdir())==[]

@pytest.mark.parametrize('change',['notification','wrong_sid','wrong_code_type','extra_body'])
def test_actual_unknown_rpc_keeps_legacy_error_without_latch(monkeypatch,tmp_path,change):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    frame=error()
    if change=='notification':frame.update(method='error')
    if change=='wrong_sid':frame['error']['message']=frame['error']['message'].replace(SID,'different')
    if change=='wrong_code_type':frame['error']['code']=False
    if change=='extra_body':frame['error']['body']='untrusted'
    proc=Refused(frame)
    # A wrong/notification startup frame is followed by a genuine non-archived error.
    if change=='notification':
        original_write=proc.write
        def notification_then_error(raw):
            original_write(raw)
            if json.loads(raw).get('method')=='thread/resume':
                proc.put({'id':2,'error':{'code':-1,'message':'ordinary refusal'}})
        proc.write=notification_then_error
    async def launch(*args,**kwargs):
        return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC input'))
    assert not ns['_CODEX_ARCHIVED'].path.exists() and not replies
    assert any(ev.get('ev')=='error' for ev in events)
    assert sessions['work::codex']==SID


def test_actual_binding_drift_is_not_attributed(monkeypatch,tmp_path):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    proc=Refused(error())
    original_write=proc.write
    def drift(raw):
        if json.loads(raw).get('method')=='thread/resume':sessions['work::codex']='different'
        original_write(raw)
    proc.write=drift
    async def launch(*args,**kwargs):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC input'))
    assert not ns['_CODEX_ARCHIVED'].path.exists() and not replies
    assert any(ev.get('ev')=='error' for ev in events)
    assert sessions['work::codex']=='different'


@pytest.mark.parametrize('kind',['unpinned','different','replacement','same_path_drift','symlink_drift','size','bare'])
def test_fresh_physical_image_pin_refuses_drift(monkeypatch,tmp_path,kind):
    # Explicit SYNTHETIC reviewed-image fixture; actual production pin unchanged.
    import hashlib
    image=tmp_path/'SYNTHETIC-codex';raw=b'SYNTHETIC reviewed literal image'
    image.write_bytes(raw);image.chmod(0o700)
    monkeypatch.setattr(a,'PIN_PATH',str(image.resolve()))
    monkeypatch.setattr(a,'PIN_SHA256',hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(a,'PIN_BYTES',len(raw))
    assert a.physical_pin(str(image)) is not None
    selector=str(image)
    if kind=='unpinned':selector=str(tmp_path/'absent')
    if kind=='different':
        other=tmp_path/'same-bytes-other-path';other.write_bytes(raw);other.chmod(0o700);selector=str(other)
    if kind=='replacement':
        fresh=tmp_path/'replacement';fresh.write_bytes(b'X'*len(raw));fresh.chmod(0o700);fresh.replace(image)
    if kind=='same_path_drift':image.write_bytes(b'X'*len(raw))
    if kind=='symlink_drift':
        link=tmp_path/'selector';link.symlink_to(image);assert a.physical_pin(str(link)) is not None
        link.unlink();link.symlink_to(tmp_path/'unknown');selector=str(link)
    if kind=='size':image.write_bytes(raw+b'extra')
    if kind=='bare':selector='codex'
    assert a.physical_pin(selector) is None


def test_actual_binding_requires_fresh_physical_pin(monkeypatch,tmp_path):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    runtime=ns['_question_runtime_binding']()
    assert ns['_codex_archived_binding']('work',SID,runtime) is not None
    ns['CODEX_BIN']='/SYNTHETIC/unpinned'
    changed=ns['_question_runtime_binding']()
    assert ns['_codex_archived_binding']('work',SID,changed) is None
    assert not ns['_CODEX_ARCHIVED'].path.exists()


@pytest.mark.parametrize('mutation',['sqlite','provider','shell'])
def test_integrated_effective_refusal_has_no_resume_or_archive_authority(monkeypatch,tmp_path,mutation):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    proc=Refused(error());original=copy.deepcopy(sessions)
    oldwrite=proc.write
    def write(raw):
        msg=json.loads(raw)
        if msg.get('method')=='config/read':
            proc.writes.append(msg)
            cfg=copy.deepcopy(ns['_storage_fixture'].config)
            if mutation=='sqlite':cfg['sqlite_home']=str(home)
            if mutation=='provider':cfg['model_providers']={'openai':{'requires_openai_auth':False,'base_url':'https://SYNTHETIC.invalid'}}
            if mutation=='shell':cfg['shell_environment_policy']['set']['CODEX_HOME']=str(home)
            proc.put({'id':msg['id'],'result':{'config':cfg}})
        else:oldwrite(raw)
    proc.write=write
    async def launch(*args,**kwargs):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC effective input'))
    assert not any(m.get('method') in {'thread/resume','thread/start','turn/start'} for m in proc.writes)
    assert not ns['_CODEX_ARCHIVED'].path.exists() and not replies
    assert sessions==original and list(home.iterdir())==[]
    assert any(ev.get('ev')=='error' for ev in events)


@pytest.mark.parametrize('mutation',['namespace','home','type','unbound_map','parent_only'])
def test_integrated_binding_refuses_foreign_scope_or_old_unbound_sid(monkeypatch,tmp_path,mutation):
    from dataclasses import replace
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    scope=ns['_storage_fixture'].scope;runtime=ns['_question_runtime_binding']()
    actual=ns['_codex_archived_binding']('work',SID,runtime,storage=scope)
    assert actual is not None
    wrong=scope
    if mutation=='namespace':wrong=replace(scope,namespace='c'*32)
    if mutation=='home':wrong=replace(scope,home=home)
    if mutation=='type':wrong=str(scope.home)
    if mutation=='unbound_map':
        rows=json.loads(ns['_fs'].SESSIONS_FILE.read_text());rows['work::codex']['sessions'][SID].pop('fleet_codex_namespace');ns['_fs'].SESSIONS_FILE.write_text(json.dumps(rows))
    if mutation=='parent_only':
        # A different parent reference cannot replace an admitted child Scope.
        monkeypatch.setenv('CODEX_HOME',str(tmp_path/'UNKNOWN-parent'))
    assert ns['_codex_archived_binding']('work',SID,ns['_question_runtime_binding'](),storage=wrong) is None
    assert not ns['_CODEX_ARCHIVED'].path.exists() and sessions['work::codex']==SID


@pytest.mark.parametrize('mutation',['marker','process','selector','mapping'])
def test_integrated_owned_response_drift_cannot_mint_or_transfer_receipt(monkeypatch,tmp_path,mutation):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    proc=Refused(error());oldwrite=proc.write
    def write(raw):
        if json.loads(raw).get('method')=='thread/resume':
            if mutation=='marker':
                import fleet_codex_storage as storage
                p=ns['_storage_fixture'].scope.home/storage.MARKER
                data=json.loads(p.read_text());data['namespace']='d'*32;p.write_text(json.dumps(data))
            if mutation=='process':ns['CODEX_PROCS']['work']=object()
            if mutation=='selector':ns['CODEX_BIN']='/SYNTHETIC/unpinned'
            if mutation=='mapping':
                p=ns['_fs'].SESSIONS_FILE;rows=json.loads(p.read_text());rows['work::codex']['sessions'][SID]['fleet_codex_namespace']['namespace']='d'*32;p.write_text(json.dumps(rows))
        oldwrite(raw)
    proc.write=write
    async def launch(*args,**kwargs):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC current response'))
    assert not ns['_CODEX_ARCHIVED'].path.exists() and not replies
    assert sessions['work::codex']==SID and list(home.iterdir())==[]
    assert not any(m.get('method')=='turn/start' for m in proc.writes)
    assert any(ev.get('ev')=='error' for ev in events)


def test_integrated_new_start_preserves_old_rows_and_legacy_has_no_defaults(monkeypatch,tmp_path):
    ns,events,logs,sessions,replies,home=path_namespace(monkeypatch,tmp_path)
    scope=ns['_storage_fixture'].scope;p=ns['_fs'].SESSIONS_FILE
    rows=json.loads(p.read_text());oldrow=copy.deepcopy(rows['work::codex']['sessions'][SID]);rows['work::codex']['active']=None;p.write_text(json.dumps(rows));sessions.clear()
    ev=public.completed();ev['params']['threadId']='SYNTHETIC-NEW'
    done={'method':'turn/completed','params':{'threadId':'SYNTHETIC-NEW','turn':{'id':'turn','status':'completed'}}}
    proc=public.SyntheticProcess([ev,done],thread='SYNTHETIC-NEW');launches=[]
    async def launch(*args,**kwargs):launches.append((args,kwargs));return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC explicitly new input'))
    methods=[m.get('method') for m in proc.writes]
    assert methods.index('config/read')<methods.index('thread/start')<methods.index('turn/start') and 'thread/resume' not in methods
    assert launches[0][1]['env']['CODEX_HOME']==str(scope.home) and list(home.iterdir())==[]
    after=json.loads(p.read_text());assert after['work::codex']['sessions'][SID]==oldrow
    assert after['work::codex']['active']=='SYNTHETIC-NEW'
    assert after['work::codex']['sessions']['SYNTHETIC-NEW']['fleet_codex_namespace']=={'home':str(scope.home),'namespace':scope.namespace}
    asks=[e for e in events if e.get('ev')=='ask']
    assert len(asks)==1 and 'question_ui_defaults' not in asks[0]
    assert asks[0]['questions'][0]['question']=='Full scope and risks'
    assert not ns['_CODEX_ARCHIVED'].path.exists()

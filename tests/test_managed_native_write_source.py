"""Synthetic same-parent child writes; no native product or existing home."""
import asyncio
import inspect
import ast
import json
import gc
import weakref
from pathlib import Path
from types import SimpleNamespace
import pytest
from feishu_bridge import fleet_managed_child as child
from feishu_bridge import fleet_managed_runtime as runtime
from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_target_control as control
from test_managed_entry_source import bound,deliver
from test_managed_child_source import native,Process,fake_spawn
from test_codex_questions import source_namespace as original_source_namespace

def source_namespace(monkeypatch,b):
    ns,events,logs,sessions=original_source_namespace(monkeypatch)
    # The original question fixture installs its own synthetic Scope. Rebind
    # only this new test to the same owned child's synthetic Scope.
    monkeypatch.setattr(child.storage_module,'select',lambda:b.scope)
    return ns,events,logs,sessions

class Sink:
    def __init__(self):self.frames=[];self.drains=0
    def write(self,raw):self.frames.append(raw)
    async def drain(self):self.drains+=1
    def close(self):pass

def test_actual_rpc_helper_unknown_child_does_not_write_or_report_delivery(native,monkeypatch):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        b.c.mark_unknown(b.c.snapshot()['epoch'])
        kw={'writer':c} if 'writer' in inspect.signature(ns['_codex_rpc_send']).parameters else {}
        try:await ns['_codex_rpc_send'](p,{'id':1,'method':'initialize','params':{}},**kw)
        except (ingress.Refused,control.Refused):pass
        assert proc.stdin.frames==[], 'selected UNKNOWN lease wrote a new protocol frame'
        raise ingress.Refused('fixture-final-hold')
    b.e._turn=turn
    assert isinstance(asyncio.run(deliver(b))[0],ingress.Refused)
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2


@pytest.mark.parametrize('draining',[False,True])
def test_actual_rpc_helper_preserves_original_frame_under_same_parent(native,monkeypatch,draining):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    frame={'id':1,'method':'initialize','params':{'text':'中文\r\n e\u0301 \u200d'}}
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        if draining:b.c.begin_drain(b.c.snapshot()['epoch'])
        await ns['_codex_rpc_send'](p,frame,writer=c)
        assert proc.stdin.frames==[(json.dumps(frame,ensure_ascii=False)+'\n').encode('utf-8')]
        assert b.c.snapshot()['active_leases']==2  # write/drain did not release authority.
        await c.finish(p,asyncio.create_task(c.read_stderr()))
    b.e._turn=turn;assert asyncio.run(deliver(b))==[None]


@pytest.mark.parametrize('fault',['foreign_process','closed','guard_scope','guard_action','bad_utf8','nonfinite'])
def test_actual_rpc_refusal_before_write_keeps_original_lease(native,monkeypatch,fault):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        frame={'id':1,'method':'initialize','params':{}}
        guard=None
        if fault=='foreign_process':p=Process();p.stdin=Sink()
        if fault=='closed':c._closing=True
        if fault=='guard_scope':guard=lambda:monkeypatch.setattr(child.storage_module,'select',lambda:None)
        if fault=='guard_action':guard=lambda:runtime.CURRENT.set(None)
        if fault=='bad_utf8':frame['params']['text']='\ud800'
        if fault=='nonfinite':frame['params']['value']=float('nan')
        await ns['_codex_rpc_send'](p,frame,writer=c,guard=guard)
    b.e._turn=turn;assert isinstance(asyncio.run(deliver(b))[0],BaseException)
    assert proc.stdin.frames==[] and b.c.snapshot()['state']=='UNKNOWN'
    assert b.c.snapshot()['active_leases']==2


@pytest.mark.parametrize('fault',['timeout','drain_error','post_unknown','post_scope'])
def test_partial_write_or_post_check_unknown_is_not_retried(native,monkeypatch,fault):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    monkeypatch.setattr(child,'EXIT_DEADLINE',.01)
    async def drain():
        if fault=='timeout':await asyncio.sleep(10)
        if fault=='drain_error':raise BrokenPipeError('synthetic possible write')
        if fault=='post_unknown':b.c.mark_unknown(b.c.snapshot()['epoch'])
        if fault=='post_scope':monkeypatch.setattr(child.storage_module,'select',lambda:None)
    proc.stdin.drain=drain
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        frame={'id':1,'method':'initialize','params':{}}
        with pytest.raises(BaseException):await ns['_codex_rpc_send'](p,frame,writer=c)
        with pytest.raises(BaseException):await ns['_codex_rpc_send'](p,frame,writer=c)
        assert len(proc.stdin.frames)==1
        raise ingress.Refused('fixture-final-hold')
    b.e._turn=turn;assert isinstance(asyncio.run(deliver(b))[0],ingress.Refused)
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2


@pytest.mark.parametrize('fault',['wrong_type','missing_lf','two_frames','bound'])
def test_owned_raw_frame_shape_rejects_before_stdio(native,monkeypatch,fault):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    raw={'wrong_type':'{}\n','missing_lf':b'{}','two_frames':b'{}\n{}\n','bound':b'{"too_long":1}\n'}[fault]
    if fault=='bound':monkeypatch.setattr(child,'MAX_WRITE',4)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        await c.write(p,raw)
    b.e._turn=turn;assert isinstance(asyncio.run(deliver(b))[0],ingress.Refused)
    assert proc.stdin.frames==[] and b.c.snapshot()['state']=='UNKNOWN'


def test_actual_control_steer_is_same_owned_writer_and_native_ack_stays_separate(native,monkeypatch):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        active=ns['_CodexActiveTurn'](p,'thread','turn',writer=c)
        async def drain():
            frame=json.loads(proc.stdin.frames[-1]);active.resolve({'id':frame['id'],'result':{'turnId':'turn'}})
        proc.stdin.drain=drain
        b.c.begin_drain(b.c.snapshot()['epoch'])
        await active.steer('synthetic full original answer')
        frame=json.loads(proc.stdin.frames[0]);assert frame['method']=='turn/steer'
        assert frame['params']['input']==[{'type':'text','text':'synthetic full original answer'}]
        assert active._pending=={} and b.c.snapshot()['active_leases']==2
        await c.finish(p,asyncio.create_task(c.read_stderr()))
    b.e._turn=turn;assert asyncio.run(deliver(b))==[None]


@pytest.mark.parametrize('fault',['before_guard','unknown','drain'])
def test_actual_control_refusal_removes_only_unconfirmed_future_no_second_frame(native,monkeypatch,fault):
    b=native;proc=Process();proc.stdin=Sink();fake_spawn(monkeypatch,proc)
    ns,_,_,_=source_namespace(monkeypatch,b)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        active=ns['_CodexActiveTurn'](p,'thread','turn',writer=c)
        def refused():raise ingress.Refused('fixture-before-write')
        if fault=='unknown':b.c.mark_unknown(b.c.snapshot()['epoch'])
        if fault=='drain':
            async def broken():raise BrokenPipeError('synthetic unknown drain')
            proc.stdin.drain=broken
        with pytest.raises(BaseException):await active.steer('synthetic',guard=refused if fault=='before_guard' else None)
        assert active._pending=={} and len(proc.stdin.frames)==(1 if fault=='drain' else 0)
        with pytest.raises(BaseException):await active.steer('synthetic')
        assert len(proc.stdin.frames)==(1 if fault=='drain' else 0)
        raise ingress.Refused('fixture-final-hold')
    b.e._turn=turn;assert isinstance(asyncio.run(deliver(b))[0],ingress.Refused)
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2


def test_actual_selected_call_sites_bind_owned_send_control_and_approvals():
    tree=ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/fleet_bridge.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_turn_codex')
    wrapper=next(n for n in ast.walk(node) if isinstance(n,ast.AsyncFunctionDef) and n.name=='native_send')
    assert 'writer=managed_native' in ast.unparse(wrapper)
    assert sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='native_send' for n in ast.walk(node))==8
    constructors=[n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_CodexActiveTurn']
    assert len(constructors)==1 and 'managed_native' in ast.unparse(constructors[0])
    assert 'lambda frame: native_send(frame)' in ast.unparse(node)


@pytest.mark.parametrize('fault',['pid_bool','returncode','group','lookup_error','cancelled'])
def test_actual_entry_turn_retains_returned_owner_without_external_child_list(native,monkeypatch,fault):
    b=native;del b.children  # No fixture registry may substitute actual Entry.
    ns,_,_,_=source_namespace(monkeypatch,b)
    ns['_MANAGED_ENTRY']=b.e;ns['REPO']=str(b.r.selection.target_home)
    ns['_codex_archived_binding']=lambda *a,**kw:None
    ns['_load_session']=lambda *a:None
    monkeypatch.setattr(child.storage_module,'session_mapping',lambda *a:None)
    monkeypatch.setattr(ns['codex_questions'],'selected_protocol',lambda _:None)
    returned=[];dispatch=[]
    async def spawn(*a,**kw):
        p=Process();p.stdin=Sink()
        if fault=='pid_bool':p.pid=True
        if fault=='returncode':p.returncode=False
        returned.append(weakref.ref(p));dispatch.append(1);return p
    monkeypatch.setattr(child.asyncio,'create_subprocess_exec',spawn)
    def group(pid):
        if fault=='lookup_error':raise ProcessLookupError()
        if fault=='cancelled':raise asyncio.CancelledError()
        return pid+1
    monkeypatch.setattr(child.os,'getpgid',group)
    async def turn(chat,prompt,mid):await ns['_run_turn_codex'](chat,prompt,mid)
    b.e._turn=turn
    result=asyncio.run(deliver(b));assert isinstance(result[0],BaseException)
    del result;gc.collect()
    work=b.frame['work_id']
    assert list(b.e._native_owners)==[work] and dispatch==[1]
    owner=b.e._native_owners[work]
    assert owner.entry is b.e and owner.action.request.work_id == work and json.loads(owner.action.request.payload)['text'] == b.frame['text']
    assert owner._process is returned[0]() and owner._process.stdin is not None
    assert owner._observed_pid==owner._process.pid
    assert owner._observed_returncode is owner._process.returncode
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2
    assert ns['CODEX_PROCS']=={}  # Refused spawn never published a valid leader.
    with pytest.raises((ingress.Refused,control.Refused)):
        b.e.native_child(b.e.route.native_chat,'same synthetic prompt',None,False)
    assert list(b.e._native_owners)==[work] and dispatch==[1]


def test_preexisting_actual_owner_cannot_be_overwritten_or_spawned(native,monkeypatch):
    b=native;del b.children;sentinel=object();b.e._native_owners[b.frame['work_id']]=sentinel
    async def forbidden(*a,**kw):pytest.fail('No process after owner conflict')
    monkeypatch.setattr(child.asyncio,'create_subprocess_exec',forbidden)
    async def turn(chat,prompt,mid):b.e.native_child(chat,prompt,None,False)
    b.e._turn=turn
    result=asyncio.run(deliver(b));assert isinstance(result[0],ingress.Refused)
    assert b.e._native_owners[b.frame['work_id']] is sentinel
    assert b.c.snapshot()['active_leases']==1 and b.c.snapshot()['state']=='UNKNOWN'


def test_actual_owner_registry_removes_only_exact_natural_settlement(native,monkeypatch):
    b=native;del b.children;proc=Process();fake_spawn(monkeypatch,proc)
    async def turn(chat,prompt,mid):
        c=b.e.native_child(chat,prompt,None,False)
        assert b.e._native_owners[b.frame['work_id']] is c
        p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        await c.finish(p,asyncio.create_task(c.read_stderr()))
        assert b.e._native_owners=={} and b.c.snapshot()['active_leases']==1
    b.e._turn=turn;assert asyncio.run(deliver(b))==[None]

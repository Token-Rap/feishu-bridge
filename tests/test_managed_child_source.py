"""Fresh synthetic coordinator/frontend, fake protocol and benign Python child.

No Codex/config/account/model/native product or existing user home is invoked.
"""
import asyncio
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from feishu_bridge import fleet_managed_child as child
from feishu_bridge import fleet_managed_entry as entry
from feishu_bridge import fleet_codex_storage as storage
from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_managed_runtime as runtime
from test_managed_entry_source import bound, deliver, TEXT


@pytest.fixture
def native(bound, monkeypatch):
    b=bound
    nh=b.r.selection.target_home/'synthetic-native';nh.mkdir(mode=0o700)
    scope=storage.Scope(nh,'a'*32,'b'*64,(nh.stat().st_dev,nh.stat().st_ino),nh/'synthetic-desktop')
    monkeypatch.setattr(storage,'select',lambda:scope)
    for k,v in {'FLEET_TARGET_CONTROL_REQUIRED':'1',
        'FLEET_TARGET_CONTROL_DIR':str(b.r.selection.directory),
        'FLEET_TARGET_CONTROL_ID':b.r.selection.selector_id,
        'FLEET_BRIDGE_HOME':str(b.r.selection.target_home)}.items():monkeypatch.setenv(k,v)
    b.e._stamp=lambda text,at,display:'STAMP['+display+']\n'+text
    b.state['terminal']='completed'  # Fixture-only, no product attestation.
    b.children=[]
    def begin(prompt):
        c=b.e.native_child(b.e.route.native_chat,prompt,None,False);b.children.append(c);return c
    b.begin=begin;b.scope=scope;b.reserve()
    return b


class Reader:
    def __init__(self,data=b''):self.data=data
    async def read(self,n=-1):
        data,self.data=self.data[:n] if n>=0 else self.data,self.data[n:] if n>=0 else b''
        return data
    def at_eof(self):return not self.data


class Process:
    def __init__(self,code=0,tail=b'',err=b''):
        self.pid=12345;self.returncode=None;self.stdout=Reader(tail);self.stderr=Reader(err)
        self.stdin=SimpleNamespace(close=lambda:None);self.code=code
    async def wait(self):self.returncode=self.code;return self.code
    def terminate(self):pytest.fail('No legacy terminate in selected cleanup')
    def kill(self):pytest.fail('No selected process signal')


def fake_spawn(monkeypatch,proc):
    calls=[]
    async def spawn(*args,**kw):calls.append((args,kw));return proc
    monkeypatch.setattr(child.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(child.os,'getpgid',lambda pid:pid)
    def absent(pid,sig):
        assert sig==0;raise ProcessLookupError()
    monkeypatch.setattr(child.os,'killpg',absent)
    return calls


@pytest.mark.parametrize('code',[0,1,-15])
def test_scoped_child_lease_blocks_parent_until_natural_group_stdio_settlement(native,monkeypatch,code):
    b=native;proc=Process(code);calls=fake_spawn(monkeypatch,proc)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt)
        assert b.c.snapshot()['active_leases']==2
        p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        stderr=asyncio.create_task(c.read_stderr())
        b.c.begin_drain(b.c.snapshot()['epoch'])
        result=await c.finish(p,stderr)
        assert result.returncode==code and result.owned_group_absent and result.stdio_eof
        assert not result.complete_descendant_exclusion and not result.native_ready
        assert b.c.snapshot()['active_leases']==1
    b.e._turn=turn
    assert asyncio.run(deliver(b))==[None]
    args,kw=calls[0]
    assert args[:5]==('/synthetic/codex','--strict-config','app-server','--listen','stdio://')
    assert kw['start_new_session'] is True and kw['env']['CODEX_HOME']==str(b.scope.home)
    assert b.c.snapshot()['unresolved']==0 and b.c.snapshot()['state']=='DRAINING'


@pytest.mark.parametrize('fault',['timeout','stdout_tail','stderr_bound','group_present','group_permission',
    'scope_drift','owner','spawn','return_shape','returncode_shape','stderr_cancel'])
def test_unknown_spawn_or_cleanup_keeps_lease_and_no_replay(native,monkeypatch,fault):
    b=native;proc=Process(tail=b'private synthetic tail' if fault=='stdout_tail' else b'',
                            err=b'x'*(child.MAX_STDERR+1) if fault=='stderr_bound' else b'')
    calls=fake_spawn(monkeypatch,proc)
    if fault=='owner':monkeypatch.setattr(child.os,'getpgid',lambda pid:pid+1)
    if fault=='spawn':
        async def fail(*a,**kw):raise OSError('synthetic spawn failure')
        monkeypatch.setattr(child.asyncio,'create_subprocess_exec',fail)
    if fault=='timeout':
        monkeypatch.setattr(child,'EXIT_DEADLINE',.01)
        async def wait():await asyncio.sleep(10)
        proc.wait=wait
    if fault=='return_shape':proc.code=True
    if fault=='returncode_shape':
        async def mixed():proc.returncode=True;return 1
        proc.wait=mixed
    if fault=='group_present':monkeypatch.setattr(child.os,'killpg',lambda pid,sig:None)
    if fault=='group_permission':
        def denied(pid,sig):raise PermissionError()
        monkeypatch.setattr(child.os,'killpg',denied)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt)
        p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        stderr=asyncio.create_task(c.read_stderr())
        if fault=='scope_drift':monkeypatch.setattr(storage,'select',lambda:None)
        if fault=='stderr_cancel':stderr.cancel()
        await c.finish(p,stderr)
    b.e._turn=turn
    results=asyncio.run(deliver(b))
    assert isinstance(results[0],BaseException)
    assert b.c.snapshot()['state']=='UNKNOWN' and b.c.snapshot()['active_leases']==2
    assert b.c.snapshot()['unresolved']==1
    assert len(calls)<=1 and len(b.children)==1


def test_prompt_origin_full_bytes_and_one_entry_attempt_are_bound(native):
    b=native
    async def turn(chat,prompt,mid):
        assert prompt.endswith(TEXT) and runtime.CURRENT.get().request.payload
        with pytest.raises(ingress.Refused,match='managed_native_prompt_unknown'):
            b.e.native_child(chat,prompt+' changed',None,False)
        with pytest.raises(ingress.Refused,match='managed_native_prompt_unknown'):
            b.e.native_child(chat,prompt,'foreign-message',False)
        with pytest.raises(ingress.Refused,match='managed_native_prompt_unknown'):
            b.e.native_child(chat,prompt,None,True)
        b.begin(prompt)
        with pytest.raises(ingress.Refused,match='managed_native_attempt_consumed'):b.begin(prompt)
        raise ingress.Refused('fixture-no-spawn')
    b.e._turn=turn;assert isinstance(asyncio.run(deliver(b))[0],ingress.Refused)
    assert b.c.snapshot()['active_leases']==2 and b.c.snapshot()['state']=='UNKNOWN'
    assert entry._NATIVE_PROMPT.get() is None


def test_stderr_exact_bound_and_cleanup_attempt_cannot_repeat(native,monkeypatch):
    b=native;proc=Process(err=b'x'*child.MAX_STDERR);fake_spawn(monkeypatch,proc)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        task=asyncio.create_task(c.read_stderr());await c.finish(p,task)
        assert len(task.result())==child.MAX_STDERR
        with pytest.raises(ingress.Refused,match='managed_native_cleanup_consumed'):await c.finish(p,task)
        with pytest.raises(ingress.Refused,match='managed_native_attempt_consumed'):
            await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
    b.e._turn=turn;assert asyncio.run(deliver(b))==[None]


def test_actual_benign_python_child_natural_exit_uses_real_owned_group(native,monkeypatch):
    b=native;real=asyncio.create_subprocess_exec;started=[]
    async def benign(*args,**kw):
        # Test-only replacement is explicit; native command/policy is NOT tested.
        p=await real(sys.executable,'-I','-S','-B','-c',
            'import sys;sys.stdin.buffer.read()',cwd=str(b.r.selection.target_home),
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},stdin=kw['stdin'],stdout=kw['stdout'],
            stderr=kw['stderr'],limit=kw['limit'],start_new_session=kw['start_new_session'])
        started.append(p);return p
    monkeypatch.setattr(child.asyncio,'create_subprocess_exec',benign)
    async def turn(chat,prompt,mid):
        c=b.begin(prompt);p=await c.spawn(b.scope,'/synthetic/codex',str(b.r.selection.target_home),1024)
        result=await c.finish(p,asyncio.create_task(c.read_stderr()))
        assert result.returncode==0 and result.owned_group_absent and p.returncode==0
    b.e._turn=turn;assert asyncio.run(deliver(b))==[None]
    assert len(started)==1 and b.c.snapshot()['unresolved']==0


def test_actual_bridge_branch_retains_owner_until_finish_and_has_no_selected_signal_fallback():
    tree=ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/fleet_bridge.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_turn_codex')
    assert any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='native_child' for n in ast.walk(node))
    finally_tree=ast.Module(body=node.body[-1].finalbody,type_ignores=[])
    text=ast.unparse(finally_tree)
    assert text.index('await managed_native.finish')<text.index('CODEX_PROCS.pop')
    guarded=next(n for n in ast.walk(finally_tree) if isinstance(n,ast.If) and ast.unparse(n.test)=='managed_native is None and proc is not None')
    assert 'proc.terminate()' in ast.unparse(guarded)
    helper=ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/fleet_managed_child.py').read_text())
    assert not any(isinstance(n,ast.Attribute) and n.attr in {'terminate','kill'} for n in ast.walk(helper))


def test_actual_selected_begin_refusal_never_erases_existing_native_owner(monkeypatch):
    from test_codex_questions import source_namespace
    ns,_,_,_=source_namespace(monkeypatch)
    owner=object();ns['CODEX_PROCS']['work']=owner
    def begin(*a):raise ingress.Refused('fixture-before-child')
    ns['_MANAGED_ENTRY']=SimpleNamespace(native_child=begin)
    async def forbidden(*a,**kw):pytest.fail('No child launch after selected begin refusal')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',forbidden)
    with pytest.raises(ingress.Refused,match='managed_native_owner_unbound'):
        asyncio.run(ns['_run_turn_codex']('work','synthetic original prompt'))
    assert ns['CODEX_PROCS']['work'] is owner

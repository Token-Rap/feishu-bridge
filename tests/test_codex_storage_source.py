"""SOURCE-only storage admission and actual public caller fixtures. No Codex/SDK."""
import ast
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fleet_codex_storage as storage


def make_home(root):
    h = root / 'fleet-private'
    h.mkdir(mode=0o700)
    cfg = {'sqlite_home': str(h), 'shell_environment_policy': {'set': {**{k:str(h) for k in storage._RESERVED},storage.DESKTOP_KEY:str(root/'desktop-personal')}},
           'forced_login_method':'chatgpt', 'cli_auth_credentials_store':'file', 'model_provider':'openai'}
    body = 'forced_login_method="chatgpt"\n'+'sqlite_home='+json.dumps(str(h))+'\n[shell_environment_policy.set]\n'+''.join(k+'='+json.dumps(str(h))+'\n' for k in storage._RESERVED)+storage.DESKTOP_KEY+'='+json.dumps(str(root/'desktop-personal'))+'\n'
    (h/'config.toml').write_text(body);(h/'config.toml').chmod(0o600)
    (h/'auth.json').write_bytes(b'SYNTHETIC placeholder; never read');(h/'auth.json').chmod(0o600)
    (h/storage.MARKER).write_text(json.dumps({'version':1,'namespace':'a'*32,'config_sha256':hashlib.sha256(body.encode()).hexdigest()}));(h/storage.MARKER).chmod(0o600)
    return h,cfg


@pytest.fixture
def selected(tmp_path, monkeypatch):
    h,cfg=make_home(tmp_path);monkeypatch.setenv(storage.KEY,str(h))
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'desktop-personal'))
    monkeypatch.setenv('CODEX_SQLITE_HOME',str(tmp_path/'desktop-db'))
    return h,cfg,storage.select()


def transcript(h,sid='thread'):
    p=h/'sessions'/'2026'/'10'/('rollout-now-'+sid+'.jsonl');p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':sid}})+'\n'+json.dumps({'type':'compacted'})+'\n')
    return p


def compile_defs(source,names,ns):
    tree=ast.parse(source)
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])),'<ACTUAL SOURCE>','exec'),ns)
    return ns


def bridge_defs(names,ns):
    return compile_defs((ROOT/'fleet_bridge.py').read_text(),names,ns)


def test_source_never_changes_parent_or_reads_auth_body(selected,monkeypatch):
    h,cfg,s=selected;before=dict(os.environ);old=Path.read_bytes
    def checked(p):
        assert p != h/'auth.json','credential contents must not be opened'
        return old(p)
    monkeypatch.setattr(Path,'read_bytes',checked)
    cmd=s.command(['codex','app-server','--listen','stdio://']);env=s.env()
    assert env['CODEX_HOME']==env['CODEX_SQLITE_HOME']==env[storage.KEY]==str(h)
    assert dict(os.environ)==before and cmd[:4]==['codex','app-server','--listen','stdio://']
    s.effective(cfg)


@pytest.mark.parametrize('field,value',[('forced_login_method','api'),('cli_auth_credentials_store',None),('model_provider','foreign')])
def test_layered_account_route_cannot_escape_selected_home(selected,field,value):
    h,cfg,s=selected
    cfg[field]=value
    with pytest.raises(storage.StorageRefused,match='effective_account_route_unknown'):
        s.effective(cfg)


@pytest.mark.parametrize('bad',['missing','relative','desktop','symlink','public','config-drift','marker','auth-missing'])
def test_invalid_home_never_falls_back(selected,monkeypatch,tmp_path,bad):
    h,cfg,s=selected
    if bad=='missing':monkeypatch.delenv(storage.KEY)
    elif bad=='relative':monkeypatch.setenv(storage.KEY,'relative')
    elif bad=='desktop':monkeypatch.setenv(storage.KEY,str(Path.home()/'.codex'))
    elif bad=='symlink':
        p=tmp_path/'linked';p.symlink_to(h);monkeypatch.setenv(storage.KEY,str(p))
    elif bad=='public':h.chmod(0o755)
    elif bad=='config-drift':(h/'config.toml').write_text('sqlite_home="foreign"')
    elif bad=='marker':(h/storage.MARKER).write_text('{}')
    else:(h/'auth.json').unlink()
    with pytest.raises(storage.StorageRefused):storage.select()


@pytest.mark.parametrize('which',['foreign-sqlite','missing-set','include-drops-home','profile','foreign-log'])
def test_effective_child_environment_unknown_refuses(selected,which):
    h,cfg,s=selected;cfg=deepcopy(cfg)
    if which=='foreign-sqlite':cfg['sqlite_home']='foreign'
    elif which=='missing-set':cfg['shell_environment_policy']['set']={}
    elif which=='include-drops-home':cfg['shell_environment_policy']['include_only']=['PATH','HOME']
    elif which=='profile':cfg['shell_environment_policy']['experimental_use_profile']=True
    else:cfg['log_dir']='/foreign'
    with pytest.raises(storage.StorageRefused):s.effective(cfg)


def test_home_drift_and_runtime_namespace_binding(selected):
    h,cfg,s=selected;before=storage.runtime_binding();d=json.loads((h/storage.MARKER).read_text());d['namespace']='b'*32;(h/storage.MARKER).write_text(json.dumps(d))
    assert storage.runtime_binding()!=before
    with pytest.raises(storage.StorageRefused):s.env()


@pytest.mark.parametrize('kind',['missing','wrong-id','duplicate','symlink','outside'])
def test_resume_requires_exact_fleet_rollout_not_desktop(selected,tmp_path,kind):
    h,cfg,s=selected;p=transcript(h)
    if kind=='missing':p.unlink()
    elif kind=='wrong-id':p.write_text(json.dumps({'type':'session_meta','payload':{'id':'other'}}))
    elif kind=='duplicate':(p.parent/'rollout-other-thread.jsonl').write_bytes(p.read_bytes())
    elif kind=='symlink':p.unlink();p.symlink_to(tmp_path/'personal.jsonl')
    else:
        original=p.read_bytes();p.unlink();external=tmp_path/'outside';external.mkdir();(external/p.name).write_bytes(original);p.parent.rmdir();p.parent.symlink_to(external)
    with pytest.raises(storage.StorageRefused):s.transcript('thread')


def test_session_mapping_corruption_is_not_new_thread(tmp_path,selected):
    h,cfg,scope=selected
    p=tmp_path/'sessions.json';p.write_text('broken')
    with pytest.raises(storage.StorageRefused):storage.session_mapping(p,'work::codex','',scope)
    p.write_text(json.dumps({'work::codex':{'active':'original','sessions':{'original':{'fleet_codex_namespace':{'home':str(h),'namespace':scope.namespace}}}}}))
    with pytest.raises(storage.StorageRefused):storage.session_mapping(p,'work::codex','',scope)
    storage.session_mapping(p,'work::codex','original',scope)
    p.write_text(json.dumps({'work::codex':{'active':None,'sessions':[]}}))
    storage.session_mapping(p,'work::codex','',scope)


class FakeProc:
    """Bounded synthetic config-only transport, never an OS child."""
    def __init__(self,cfg):self.cfg=cfg;self.stdin=self;self.stdout=self;self.frames=[];self.queue=[];self.pid=321;self.returncode=None
    def write(self,b):
        f=json.loads(b);self.frames.append(f)
        if f.get('method')=='initialize':self.queue.append({'id':f['id'],'result':{}})
        if f.get('method')=='config/read':self.queue.append({'id':f['id'],'result':{'config':self.cfg}})
    async def drain(self):pass
    async def read(self):return b''
    @property
    def stderr(self):return self
    async def readline(self):return (json.dumps(self.queue.pop(0))+'\n').encode() if self.queue else b''
    def close(self):self.returncode=0
    async def wait(self):self.returncode=0;return 0
    def terminate(self):raise AssertionError('no real/synthetic signal required')


def test_terminal_config_only_rpc_and_fail_before_any_pty(selected,monkeypatch):
    h,cfg,s=selected;procs=[];launches=[]
    async def spawn(*args,**kw):
        launches.append((args,kw));p=FakeProc(cfg);procs.append(p);return p
    monkeypatch.setattr(storage.asyncio,'create_subprocess_exec',spawn)
    asyncio.run(storage.terminal_preflight(s,'/SYNTHETIC/codex',h))
    assert [f['method'] for f in procs[0].frames]==['initialize','notifications/initialized','config/read']
    assert launches[0][1]['env']['CODEX_HOME']==str(h)
    cfg['shell_environment_policy']['include_only']=['PATH']
    with pytest.raises(storage.StorageRefused):asyncio.run(storage.terminal_preflight(s,'/SYNTHETIC/codex',h))


def test_actual_legacy_exec_helper_carries_env_not_bare_argv(selected):
    h,cfg,s=selected;transcript(h)
    ns=bridge_defs({'_codex_cmd'},dict(Path=Path,os=os,time=__import__('time'),CODEX_BIN='codex',CODEX_SANDBOX='danger-full-access',CODEX_REASONING_SUMMARY='detailed',_codex_storage=storage))
    launch,out=ns['_codex_cmd']('complete prompt','gpt-synthetic','high','thread')
    assert isinstance(launch,storage.Launch) and launch.argv[:3]==['codex','exec','resume']
    assert launch.env['CODEX_HOME']==str(h) and 'thread' in launch.argv
    with pytest.raises(TypeError):list(launch) # A future old caller cannot inherit a desktop home.


@pytest.mark.parametrize('method',['_codex_compact_thread','_rollover_codex_tool_config'])
def test_actual_compact_and_discovery_use_same_home(selected,monkeypatch,method):
    h,cfg,s=selected;transcript(h);procs=[];launches=[]
    class P(FakeProc):
        async def read(self):return b''
        def write(self,b):
            super().write(b);f=json.loads(b)
            if f.get('method')=='thread/resume':self.queue.append({'id':f['id'],'result':{'thread':{'id':'thread'}}})
            if f.get('method')=='thread/compact/start':
                self.queue.extend([{'id':f['id'],'result':{}},{'method':'turn/completed','params':{'threadId':'thread','turn':{'status':'completed'}}}])
        @property
        def stderr(self):return self
    async def spawn(*a,**kw):launches.append((a,kw));p=P(cfg);procs.append(p);return p
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    ns=dict(asyncio=asyncio,json=json,contextlib=__import__('contextlib'),CODEX_BIN='/SYNTHETIC/codex',REPO=str(h),CODEX_STREAM_LIMIT=2**24,_codex_storage=storage)
    bridge_defs({method,'_rollover_codex_command','_codex_rpc_send','_codex_rpc_response'},ns)
    asyncio.run(ns[method]('thread' if method=='_codex_compact_thread' else lambda x:None))
    assert launches[0][1]['env']['CODEX_HOME']==str(h)
    if method=='_codex_compact_thread':
        resume=next(f for f in procs[0].frames if f.get('method')=='thread/resume')
        assert resume['params']['threadId']=='thread' and resume['params']['config']==s.config()
    else:assert all(f['method'] not in ('thread/start','thread/resume','turn/start') for f in procs[0].frames)


def test_actual_call_sites_have_env_and_thread_namespace_and_unchanged_probe_rules():
    src=(ROOT/'fleet_bridge.py').read_text();tree=ast.parse(src)
    funcs={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    for name in ['_run_turn_codex','_codex_compact_thread','_rollover_codex_tool_config','_rollover_write_note_codex']:
        launches=[n for n in ast.walk(funcs[name]) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='create_subprocess_exec']
        assert launches and all(any(k.arg=='env' and ast.unparse(k.value)=='storage.env()' for k in n.keywords) for n in launches)
    run=ast.unparse(funcs['_run_turn_codex'])
    assert 'storage.transcript(thread)' in run and 'storage.effective' in run
    assert "{'ephemeral': True} if permissions_probe else {}" in run
    assert 'Codex resume returned a replacement thread; no session reset' in run
    fork=ast.unparse(funcs['_rollover_write_note_codex'])
    assert "'ephemeral': True" in fork and "'networkAccess': False" in fork
    assert '_ro.codex_transcript_path' not in ast.unparse(funcs['_rollover_tick'])


@pytest.mark.parametrize('refusal',[False,True])
def test_windows_acl_unknown_never_silently_accepts(selected,monkeypatch,refusal):
    h,cfg,s=selected;calls=[]
    def run(argv,**kw):
        calls.append(argv);return SimpleNamespace(returncode=1 if refusal else 0,stdout=b'private' if not refusal else b'')
    monkeypatch.setattr(storage.subprocess,'run',run)
    if refusal:
        with pytest.raises(storage.StorageRefused):storage._windows_private([h])
    else:storage._windows_private([h])
    import base64
    ps=base64.b64decode(calls[0][-1]).decode('utf-16le')
    assert 'Get-Acl' in ps and 'Set-Acl' not in ps and 'foreign_owner' in ps and 'nonprivate_acl' in ps


def ordinary_namespace(monkeypatch,h):
    """Reuse the existing actual-definition harness without importing SDK/app."""
    import contextvars,contextlib,time,re
    import codex_questions as q
    import codex_approvals
    harness_globals=dict(ast=ast,asyncio=asyncio,contextvars=contextvars,contextlib=contextlib,
        hashlib=hashlib,json=json,os=os,time=time,re=re,Path=Path,SimpleNamespace=SimpleNamespace,
        q=q,codex_approvals=codex_approvals,app_frontend=SimpleNamespace(),ROOT=ROOT)
    compile_defs((ROOT/'tests/test_codex_questions.py').read_text(),{'source_namespace'},harness_globals)
    ns,events,logs,sessions=harness_globals['source_namespace'](monkeypatch)
    ns['_codex_storage']=storage;ns['_fs']=SimpleNamespace(SESSIONS_FILE=h.parent/'fleet-sessions.json',
                                                         sessions_revision=lambda: None)
    return ns,events,logs,sessions


@pytest.mark.parametrize('mode',['new','resume','missing-original','invalid-effective'])
def test_actual_ordinary_start_resume_and_pre_model_refusals(selected,monkeypatch,mode):
    h,cfg,s=selected;ns,events,logs,sessions=ordinary_namespace(monkeypatch,h)
    if mode=='new':sessions.clear()
    elif mode!='missing-original':transcript(h)
    ns['_fs'].SESSIONS_FILE.write_text(json.dumps({k:{'active':v,'sessions':{v:{'fleet_codex_namespace':{'home':str(h),'namespace':s.namespace}}}} for k,v in sessions.items()}))
    if mode=='invalid-effective':cfg=deepcopy(cfg);cfg['shell_environment_policy']['include_only']=['HOME']
    launches=[];proc=FakeProc(cfg)
    old_write=proc.write
    def write(raw):
        old_write(raw);f=json.loads(raw)
        if f.get('method') in ('thread/start','thread/resume'):
            proc.queue.append({'id':f['id'],'result':{'thread':{'id':'thread'}}})
    proc.write=write
    async def spawn(*a,**kw):launches.append((a,kw));return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    real_send=ns['_codex_rpc_send']
    class BeforeModel(BaseException):pass
    async def send(p,frame,**kw):
        if frame.get('method')=='turn/start':raise BeforeModel()
        await real_send(p,frame,**kw)
    ns['_codex_rpc_send']=send
    before=deepcopy(sessions)
    if mode in ('new','resume'):
        with pytest.raises(BeforeModel):
            asyncio.run(ns['_run_turn_codex']('work','complete SYNTHETIC input'))
        frame=next(f for f in proc.frames if f.get('method') in ('thread/start','thread/resume'))
        assert frame['method']==('thread/start' if mode=='new' else 'thread/resume')
        assert frame['params']['config']==s.config()
        if mode=='resume':assert frame['params']['threadId']=='thread'
        assert frame['params']['sandbox']=='danger-full-access' and frame['params']['approvalPolicy']=='never'
        assert not frame['params'].get('ephemeral',False)
        assert launches[0][1]['env']['CODEX_HOME']==str(h)
    else:
        asyncio.run(ns['_run_turn_codex']('work','complete SYNTHETIC input'))
        assert not any(f.get('method') in ('thread/start','thread/resume') for f in proc.frames)
        if mode=='missing-original':assert launches==[]
    assert sessions==before
    assert not any(f.get('method')=='turn/start' for f in proc.frames)


def test_removed_terminal_mode_is_not_reintroduced():
    assert not (ROOT / 'fleet_pty.py').exists()
    tree = ast.parse((ROOT / 'fleet_bridge.py').read_text())
    assert not any(isinstance(node, ast.Import) and
                   any(alias.name == 'fleet_pty' for alias in node.names)
                   for node in tree.body)


def test_per_session_namespace_not_inferred_from_id_or_file(selected,tmp_path):
    h,cfg,s=selected;p=tmp_path/'map.json';transcript(h)
    p.write_text(json.dumps({'work::codex':{'active':'thread','sessions':{'thread':{'name':'held original'}}}}))
    before=p.read_bytes()
    with pytest.raises(storage.StorageRefused):storage.session_mapping(p,'work::codex','thread',s)
    import fleet_sessions
    old=fleet_sessions.SESSIONS_FILE;fleet_sessions.SESSIONS_FILE=p
    try:
        with pytest.raises(storage.StorageRefused):storage.record_session(fleet_sessions,'work::codex','thread',s,'unchanged')
    finally:fleet_sessions.SESSIONS_FILE=old
    assert p.read_bytes()==before


def test_new_session_namespace_record_preserves_ids_history_other_work(selected,tmp_path):
    h,cfg,s=selected;p=tmp_path/'map.json'
    data={'other':{'active':'other-id','sessions':{'other-id':{'name':'held','ts':'original'}}},'work::codex':{'active':None,'sessions':{'old':{'name':'personal historical','ts':'original'}}}}
    p.write_text(json.dumps(data));import fleet_sessions
    old=fleet_sessions.SESSIONS_FILE;fleet_sessions.SESSIONS_FILE=p
    try:storage.record_session(fleet_sessions,'work::codex','new-native-id',s,'complete question')
    finally:fleet_sessions.SESSIONS_FILE=old
    after=json.loads(p.read_text());assert after['other']==data['other'] and after['work::codex']['sessions']['old']==data['work::codex']['sessions']['old']
    storage.session_mapping(p,'work::codex','new-native-id',s)
    assert after['work::codex']['active']=='new-native-id'
    assert after['work::codex']['sessions']['new-native-id']['fleet_codex_namespace']=={'home':str(h),'namespace':s.namespace}


def test_foreign_account_refuses_without_changing_ownership(selected,monkeypatch):
    h,cfg,s=selected
    if os.name=='nt':pytest.skip('POSIX account SOURCE control')
    before=h.stat().st_uid;monkeypatch.setattr(storage.os,'getuid',lambda:-1)
    with pytest.raises(storage.StorageRefused):storage.select()
    assert h.stat().st_uid==before


def test_effective_auth_environment_is_refused_not_switched(selected,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','SYNTHETIC-not-an-account-token')
    with pytest.raises(storage.StorageRefused):storage.select()


def rebind_config(h, text):
    (h/'config.toml').write_text(text)
    marker=json.loads((h/storage.MARKER).read_text())
    marker['config_sha256']=hashlib.sha256(text.encode()).hexdigest()
    (h/storage.MARKER).write_text(json.dumps(marker))


@pytest.mark.parametrize('route',[
    {'base_url':'https://not-a-live-provider.invalid/v1','env_key':'SYNTHETIC_OTHER_KEY','requires_openai_auth':False},
    {'auth':{'command':'SYNTHETIC NEVER RUN','args':['SYNTHETIC'],'cwd':'/SYNTHETIC'}},
    {'experimental_bearer_token':'SYNTHETIC'}, {'http_headers':{'Authorization':'SYNTHETIC'}},
    {'env_http_headers':{'Authorization':'SYNTHETIC_KEY'}}, {'requires_openai_auth':True}, {},
    None, [], False,
])
def test_selected_provider_routes_refuse_local_and_effective(selected,route):
    h,cfg,scope=selected
    cfg['model_providers']={'openai':route}
    with pytest.raises(storage.StorageRefused,match='effective_account_route_unknown'):
        scope.effective(cfg)
    text=(h/'config.toml').read_text()+'\n[model_providers.openai]\n'
    if type(route) is dict:
        # Complete concrete nested regression, without reading/exporting auth.
        if 'base_url' in route:text+='base_url="https://not-a-live-provider.invalid/v1"\nenv_key="SYNTHETIC_OTHER_KEY"\nrequires_openai_auth=false\n'
        elif 'auth' in route:text+='[model_providers.openai.auth]\ncommand="SYNTHETIC NEVER RUN"\n'
        else:text+='name="SYNTHETIC UNREVIEWED"\n'
    rebind_config(h,text)
    with pytest.raises(storage.StorageRefused,match='account_route_unreviewed'):
        storage.select()


@pytest.mark.parametrize('field',['openai_base_url','chatgpt_base_url','auth','base_url','env_key','http_headers','env_http_headers','experimental_bearer_token'])
def test_route_override_refuses_even_empty_unknown_values(selected,field):
    h,cfg,scope=selected;cfg[field]=''
    with pytest.raises(storage.StorageRefused):scope.effective(cfg)
    # Put root keys before the shell table so this is an actual top-level TOML.
    rebind_config(h,field+'="SYNTHETIC"\n'+(h/'config.toml').read_text())
    with pytest.raises(storage.StorageRefused):storage.select()


@pytest.mark.parametrize('providers',[None,{}])
def test_supported_builtin_route_has_no_custom_declaration(selected,providers):
    h,cfg,scope=selected;cfg['model_providers']=providers;scope.effective(cfg)
    assert storage.select()==scope
    assert cfg['forced_login_method']=='chatgpt' and cfg['model_provider']=='openai'


def test_exact_public_chatgpt_projection_keeps_storage_and_auth_bound(selected,monkeypatch):
    h,cfg,scope=selected
    cfg['chatgpt_base_url']='https://chatgpt.com/backend-api/'
    old=Path.read_bytes
    def checked(p):
        assert p != h/'auth.json','credential contents must not be opened'
        return old(p)
    monkeypatch.setattr(Path,'read_bytes',checked)
    scope.effective(cfg)
    assert storage.select()==scope
    assert scope.env()['CODEX_HOME']==scope.env()['CODEX_SQLITE_HOME']==str(h)


@pytest.mark.parametrize('value',[
    '', 'https://chatgpt.com/backend-api', 'https://chatgpt.com/backend-api/codex',
    'https://chatgpt.com/backend-api//', 'https://chatgpt.com/backend-api/extra',
    'https://chatgpt.com/backend-api/?token=SYNTHETIC_SECRET',
    'https://chatgpt.com/backend-api/#fragment', 'http://chatgpt.com/backend-api/',
    'https://chatgpt.com:443/backend-api/', 'https://CHATGPT.COM/backend-api/',
    'https://chatgpt.com./backend-api/', 'https://chatgpt.com.evil.invalid/backend-api/',
    'https://user@chatgpt.com/backend-api/', 'https://chatgpt.com/backend-api/ ',
    ' https://chatgpt.com/backend-api/', 'https://chatgpt.com/backend-api/\n',
    [], {}, False, 0, b'https://chatgpt.com/backend-api/',
])
def test_effective_chatgpt_projection_is_exact_and_never_exports_value(selected,value):
    h,cfg,scope=selected;cfg['chatgpt_base_url']=value
    with pytest.raises(storage.StorageRefused) as exc:
        scope.effective(cfg)
    assert str(exc.value)=='Fleet Codex storage: effective_account_route_unknown'


def test_local_exact_public_chatgpt_override_is_still_unreviewed(selected):
    h,cfg,scope=selected
    rebind_config(h,'chatgpt_base_url="https://chatgpt.com/backend-api/"\n'+(h/'config.toml').read_text())
    with pytest.raises(storage.StorageRefused,match='account_route_unreviewed'):
        storage.select()


@pytest.mark.parametrize('field,value',[
    ('model_provider','foreign'), ('forced_login_method','api'),
    ('cli_auth_credentials_store','keyring'), ('model_providers',{'openai':{}}),
    ('openai_base_url','https://chatgpt.com/backend-api/'), ('auth',{}),
    ('base_url','https://chatgpt.com/backend-api/'), ('env_key','SYNTHETIC'),
    ('http_headers',{}), ('env_http_headers',{}), ('experimental_bearer_token','SYNTHETIC'),
])
def test_canonical_chatgpt_projection_does_not_relax_other_account_gates(selected,field,value):
    h,cfg,scope=selected;cfg['chatgpt_base_url']='https://chatgpt.com/backend-api/'
    cfg[field]=value
    with pytest.raises(storage.StorageRefused,match='effective_account_route_unknown'):
        scope.effective(cfg)


@pytest.mark.parametrize('gate',['sqlite','shell','log'])
def test_canonical_chatgpt_projection_does_not_skip_later_storage_gates(selected,gate):
    h,cfg,scope=selected;cfg['chatgpt_base_url']='https://chatgpt.com/backend-api/'
    if gate=='sqlite':cfg['sqlite_home']='foreign'
    elif gate=='shell':cfg['shell_environment_policy']['set']={}
    else:cfg['log_dir']='/foreign'
    with pytest.raises(storage.StorageRefused):scope.effective(cfg)


def test_unknown_url_object_is_not_formatted_or_compared(selected):
    class Unreviewed:
        def __str__(self):raise AssertionError('unreviewed value formatted')
        def __eq__(self,other):raise AssertionError('unreviewed value compared')
    h,cfg,scope=selected;cfg['chatgpt_base_url']=Unreviewed()
    with pytest.raises(storage.StorageRefused,match='effective_account_route_unknown'):
        scope.effective(cfg)


@pytest.mark.parametrize('name',['config.toml',storage.MARKER,'auth.json','transcript','mapping'])
def test_regular_hardlinks_cannot_supply_storage_provenance(selected,tmp_path,name):
    h,cfg,scope=selected
    path=transcript(h) if name=='transcript' else tmp_path/'sessions.json' if name=='mapping' else h/name
    if name=='mapping':path.write_text('{}')
    os.link(path,tmp_path/'hardlink')
    with pytest.raises(storage.StorageRefused,match='hardlinked_input'):
        if name=='transcript':scope.transcript('thread')
        elif name=='mapping':storage.session_mapping(path,'work::codex','',scope)
        else:storage.select()


def test_custom_desktop_home_and_nested_child_reference_are_consistent(selected,monkeypatch):
    h,cfg,scope=selected;desktop=h.parent/'desktop-personal'
    assert scope.desktop_home==desktop
    before=dict(os.environ);child=scope.env()
    assert child[storage.DESKTOP_KEY]==str(desktop) and dict(os.environ)==before
    monkeypatch.setenv('CODEX_HOME',str(h));monkeypatch.setenv(storage.DESKTOP_KEY,str(desktop))
    assert storage.select()==scope
    monkeypatch.delenv(storage.DESKTOP_KEY)
    with pytest.raises(storage.StorageRefused,match='desktop_home_unknown'):storage.select()


def test_custom_desktop_overlap_and_reference_conflict_refuse(selected,monkeypatch):
    h,cfg,scope=selected
    monkeypatch.setenv('CODEX_HOME',str(h.parent))
    with pytest.raises(storage.StorageRefused,match='desktop_home_overlap'):storage.select()
    monkeypatch.setenv(storage.DESKTOP_KEY,str(h.parent/'another-desktop'))
    with pytest.raises(storage.StorageRefused,match='desktop_reference_conflict'):storage.select()


def test_archived_desktop_sid_has_no_namespace_or_context_fallback(selected,tmp_path):
    h,cfg,scope=selected;sid='22222222-3333-4444-8555-666666666666'
    desktop=scope.desktop_home;desktop.mkdir(mode=0o700)
    old=desktop/'archived_sessions';old.mkdir()
    p=old/('rollout-original-'+sid+'.jsonl')
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':sid}})+'\nSYNTHETIC CONTEXT\n')
    mapping=tmp_path/'sessions.json'
    mapping.write_text(json.dumps({'work::codex':{'active':sid,'sessions':{sid:{'name':'SYNTHETIC original'}}}}))
    before=(p.read_bytes(),mapping.read_bytes())
    with pytest.raises(storage.StorageRefused,match='thread_not_in_fleet_namespace'):scope.transcript(sid)
    with pytest.raises(storage.StorageRefused,match='session_namespace_unbound'):
        storage.session_mapping(mapping,'work::codex',sid,scope)
    assert (p.read_bytes(),mapping.read_bytes())==before and p.parent==old
    assert not (h/'sessions').exists()


def test_config_cleanup_has_nominal_wait_and_unbounded_final_reap():
    tree=ast.parse((ROOT/'fleet_codex_storage.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='terminal_preflight')
    waits=[n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='wait_for']
    assert sorted(n.args[1].value for n in waits)==[5,10,10]
    assert 'proc.terminate()' in ast.unparse(node) and 'await proc.wait()' in ast.unparse(node)
    assert 'proc.kill' not in ast.unparse(node)

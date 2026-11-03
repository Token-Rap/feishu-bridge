"""Private owned SYNTHETIC storage and real admission for public source fixtures.

No Codex/SDK/login, no storage-success replacement, no credential body read.
"""
import hashlib,json,os,tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from feishu_bridge.codex import fleet_codex_storage as storage
from feishu_bridge.sessions import fleet_sessions


def make_storage(monkeypatch, base=None):
    owned=None
    if base is None:
        owned=tempfile.TemporaryDirectory(prefix='SYNTHETIC-fleet-storage-')
        base=Path(owned.name).resolve()
    h=base/'fleet-private';h.mkdir(mode=0o700)
    desktop=base/'desktop-personal'
    values={**{k:str(h) for k in storage._RESERVED},storage.DESKTOP_KEY:str(desktop)}
    body='forced_login_method="chatgpt"\nsqlite_home='+json.dumps(str(h))+'\n[shell_environment_policy.set]\n'+''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items())
    for name,content in (('config.toml',body.encode()),('auth.json',b'SYNTHETIC UNREAD PLACEHOLDER'),
            (storage.MARKER,json.dumps(dict(version=1,namespace='a'*32,config_sha256=hashlib.sha256(body.encode()).hexdigest())).encode())):
        p=h/name;p.write_bytes(content);p.chmod(0o600)
    monkeypatch.setenv(storage.KEY,str(h));monkeypatch.setenv('CODEX_HOME',str(desktop))
    monkeypatch.delenv(storage.DESKTOP_KEY,raising=False)
    for k in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','OPENAI_BASE_URL','CHATGPT_BASE_URL'):
        monkeypatch.delenv(k,raising=False)
    scope=storage.select()
    cfg=dict(sqlite_home=str(h),forced_login_method='chatgpt',cli_auth_credentials_store='file',model_provider='openai',shell_environment_policy={'set':values})
    return SimpleNamespace(scope=scope,config=cfg,owned=owned)


def header(fixture,sid):
    p=fixture.scope.home/'sessions'/('rollout-SYNTHETIC-'+sid+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({'type':'session_meta','payload':{'id':sid}})+'\n')
    return p


@pytest.fixture
def storage_fixture(tmp_path,monkeypatch):
    return make_storage(monkeypatch,tmp_path)


def question_storage(monkeypatch,sessions):
    # A prior explicit pure storage fixture remains authoritative for the nested
    # source harness; otherwise allocate one private owned SYNTHETIC fixture.
    existing=bool(os.environ.get(storage.KEY))
    if existing:
        scope=storage.select();f=SimpleNamespace(scope=scope,owned=None)
        f.config=dict(sqlite_home=str(scope.home),forced_login_method='chatgpt',cli_auth_credentials_store='file',model_provider='openai',shell_environment_policy={'set':{**{k:str(scope.home) for k in storage._RESERVED},storage.DESKTOP_KEY:str(scope.desktop_home)}})
    else:f=make_storage(monkeypatch)
    mapping=f.scope.home.parent/'fleet-sessions.json'
    data={k:{'active':sid,'sessions':{sid:{'fleet_codex_namespace':{'home':str(f.scope.home),'namespace':f.scope.namespace}}}} for k,sid in sessions.items()}
    mapping.write_text(json.dumps(data))
    if not existing:
        for sid in sessions.values():header(f,sid)
    def write_rows(rows):
        # Exercise the existing production atomic writer, then mirror the old
        # harness's public in-memory session observation. No bypass of admission.
        old=fleet_sessions.SESSIONS_FILE
        try:
            fleet_sessions.SESSIONS_FILE=mapping;fleet_sessions.write_sessions(rows)
        finally:fleet_sessions.SESSIONS_FILE=old
        sessions.clear();sessions.update({k:v.get('active') for k,v in rows.items() if isinstance(v,dict) and v.get('active')})
    f.fs=SimpleNamespace(SESSIONS_FILE=mapping,chat_entry=fleet_sessions.chat_entry,
                         write_sessions=write_rows, sessions_revision=lambda: None)
    return f


def original_protocol(frames,fixture):
    """Verify exact storage additions, then retain every original RPC assertion."""
    import copy
    projected=[]
    for frame in copy.deepcopy(frames):
        if frame.get('method')=='config/read':
            assert frame['params'].get('includeLayers') is False
            continue
        if frame.get('method') in ('thread/start','thread/resume','thread/fork'):
            cfg=frame['params'].get('config',{})
            for k,v in fixture.scope.config().items():
                assert cfg.pop(k)==v
            if cfg:frame['params']['config']=cfg
            else:frame['params'].pop('config',None)
        projected.append(frame)
    return projected

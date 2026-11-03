"""Actual owner authoring/bootstrap/stdout/Registry SOURCE, synthetic process only."""
import ast,asyncio,copy,json,os,time
from pathlib import Path
import pytest
from feishu_bridge import codex_questions as q
from feishu_bridge import question_defaults_intents as intents
from feishu_bridge import question_defaults_producer as producer
import test_codex_questions as old


def setup(monkeypatch,tmp_path,count=1):
    ns,frames,logs,sessions=old.source_namespace(monkeypatch)
    ns.update(question_defaults_intents=intents,question_defaults_producer=producer,
              _BRIDGE_HOME=tmp_path,_QUESTION_DEFAULT_OWNER=object(),
              _QUESTION_DEFAULT_PRODUCER=None,_QUESTION_DEFAULT_INTENTS=None)
    source=ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/fleet_bridge.py').read_text())
    fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='_question_defaults_bootstrap')
    exec(compile(ast.Module([fn],type_ignores=[]),'ACTUAL_SOURCE_BOOTSTRAP','exec'),ns)
    ns['_question_defaults_bootstrap']()
    questions=[{'title':f'Full SYNTHETIC scope and risks {i}','options':[f'Inspect original {i}',f'Approve complete bounded {i}']} for i in range(1,count+1)]
    now=time.time()
    value={'version':1,'author':intents.AUTHOR,'bot':'backend','intent_id':'a'*32,
           'created_at':now,'expires_at':now+300,'process_id':321,
           'binding':{'work':'work','display':'group:fleet-product','thread':'thread','turn':'turn',
                      'runtime':ns['_question_runtime_binding'](),'protocol':q.PROTOCOL},
           'questions':questions,'defaults':[{'header':f'Q{i}','optionIndex':1,'label':v['options'][1],
                                             'role':'approval' if i%2 else 'choice'} for i,v in enumerate(questions,1)]}
    return ns,frames,logs,value


@pytest.mark.parametrize('count',[1,2,3])
def test_actual_author_input_bootstrap_stdout_loop_emits_one_bound_declaration(monkeypatch,tmp_path,count):
    ns,frames,logs,value=setup(monkeypatch,tmp_path,count);before=copy.deepcopy(value)
    path=intents.author_intent(tmp_path,bot='backend',value=value)
    proc=old.SyntheticProcess([old.completed(value['questions']),old.normal_end()]);proc.pid=321
    launches=[]
    async def synthetic_launch(*a,**kw):launches.append((a,kw));return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',synthetic_launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC original prompt'))
    asks=[f for f in frames if f.get('ev')=='ask'];assert len(asks)==1
    card=next(iter(ns['CODEX_QUESTIONS'].cards.values()));frame=asks[0]
    assert frame['question_ui_defaults']['defaults']==before['defaults']
    assert frame['question_binding']==card.wire_binding() and card.state=='pending' and card.answer==card.user==''
    assert frame['expires_at']-frame['created_at']==600 and value==before
    assert not path.exists() and (path.parent/('used.'+value['intent_id']+'.json')).exists()
    assert len(launches)==1 and not any(w.get('method')=='turn/steer' for w in proc.writes)
    assert not any('human_answer_received' in line or 'sent_same_thread' in line for line in logs)
    # Existing first-seen Registry item and consumed intent cannot emit again.
    origin=card.origin;ns['_observe_question_event'](origin,old.completed(value['questions']),process=proc,current=True)
    assert len([f for f in frames if f.get('ev')=='ask'])==1


def test_no_owner_declaration_legacy_stdout_is_unchanged(monkeypatch,tmp_path):
    ns,frames,_,value=setup(monkeypatch,tmp_path)
    proc=old.SyntheticProcess([old.completed(),old.normal_end()]);proc.pid=321
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','SYNTHETIC legacy prompt'))
    asks=[f for f in frames if f.get('ev')=='ask'];assert len(asks)==1 and 'question_ui_defaults' not in asks[0]
    assert not (tmp_path/intents.DIRECTORY).exists()


@pytest.mark.parametrize('change',['bool_pid','expired','future','role','partial','label','reordered','author','extra','type'])
def test_author_closed_input_refuses_without_directory_or_defaults(monkeypatch,tmp_path,change):
    _,_,_,v=setup(monkeypatch,tmp_path,2)
    if change=='bool_pid':v['process_id']=True
    elif change=='expired':v['created_at']-=601;v['expires_at']-=601
    elif change=='future':v['created_at']+=600;v['expires_at']+=600
    elif change=='role':v['defaults'][0]['role']='Recommended'
    elif change=='partial':v['defaults'].pop()
    elif change=='label':v['defaults'][0]['label']='Approve prose, not original'
    elif change=='reordered':v['defaults'].reverse()
    elif change=='author':v['author']='client'
    elif change=='extra':v['selected']=True
    else:v['binding']['turn']=False
    with pytest.raises(ValueError):intents.author_intent(tmp_path,bot='backend',value=v)
    assert not (tmp_path/intents.DIRECTORY).exists()


@pytest.mark.parametrize('change',['pid','turn','runtime','questions','replay','rpc','normal','seen','mode','symlink','ambiguous'])
def test_unknown_or_stale_owner_intent_cannot_annotate_original(monkeypatch,tmp_path,change):
    ns,frames,_,v=setup(monkeypatch,tmp_path)
    path=intents.author_intent(tmp_path,bot='backend',value=v)
    proc=old.SyntheticProcess([]);proc.pid=321
    b=v['binding'];origin=q.Origin(q.Binding(*(b[k] for k in intents.BINDING),'connection'),proc)
    # Set actual ordinary current guard values for direct current-origin observer.
    ns['CODEX_PROCS']['work']=proc;ns['CODEX_TURNS']['work']=ns['_CodexActiveTurn'](proc,'thread','turn')
    event=old.completed(v['questions'])
    if change=='pid':proc.pid=322
    elif change=='turn':event['params']['turnId']='other'
    elif change=='runtime':origin.binding=q.Binding(b['work'],b['display'],b['thread'],b['turn'],'other',q.PROTOCOL,'connection')
    elif change=='questions':event['params']['item']['questions'][0]['title']='Different full original'
    elif change=='replay':event['replay']=True
    elif change=='rpc':event['id']=3
    elif change=='normal':origin.normal=True
    elif change=='seen':origin.seen.add('item')
    elif change=='mode':path.chmod(0o600)
    elif change=='symlink':path.unlink();path.symlink_to(tmp_path/'missing')
    else:
        other=copy.deepcopy(v);other['intent_id']='b'*32;intents.author_intent(tmp_path,bot='backend',value=other)
    ns['_observe_question_event'](origin,event,process=proc,current=True)
    assert all('question_ui_defaults' not in f for f in frames)
    assert not list(path.parent.glob('used.*'))


def test_post_claim_possible_write_failure_no_legacy_fallback(monkeypatch,tmp_path):
    ns,frames,_,v=setup(monkeypatch,tmp_path);path=intents.author_intent(tmp_path,bot='backend',value=v)
    proc=old.SyntheticProcess([]);proc.pid=321;b=v['binding']
    origin=q.Origin(q.Binding(*(b[k] for k in intents.BINDING),'connection'),proc)
    ns['CODEX_PROCS']['work']=proc;ns['CODEX_TURNS']['work']=ns['_CodexActiveTurn'](proc,'thread','turn')
    calls=[]
    def unknown(*a,**kw):calls.append(kw);raise RuntimeError('SYNTHETIC possible emitter write')
    monkeypatch.setattr(ns['_QUESTION_DEFAULT_PRODUCER'],'admit_prepared_question',unknown)
    ns['_observe_question_event'](origin,old.completed(v['questions']),process=proc,current=True)
    assert len(calls)==1 and not frames and not path.exists()
    ns['_observe_question_event'](origin,old.completed(v['questions']),process=proc,current=True)
    # A repeated event could otherwise fall through to legacy issuance. A claimed
    # owner event must be marked seen even when the emitter's disposition is unknown.
    assert len(calls)==1 and not frames


def test_unknown_bot_bootstrap_allocates_no_owner_or_inbox(monkeypatch,tmp_path):
    ns,_,_,_=setup(monkeypatch,tmp_path);ns['_BOT_NAME']=None
    ns['_QUESTION_DEFAULT_PRODUCER']=ns['_QUESTION_DEFAULT_INTENTS']=None
    ns['_question_defaults_bootstrap']()
    assert ns['_QUESTION_DEFAULT_PRODUCER'] is ns['_QUESTION_DEFAULT_INTENTS'] is None


def test_exact_production_loop_calls_owner_observer_before_security_handlers():
    src=(Path(__file__).parents[1]/'src/feishu_bridge/fleet_bridge.py').read_text();tree=ast.parse(src)
    fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_turn_codex')
    calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call)]
    observe=next(n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='_observe_question_event')
    security=next(n for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='feed' and ast.unparse(n.func.value)=='approvals')
    assert observe.lineno<security.lineno
    assert ast.unparse(observe.args[0])=='question_origin' and ast.unparse(observe.args[1])=='ev'
    assert ast.unparse(next(k.value for k in observe.keywords if k.arg=='current'))=='CODEX_PROCS.get(chat_id) is proc and CODEX_TURNS.get(chat_id) is control'


def test_claim_directory_sync_failure_retains_record_and_never_emits_or_falls_back(monkeypatch,tmp_path):
    ns,frames,logs,value=setup(monkeypatch,tmp_path)
    intents.author_intent(tmp_path,bot='backend',value=value)
    origin=q.Origin(q.Binding(**value['binding'],connection='SYNTHETIC'),type('SyntheticProcess',(),{'pid':321})())
    def failed_sync(root):raise OSError('SYNTHETIC directory persistence refused')
    monkeypatch.setattr(intents,'_sync_root',failed_sync)
    inbox=ns['_QUESTION_DEFAULT_INTENTS']
    assert inbox.try_event(old.completed(value['questions']),origin=origin,process=origin.process,current=True,
        adapter=ns['_QUESTION_DEFAULT_PRODUCER'],owner=ns['_QUESTION_DEFAULT_OWNER']) is True
    assert origin.seen=={'item'} and not frames
    assert (tmp_path/intents.DIRECTORY/('used.'+value['intent_id']+'.json')).exists()

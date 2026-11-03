"""Actual stdout wrapper/Registry SOURCE size conservation, no live process or user."""
import ast, copy, json
from pathlib import Path
from types import SimpleNamespace
import pytest
from feishu_bridge import codex_questions as q
from feishu_bridge import question_defaults_intents as intents
from feishu_bridge import question_defaults_producer as producer
import test_question_defaults_producer as base
from test_question_defaults_producer import current_storage, storage_fixture


def questions(length=4900, oversized=False):
    return [{'title':str(i)+'SYNTHETIC complete question '+('x'*length),
             'options':([str(j)+('a'*1020) for j in range(8)] if oversized else
                        [str(i)+'A'+('a'*498),str(i)+'B'+('b'*498)])} for i in (1,2,3)]


def harness(tmp_path, values):
    h=base.setup(); h.process=SimpleNamespace(pid=321); h.origin.process=h.process
    h.ns['CODEX_PROCS']['work']=h.process;h.ns['CODEX_TURNS']['work'].proc=h.process
    h.data=base.inputs(h,3,item='SYNTHETIC-item');h.data['questions']=copy.deepcopy(values)
    h.data['defaults']=[{'header':f'Q{i}','optionIndex':1,'label':v['options'][1],'role':'approval'}
                       for i,v in enumerate(values,1)]
    h.value={'version':1,'author':intents.AUTHOR,'bot':'backend','intent_id':'c'*32,
             'created_at':1000.,'expires_at':1300.,'process_id':321,
             'binding':{k:getattr(h.origin.binding,k) for k in intents.BINDING},
             'questions':copy.deepcopy(values),'defaults':copy.deepcopy(h.data['defaults'])}
    h.inbox=intents.OwnerIntentInbox(tmp_path/intents.DIRECTORY,bot='backend',audit=h.audit.append,wall=lambda:h.wall[0])
    h.event={'method':'item/completed','params':{'threadId':'thread','turnId':'turn',
             'item':{'type':'agentMessage','id':'SYNTHETIC-item','questions':copy.deepcopy(values)}}}
    fn=next(n for n in ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/fleet_bridge.py').read_text()).body
            if isinstance(n,ast.FunctionDef) and n.name=='_observe_question_event')
    ns={'_QUESTION_DEFAULT_INTENTS':h.inbox,'_QUESTION_DEFAULT_PRODUCER':h.adapter,
        '_QUESTION_DEFAULT_OWNER':h.owner,'CODEX_QUESTIONS':h.reg}
    exec(compile(ast.Module([fn],type_ignores=[]),'ACTUAL_WRAPPER_SOURCE','exec'),ns)
    h.wrapper=ns['_observe_question_event']; h.legacy=[]; original=h.reg.observe
    def observed(*args,**kwargs):
        h.legacy.append((args,kwargs));return original(*args,**kwargs)
    h.reg.observe=observed
    h.dispatch=lambda:h.wrapper(h.origin,h.event,process=h.process,current=True)
    h.author=lambda:intents.author_intent(tmp_path,bot='backend',value=h.value,now=h.wall[0])
    return h


def assert_original(h,frame):
    assert frame['questions']==[{'header':f'Q{i}','question':v['title'],'multiSelect':False,
            'options':[{'label':s,'description':''} for s in v['options']]} for i,v in enumerate(h.value['questions'],1)]
    assert json.dumps(frame['questions'],ensure_ascii=False).encode()==json.dumps([
        {'header':f'Q{i}','question':v['title'],'multiSelect':False,'options':[{'label':s,'description':''} for s in v['options']]}
        for i,v in enumerate(h.value['questions'],1)],ensure_ascii=False).encode()
    card=next(iter(h.reg.cards.values()))
    assert frame['ask_id']==card.ask_id and frame['question_binding']==card.wire_binding()
    assert (card.item,card.created_at,card.expires_at,card.deadline)==('SYNTHETIC-item',1000.,1600.,610.)
    assert card.state=='pending' and card.answer==card.user==''
    assert len(h.frames)==1 and not any('human_answer_received' in s or 'sent_same_thread' in s for s in h.audit)


def test_valid_original_optional_overflow_delivers_full_default_free_original_once(tmp_path):
    h=harness(tmp_path,questions());path=h.author();raw=path.read_bytes()
    prepared=h.adapter.prepare_owner_question(h.owner,**h.data)
    assert prepared.status=='optional_size_refused' and len(prepared.frozen.encode())>q.MAX_CARD_BYTES
    assert not h.frames and not h.reg.cards and not h.origin.seen
    h.dispatch();assert len(h.legacy)==1 and 'question_ui_defaults' not in h.frames[0]
    assert_original(h,h.frames[0]);refused=path.parent/('refused.'+h.value['intent_id']+'.json')
    assert refused.read_bytes()==raw and not path.exists() and not list(path.parent.glob('used.*'))
    assert any('optional_size_original_fallback' in s for s in h.audit)
    h.dispatch();assert len(h.frames)==1 and len(h.legacy)==2  # Unchanged Registry seen guard, no retry emission.
    with pytest.raises(ValueError):h.author()


def test_ordinary_original_itself_oversized_keeps_unchanged_refusal(tmp_path):
    h=harness(tmp_path,questions(5900,True))
    assert not intents.validate_intent(h.value,bot='backend',now=1000.)
    h.dispatch();assert len(h.legacy)==1 and not h.frames and not h.reg.cards
    assert any('card_bytes_limit' in s for s in h.audit) and h.origin.seen=={'SYNTHETIC-item'}


def test_exact_near_limit_optional_frame_admitted_once_without_fallback(tmp_path):
    lo,hi=0,4900
    while lo<hi:
        mid=(lo+hi+1)//2;h=harness(tmp_path,questions(mid))
        draft=h.adapter.prepare_owner_question(h.owner,**h.data)
        if draft.status=='ready':lo=mid
        else:assert draft.status=='optional_size_refused';hi=mid-1
    h=harness(tmp_path,questions(lo));prepared=h.adapter.prepare_owner_question(h.owner,**h.data)
    assert prepared.status=='ready' and 0<=q.MAX_CARD_BYTES-len(prepared.frozen.encode())<6
    next_h=harness(tmp_path,questions(lo+1));assert next_h.adapter.prepare_owner_question(next_h.owner,**next_h.data).status=='optional_size_refused'
    h.author();h.dispatch();assert not h.legacy
    assert_original(h,h.frames[0]);assert h.frames[0]['question_ui_defaults']['defaults']==h.value['defaults']
    assert len(json.dumps(h.frames[0],ensure_ascii=False,separators=(',',':')).encode())<=q.MAX_CARD_BYTES
    h.dispatch();assert len(h.frames)==1 and len(h.legacy)==1  # Duplicate reaches unchanged Registry seen refusal, no emit.


def test_possible_transport_write_failure_has_zero_legacy_fallback(tmp_path):
    h=harness(tmp_path,questions(20));h.author();attempts=[]
    def unknown(frame):
        attempts.append(copy.deepcopy(frame))
        if frame['ev']=='ask':raise OSError('SYNTHETIC possible write')
    h.reg.emit=unknown;h.dispatch()
    assert len([v for v in attempts if v['ev']=='ask'])==1 and not h.legacy
    assert not h.reg.cards and h.origin.seen=={'SYNTHETIC-item'}
    h.dispatch();assert len(h.legacy)==1 and len([v for v in attempts if v['ev']=='ask'])==1


@pytest.mark.parametrize('overflow',[False,True])
@pytest.mark.parametrize('failure',['sync','link_after_write'])
def test_claim_io_uncertainty_has_zero_original_fallback(monkeypatch,tmp_path,overflow,failure):
    h=harness(tmp_path,questions(4900 if overflow else 20));path=h.author();raw=path.read_bytes()
    if failure=='sync':
        def fail(root):raise OSError('SYNTHETIC claimed directory sync failure')
        monkeypatch.setattr(intents,'_sync_root',fail)
    else:
        original=intents.os.link
        def fail(*args,**kwargs):original(*args,**kwargs);raise OSError('SYNTHETIC unknown link outcome')
        monkeypatch.setattr(intents.os,'link',fail)
    h.dispatch()
    assert not h.frames and not h.legacy and h.origin.seen=={'SYNTHETIC-item'}
    records=list(path.parent.glob(('refused.' if overflow else 'used.')+'*.json'))
    assert len(records)==1 and records[0].read_bytes()==raw
    assert any('owner_io_unknown' in s for s in h.audit)
    h.dispatch();assert len(h.legacy)==1 and not h.frames  # Existing seen refusal, not a fallback emission.


@pytest.mark.parametrize('change',['expired','runtime','disconnect','fresh_decision'])
def test_optional_overflow_postclaim_scope_change_cannot_fallback(monkeypatch,tmp_path,change):
    h=harness(tmp_path,questions());h.author();sync=intents._sync_root
    def changed(root):
        sync(root)
        if change=='expired':h.wall[0]=1600.;h.clock[0]=610.
        elif change=='runtime':h.current['runtime']='changed'
        elif change=='disconnect':h.origin.disconnected=True
        else:
            other=base.inputs(h,1,item='new-decision');assert h.adapter.emit_owner_question(h.owner,**other) is not None
    monkeypatch.setattr(intents,'_sync_root',changed);h.dispatch()
    assert not h.legacy and h.origin.seen=={'SYNTHETIC-item'} | ({'new-decision'} if change=='fresh_decision' else set())
    if change=='fresh_decision':assert len(h.frames)==1 and next(iter(h.reg.cards.values())).item=='new-decision'
    else:assert not h.frames


@pytest.mark.parametrize('change',['id','questions','timestamps','process','snapshot'])
def test_frozen_draft_identity_or_first_ttl_change_refuses_before_admission(tmp_path,change):
    h=harness(tmp_path,questions(20));draft=h.adapter.prepare_owner_question(h.owner,**h.data)
    if change=='id':draft.card.ask_id='codex-question-'+'0'*32
    elif change=='questions':draft.card.questions=tuple(list(draft.card.questions))
    elif change=='timestamps':draft.card.expires_at+=1
    elif change=='process':h.origin.process=SimpleNamespace(pid=321)
    else:draft=producer._PreparedQuestion(h.adapter,'ready',draft.card,draft.frozen,draft.binding,())
    assert h.adapter.admit_prepared_question(h.owner,draft) is None
    assert not h.frames and not h.reg.cards and not h.origin.seen
    assert h.adapter.optional_size_refusal(h.owner,None) is False

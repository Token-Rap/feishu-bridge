"""SYNTHETIC owned process/transport; actual Registry and producer SOURCE paths."""
import ast,copy,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from feishu_bridge.questions import codex_questions as q
from feishu_bridge.questions import question_defaults_producer as p
from feishu_bridge.questions import question_defaults_source as draft
from feishu_bridge.codex import fleet_codex_storage as storage
from codex_storage_fixture import storage_fixture, question_storage, header


@pytest.fixture(autouse=True)
def current_storage(storage_fixture):
    header(storage_fixture, "thread")
    return storage_fixture


def setup():
    frames,audit,clock,wall=[],[],[10.0],[1000.0]
    reg=q.Registry(frames.append,audit.append,clock=lambda:clock[0],wall=lambda:wall[0])
    process=object();origin=q.Origin(q.Binding('work','group:fleet-product','thread','turn','runtime',q.PROTOCOL,'connection'),process)
    current={'runtime':'runtime','model':'codex','session':'thread','routes':{'group:fleet-product':'work'}}
    fixture=question_storage(pytest.MonkeyPatch(), {"work":"thread"})
    ns={'_codex_storage':storage,'_fs':fixture.fs,'codex_questions':q,'ROLLOVER_HOLD':set(),'IDLE_ACTIVE':set(),
        '_runtime_snapshot':lambda:(None,None,current['model']),
        '_question_runtime_binding':lambda:current['runtime'],
        '_session_chat_key':lambda work,model:work,'_load_session':lambda key:current['session'],
        '_BOT_NAME':'backend','CODEX_PROCS':{'work':process},
        'CODEX_TURNS':{'work':SimpleNamespace(proc=process,thread_id='thread',turn_id='turn')},
        'APP_FRONT':SimpleNamespace(resolve_work_display_ex=lambda want:(current['routes'].get(want),want,''))}
    src=ast.parse((Path(__file__).parents[1]/'src/feishu_bridge/bridge/fleet_bridge.py').read_text())
    fn=next(n for n in src.body if isinstance(n,ast.FunctionDef) and n.name=='_question_valid')
    exec(compile(ast.Module([fn],type_ignores=[]),'ACTUAL_SOURCE_VALID_GUARD','exec'),ns)
    owner=object();adapter=p.BridgeOwnedQuestionProducer(reg,owner=owner,bot='backend',valid=ns['_question_valid'])
    return SimpleNamespace(reg=reg,frames=frames,audit=audit,clock=clock,wall=wall,process=process,origin=origin,current=current,ns=ns,owner=owner,adapter=adapter)


def inputs(h,count=1,item='owner-item'):
    questions=[{'title':f'Full SYNTHETIC question {i} scope and risk',
                'options':[f'Keep original full option {i}/0',f'Complete bounded option {i}/1']} for i in range(1,count+1)]
    defaults=[{'header':f'Q{i}','optionIndex':i%2,'label':questions[i-1]['options'][i%2],
               'role':'approval' if i%2 else 'choice'} for i in range(1,count+1)]
    return dict(capability=p.CAPABILITY,origin=h.origin,process=h.process,
                source_binding={k:getattr(h.origin.binding,k) for k in p.BINDING_FIELDS},
                item_id=item,questions=questions,defaults=defaults)


@pytest.mark.parametrize('count',[1,2,3])
def test_public_creation_atomically_emits_immutable_original_default(count):
    h=setup();data=inputs(h,count);original=copy.deepcopy((data['questions'],data['defaults'],data['source_binding']))
    card=h.adapter.emit_owner_question(h.owner,**data);assert card is not None and h.reg.cards[card.ask_id] is card
    assert len(h.frames)==1;frame=h.frames[0];declaration=frame['question_ui_defaults']
    assert declaration['basis']==draft.original_basis(frame,now=h.wall[0])
    assert declaration['defaults']==data['defaults'];assert frame['question_binding']==card.wire_binding()
    assert card.state=='pending' and card.answer==card.user==''
    assert (card.created_at,card.expires_at,card.deadline)==(1000.,1600.,610.)
    assert card.ask_id.startswith(q.PREFIX) and len(card.ask_id)==len(q.PREFIX)+32
    assert (data['questions'],data['defaults'],data['source_binding'])==original
    data['defaults'][0]['label']='MODIFIED OWNER INPUT AFTER EMISSION'
    replay=h.adapter.replay_metadata(h.owner,card.ask_id);assert replay==frame
    replay['question_ui_defaults']['defaults'][0]['label']='MODIFIED READ COPY'
    assert h.adapter.replay_metadata(h.owner,card.ask_id)==frame and len(h.frames)==1
    assert h.adapter.emit_owner_question(h.owner,**inputs(h,count)) is None
    assert not any('human_answer_received' in s or 'sent_same_thread' in s for s in h.audit)


def test_private_owner_identity_not_capability_string_or_forged_transport_fields():
    h=setup();data=inputs(h)
    for owner in ({'from':'backend','kind':'report','current':True},'backend',object(),None):
        assert h.adapter.emit_owner_question(owner,**data) is None
    data['capability']=draft.CAPABILITY;assert h.adapter.emit_owner_question(h.owner,**data) is None
    data['capability']='codex-0.159.2-defaults';assert h.adapter.emit_owner_question(h.owner,**data) is None
    assert not h.frames and not h.reg.cards


@pytest.mark.parametrize('field',['work','display','thread','turn','runtime','protocol','connection'])
def test_full_original_binding_mismatch_refuses(field):
    h=setup();data=inputs(h);data['source_binding'][field]='different'
    assert h.adapter.emit_owner_question(h.owner,**data) is None and not h.frames


def test_current_public_origin_guard_refuses_stale_process_runtime_route_session():
    for reason in ('process','runtime','route','session','model'):
        h=setup();data=inputs(h)
        if reason=='process':data['process']=object()
        elif reason=='runtime':h.current['runtime']='changed'
        elif reason=='route':h.current['routes']['group:fleet-product']='otherwork'
        elif reason=='session':h.current['session']='newthread'
        else:h.current['model']='claude'
        assert h.adapter.emit_owner_question(h.owner,**data) is None and not h.reg.cards


def test_missing_partial_reordered_malformed_default_declarations_refuse():
    for change in ('missing','partial','reordered','bool_index','wrong_label','unknown_role','extra','gapped'):
        h=setup();data=inputs(h,2)
        if change=='missing':data['defaults']=None
        elif change=='partial':data['defaults'].pop()
        elif change=='reordered':data['defaults'].reverse()
        elif change=='bool_index':data['defaults'][0]['optionIndex']=True
        elif change=='wrong_label':data['defaults'][0]['label']='approval prose is not identity'
        elif change=='unknown_role':data['defaults'][0]['role']='recommended'
        elif change=='extra':data['defaults'][0]['selected']=True
        else:data['defaults'][0]['header']='Q3'
        assert h.adapter.emit_owner_question(h.owner,**data) is None and not h.frames


def test_legacy_actual_registry_parser_has_no_defaults_and_rejects_extra_question_keys():
    h=setup();event={'method':'item/completed','params':{'threadId':'thread','turnId':'turn','item':{'type':'agentMessage','id':'legacy-item','questions':inputs(h)['questions'],'question_ui_defaults':{'forged':'metadata'}}}}
    card=h.reg.observe(h.origin,event,process=h.process,current=True);assert card is not None
    assert 'question_ui_defaults' not in h.frames[0] and card.answer==''
    h=setup();event['params']['item']['questions'][0]['defaultIndex']=1
    assert h.reg.observe(h.origin,event,process=h.process,current=True) is None
    assert not any(f.get('ev')=='ask' for f in h.frames)


def test_origin_terminal_disconnected_and_revoked_creation_never_restores():
    for flag in ('normal','disconnected','failed'):
        h=setup();setattr(h.origin,flag,True)
        assert h.adapter.emit_owner_question(h.owner,**inputs(h)) is None and not h.frames
    h=setup();h.ns['ROLLOVER_HOLD'].add('work')
    assert h.adapter.emit_owner_question(h.owner,**inputs(h)) is None


def test_expiry_and_terminal_metadata_no_replay_or_renewal():
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h))
    h.wall[0]+=300;h.clock[0]+=300
    assert h.adapter.replay_metadata(h.owner,card.ask_id)['expires_at']==1600.
    h.clock[0]=610.;assert h.adapter.replay_metadata(h.owner,card.ask_id) is None
    assert card.state=='cancelled' and h.adapter.emit_owner_question(h.owner,**inputs(h)) is None
    for state in ('received','queued','dispatching','cancelled','delivery_failed_or_unknown'):
        h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h));card.state=state
        assert h.adapter.replay_metadata(h.owner,card.ask_id) is None and len(h.frames)==1


def test_normal_origin_completion_retains_original_but_disconnect_gives_no_new_metadata():
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h));h.reg.finish(h.origin,normal=True)
    assert h.adapter.replay_metadata(h.owner,card.ask_id)['ask_id']==card.ask_id
    assert h.adapter.emit_owner_question(h.owner,**inputs(h,item='later')) is None
    h.reg.disconnect(h.origin,expected=True);assert h.adapter.replay_metadata(h.owner,card.ask_id) is None
    assert h.reg.cards[card.ask_id] is card and card.state=='pending'


def test_changed_original_binding_or_registry_card_cannot_rebind_cached_defaults():
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h))
    card.origin=q.Origin(h.origin.binding,h.process)
    assert h.adapter.replay_metadata(h.owner,card.ask_id) is None
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h))
    b=h.origin.binding;h.origin.binding=q.Binding(b.work,b.display,b.thread,b.turn,b.runtime,b.protocol,'newconnection')
    assert h.adapter.replay_metadata(h.owner,card.ask_id) is None
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h));card.questions=tuple(list(card.questions))
    assert h.adapter.replay_metadata(h.owner,card.ask_id) is None
    h=setup();card=h.adapter.emit_owner_question(h.owner,**inputs(h));h.reg.cards[card.ask_id]=copy.copy(card)
    assert h.adapter.replay_metadata(h.owner,card.ask_id) is None


def test_final_admission_guard_refuses_binding_change_between_checks():
    h=setup();original=h.adapter.valid;calls=[0]
    def guard(card):
        calls[0]+=1
        if calls[0]==2:
            b=h.origin.binding;h.origin.binding=q.Binding(b.work,b.display,b.thread,b.turn,b.runtime,b.protocol,'changed')
        return original(card)
    h.adapter.valid=guard
    assert h.adapter.emit_owner_question(h.owner,**inputs(h)) is None and not h.reg.cards


def test_same_work_existing_card_held_other_work_independent():
    h=setup();first=h.adapter.emit_owner_question(h.owner,**inputs(h))
    assert h.adapter.emit_owner_question(h.owner,**inputs(h,item='second')) is None
    assert first.state=='pending' and len(h.frames)==1
    process=object();h.process=process;h.origin=q.Origin(q.Binding('other','group:other','thread','turn','runtime',q.PROTOCOL,'otherconnection'),process)
    rows=json.loads(h.ns['_fs'].SESSIONS_FILE.read_text());rows['other']=copy.deepcopy(rows['work'])
    h.ns['_fs'].write_sessions(rows)
    h.current['routes']['group:other']='other';h.ns['CODEX_PROCS']['other']=process;h.ns['CODEX_TURNS']['other']=SimpleNamespace(proc=process,thread_id='thread',turn_id='turn')
    assert h.adapter.emit_owner_question(h.owner,**inputs(h,item='other-item')) is not None
    assert len(h.reg.cards)==2 and first.state=='pending'


def test_transport_emission_failure_no_retry_or_answer_and_full_size_guard():
    h=setup();attempts=[]
    def fail(frame):
        attempts.append(frame)
        if frame['ev']=='ask':raise OSError('SYNTHETIC transport refusal')
    h.reg.emit=fail
    assert h.adapter.emit_owner_question(h.owner,**inputs(h)) is None
    assert h.adapter.emit_owner_question(h.owner,**inputs(h)) is None
    assert len([f for f in attempts if f['ev']=='ask'])==1 and not h.reg.cards
    h=setup();data=inputs(h,3)
    for i,question in enumerate(data['questions']):
        question['title']='Full'+('x'*5990);question['options']=[str(j)+'x'*1020 for j in range(8)]
        d=data['defaults'][i];d['label']=question['options'][d['optionIndex']]
    assert h.adapter.emit_owner_question(h.owner,**data) is None and not h.frames

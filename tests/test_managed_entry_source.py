"""Real frontend/entry/coordinator and extracted actual bridge seams only.

Fresh private synthetic stores, fake turns and in-memory writers. No product
native/config/account/model or current user history/cache is used.
"""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app_frontend as front
import fleet_managed_entry as entry
import fleet_managed_ingress as ingress
import fleet_managed_runtime as runtime
import fleet_target_control as control

TEXT = " \ufeffe\u0301 中文👩‍💻\r\n" + "完整原文\n" * 180 + "  "


class Writer:
    def __init__(self):
        self.frames = []
    def write(self, value):
        self.frames.append(json.loads(value))


@pytest.fixture
def bound(tmp_path):
    tmp_path.chmod(0o700)
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    c = control.create(tmp_path / "control", str(home), "synthetic-selector")
    release = {"release_id": "synthetic-release", "closure_sha256": "a" * 64, "namespace": "synthetic-ns"}
    epoch, op = c.enter_maintenance(0, "synthetic-operator")
    startup = c.fork_startup(op, "synthetic-startup", release)
    c.register_runtime(startup, release); c.finish_startup(startup)
    epoch = c.finish_maintenance(epoch, op); c.reopen(epoch, release, runtime_ready=True)
    r = runtime.Runtime(ingress.Selection(tmp_path / "control", "synthetic-selector", home))
    f = front.AppFrontend(home, lambda _: None)
    f.chat_id = "native-fixture-chat"
    f.set_chat_validator(lambda chat: (chat == "group:fixture", "fixture-rejected"))
    writer = Writer(); f._gateway_writers.add(writer)
    calls, events, jobs = [], [], []
    f.broadcast = lambda event, *, _managed_entry=None: events.append(event)
    def spawn(coro):
        task = asyncio.create_task(coro); jobs.append(task)
    f._spawn = spawn
    async def turn(*args):
        calls.append(args)
    state = {"engine": "codex", "busy": False, "terminal": None}
    def session_key(chat, engine):
        return chat + "::codex" if engine == "codex" else chat
    e = entry.Entry(r, f, bot="shopping", engine="codex", session_key=session_key,
        current_engine=lambda: state["engine"], turn=turn,
        stamp=lambda text, at, display: (text, at, display),
        busy=lambda chat: state["busy"], terminal=lambda scope: state["terminal"])
    f._managed_ingress = e.handle_frame
    frame = {"type": "user_message", "text": TEXT, "client": "ios.744ccca",
             "work_id": "gateway-synthetic-id", "work_actor": "authenticated-outer-user",
             "from": "UpperCase Display Actor", "chat_id": "group:fixture",
             "message_origin": {"version": 1, "kind": "gateway_human"}}
    def reserve(req=frame):
        prepared, scope = r.prepare(req, work_chat=e.route.work_chat,
            display_chat="group:fixture", engine="codex", bot="shopping", gateway_connection=True)
        r.admit(prepared, scope)
        return prepared, scope
    return SimpleNamespace(c=c,r=r,f=f,e=e,writer=writer,calls=calls,events=events,
                           jobs=jobs,state=state,frame=frame,reserve=reserve)


async def deliver(b, frame=None):
    await b.f._handle_frame(frame or b.frame, b.writer)
    return await asyncio.gather(*b.jobs, return_exceptions=True)


def test_full_original_payload_identity_and_route_survive_real_frontend(bound):
    b=bound; req, scope=b.reserve()
    result=asyncio.run(deliver(b))
    assert len(result)==1 and isinstance(result[0], ingress.Refused)
    assert str(result[0])=="managed_lifecycle_unknown"  # Product terminal proof is not present.
    assert b.writer.frames==[{"type":"inject_ack", "chat_id":"group:fixture"}]
    assert len(b.calls)==1 and b.calls[0][0]=="native-fixture-chat"
    text, at, display=b.calls[0][1]
    assert text.encode()==TEXT.encode() and at>0 and display=="group:fixture"
    ev=b.events[0]
    assert ev["text"].encode()==TEXT.encode() and ev["work_id"]==req.work_id
    assert ev["work_chat"]=="native-fixture-chat::codex" and ev["chat_id"]=="group:fixture"
    assert ev["work_actor"]=="authenticated-outer-user" and ev["client"]=="ios.744ccca"
    assert ev["from"]==ev["user"]=="UpperCase Display Actor"
    assert b.c.snapshot()["state"]=="UNKNOWN" and b.c.snapshot()["unresolved"]==1
    assert front.CUR_CHAT.get() == "" and front.DISPLAY_CHAT.get() == ""
    assert runtime.CURRENT.get() is None


def test_fixture_only_positive_terminal_settles_exact_new_request(bound):
    b=bound; b.reserve(); b.state["terminal"]="completed"
    assert asyncio.run(deliver(b))==[None]
    assert b.c.snapshot()["unresolved"]==0 and b.c.snapshot()["active_leases"]==0
    assert len(b.calls)==len(b.events)==1


def test_no_reservation_cannot_ack_spawn_or_reach_turn(bound):
    b=bound
    with pytest.raises(control.Refused, match="managed_reservation_unknown"):
        asyncio.run(deliver(b))
    assert not b.writer.frames and not b.jobs and not b.events and not b.calls


@pytest.mark.parametrize("key,value",[("text", TEXT+"changed"), ("client","web.changed"),
    ("work_actor","another-actor"), ("from","Changed Case"), ("work_id","other-id")])
def test_durable_original_scope_mismatch_refuses_before_ack(bound,key,value):
    b=bound; b.reserve(); altered=dict(b.frame); altered[key]=value
    with pytest.raises(control.Refused, match="managed_reservation_unknown"):
        asyncio.run(deliver(b,altered))
    assert not b.writer.frames and not b.jobs and not b.events and not b.calls
    assert b.c.snapshot()["queued"]==1


@pytest.mark.parametrize("kind",["answer","user_image","set_runtime","command","interrupt",
                                 "term_open","term_input","group_event"])
def test_other_producers_controls_have_no_legacy_fallback(bound,kind):
    b=bound; b.reserve(); req=dict(b.frame,type=kind)
    b.f._inject=lambda *a: pytest.fail("legacy fallback")
    with pytest.raises(ingress.Refused,match="managed_frame_unsupported"):
        asyncio.run(deliver(b,req))
    assert not b.writer.frames and not b.jobs and not b.calls


def test_unqualified_connection_or_group_never_uses_private_fallback(bound):
    b=bound; b.reserve(); b.f._gateway_writers.clear()
    with pytest.raises(ingress.Refused,match="managed_origin_unknown"):
        asyncio.run(deliver(b))
    b.f._gateway_writers.add(b.writer)
    with pytest.raises(ingress.Refused,match="managed_display_unknown"):
        asyncio.run(deliver(b,dict(b.frame,chat_id="group:unqualified")))
    assert not b.writer.frames and not b.jobs and not b.calls


@pytest.mark.parametrize("drift",["engine","native","map_key"])
def test_bound_actual_native_engine_map_derivation_is_rechecked(bound,drift):
    b=bound; b.reserve()
    if drift=="engine": b.state["engine"]="claude"
    elif drift=="native": b.f.chat_id="other-chat"
    else: b.e._session_key=lambda *a: "another-map-row"
    with pytest.raises(ingress.Refused,match="managed_entry_binding_drift"):
        asyncio.run(deliver(b))
    assert not b.writer.frames and not b.jobs and not b.calls


def test_drain_before_receive_keeps_exact_queue_without_ack_or_replay(bound):
    b=bound; req,_=b.reserve(); b.c.begin_drain(b.c.snapshot()["epoch"])
    with pytest.raises(control.Refused,match="admission_closed"):
        asyncio.run(deliver(b))
    assert not b.writer.frames and not b.jobs and not b.calls
    with b.c.tx() as db:
        assert db.execute("SELECT payload,state FROM work WHERE work_id=?",(req.work_id,)).fetchone()==(req.payload,"QUEUED")


def test_drain_after_route_ack_does_not_dispatch_or_drop_full_payload(bound):
    b=bound; req,_=b.reserve()
    async def scenario():
        await b.f._handle_frame(b.frame,b.writer)
        b.c.begin_drain(b.c.snapshot()["epoch"])
        return await asyncio.gather(*b.jobs,return_exceptions=True)
    result=asyncio.run(scenario())
    assert isinstance(result[0],control.Refused) and not b.calls and not b.events
    assert b.writer.frames==[{"type":"inject_ack","chat_id":"group:fixture"}]
    assert b.c.snapshot()["queued"]==1 and b.c.snapshot()["active_leases"]==0


def test_existing_legacy_busy_state_cannot_merge_reset_or_steer(bound):
    b=bound; b.reserve(); b.state["busy"]=True
    result=asyncio.run(deliver(b))
    assert isinstance(result[0],ingress.Refused) and str(result[0])=="managed_legacy_work_unsettled"
    assert not b.calls and b.c.snapshot()["unresolved"]==1 and b.c.snapshot()["state"]=="UNKNOWN"


def test_current_request_cannot_skip_or_boot_replay_older_queue(bound):
    b=bound; older=dict(b.frame,work_id="older-retained")
    b.reserve(older); req,_=b.reserve(); b.state["terminal"]="completed"
    result=asyncio.run(deliver(b))
    assert isinstance(result[0],control.Refused) and str(result[0])=="claim_not_queued_head"
    assert not b.calls and not b.events and b.c.snapshot()["queued"]==2


def test_duplicate_frame_cannot_dispatch_or_mirror_twice(bound):
    b=bound; b.reserve(); b.state["terminal"]="completed"
    async def twice():
        await b.f._handle_frame(b.frame,b.writer)
        await b.f._handle_frame(b.frame,b.writer)
        return await asyncio.gather(*b.jobs,return_exceptions=True)
    result=asyncio.run(twice())
    assert len(b.calls)==len(b.events)==1 and result[0] is None
    assert isinstance(result[1],control.Refused)
    assert b.c.snapshot()["unresolved"]==0


def extract(name):
    tree=ast.parse((Path(__file__).parents[1]/"fleet_bridge.py").read_text())
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
    return ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[]))


@pytest.mark.parametrize("name,args,method",[("_app_inject",(TEXT,"ios.744ccca"),"inject"),
    ("handle_message",("native",TEXT,None),"handle_message")])
def test_actual_bridge_seam_short_circuits_all_legacy_side_effects(name,args,method):
    calls=[]
    async def managed(*values): calls.append(values); return "typed-dispatch"
    ns={"_MANAGED_ENTRY":SimpleNamespace(**{method:managed})}
    exec(compile(extract(name),"fleet_bridge.py","exec"),ns)
    assert asyncio.run(ns[name](*args))=="typed-dispatch" and calls==[args]
    # All missing legacy globals would raise if trim/mirror/queue/reset ran.


def test_actual_run_turn_checks_exact_entry_before_question_or_native_branch():
    class Entry:
        def require_turn(self,*a): raise ingress.Refused("synthetic-turn-refusal")
    ns={"_MANAGED_ENTRY":Entry()}
    exec(compile(extract("run_turn"),"fleet_bridge.py","exec"),ns)
    with pytest.raises(ingress.Refused,match="synthetic-turn-refusal"):
        asyncio.run(ns["run_turn"]("native",TEXT))


def test_unbound_startup_guard_and_real_start_callback_dependency_are_retained(tmp_path):
    import fleet_managed_bootstrap
    with pytest.raises(ingress.Refused,match="managed_full_lifecycle_unbound"):
        fleet_managed_bootstrap.require_startup_binding({"FLEET_TARGET_CONTROL_REQUIRED":"1",
            "FLEET_TARGET_CONTROL_DIR":str(tmp_path), "FLEET_TARGET_CONTROL_ID":"fixture", "FLEET_BRIDGE_HOME":str(tmp_path)},[])
    tree=extract("amain")
    call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call)
              and isinstance(n.func,ast.Attribute) and n.func.attr=="start"
              and isinstance(n.func.value,ast.Name) and n.func.value.id=="APP_FRONT")
    assert any(k.arg=="managed_ingress" for k in call.keywords)


def test_private_bot_display_derives_native_work_without_group_lookup(bound):
    b=bound; req=dict(b.frame,chat_id="shopping")
    prepared,scope=b.r.prepare(req,work_chat=b.e.route.work_chat,display_chat="shopping",
        engine="codex",bot="shopping",gateway_connection=True)
    b.r.admit(prepared,scope); b.state["terminal"]="completed"
    b.f.set_chat_validator(lambda chat: pytest.fail("Private bot display must not use group lookup"))
    assert asyncio.run(deliver(b,req))==[None]
    assert b.writer.frames==[{"type":"inject_ack","chat_id":"shopping"}]
    assert b.calls[0][0]=="native-fixture-chat" and b.calls[0][1][2]=="shopping"
    assert b.events[0]["chat_id"]=="shopping"
    assert b.events[0]["work_chat"]=="native-fixture-chat::codex"
    assert b.events[0]["text"].encode()==TEXT.encode()


@pytest.mark.parametrize("display",[None,True,0,[],{},""," shopping","shopping ",
    "native-fixture-chat","group:fixture "," group:fixture"])
def test_original_display_shape_and_exact_identity_cannot_normalize_or_fallback(bound,display):
    b=bound; b.reserve()
    with pytest.raises(ingress.Refused,match="managed_display_unknown"):
        asyncio.run(deliver(b,dict(b.frame,chat_id=display)))
    assert not b.writer.frames and not b.jobs and not b.events and not b.calls
    assert b.c.snapshot()["queued"]==1


@pytest.mark.parametrize("field",["engine","map_key","native"])
def test_binding_callbacks_cannot_use_equality_coercion(bound,field):
    b=bound; b.reserve()
    class EqualToAnything:
        def __eq__(self,other): pytest.fail("Binding must reject original shape before equality")
    if field=="engine": b.e._current_engine=lambda: EqualToAnything()
    elif field=="map_key": b.e._session_key=lambda *a: EqualToAnything()
    else: b.f.chat_id=EqualToAnything()
    with pytest.raises(ingress.Refused,match="managed_entry_binding_drift"):
        asyncio.run(deliver(b))
    assert not b.writer.frames and not b.jobs and not b.calls

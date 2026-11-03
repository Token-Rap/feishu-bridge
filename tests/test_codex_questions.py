"""Synthetic public source paths only: no bridge/SDK import or real model process."""
from __future__ import annotations
import ast
import asyncio
import contextlib
import contextvars
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
import tempfile
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feishu_bridge.questions import codex_questions as q
from feishu_bridge.codex import codex_approvals
from feishu_bridge.codex import codex_archived_resume
from feishu_bridge.bridge import app_frontend

ROOT = Path(__file__).resolve().parents[1]

def completed(questions=None, **changes):
    event = {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
             "item": {"type": "agentMessage", "id": "item", "text": "already streamed",
                      "questions": questions if questions is not None else [{"title": "Full scope and risks", "options": ["Approve ONE", "Inspect", "Decline"]}]}}}
    event.update(changes)
    return event


def registry():
    frames, audit, clock = [], [], [10.0]
    reg = q.Registry(frames.append, audit.append, clock=lambda: clock[0], wall=lambda: 1000.)
    proc = object()
    origin = q.Origin(q.Binding("work", "group:fleet-product", "thread", "turn", "runtime", q.PROTOCOL, "connection"), proc)
    return reg, origin, proc, frames, audit, clock


def make_card():
    reg, origin, proc, frames, audit, clock = registry()
    card = reg.observe(origin, completed(), process=proc, current=True)
    assert card is not None
    return reg, origin, proc, frames, audit, clock, card


def answer(card, **changes):
    req = {"type": "answer", "ask_id": card.ask_id, "chat_id": card.origin.binding.display,
           "answers": [{"header": h, "answer": opts[0]} for h, _, opts in card.questions],
           "question_answer": {"source": "gateway_authenticated_human", "user": "human",
                               "binding": card.wire_binding()}}
    req.update(changes)
    return req


def test_typed_completed_producer_and_duplicate_once():
    reg, origin, proc, frames, audit, clock = registry()
    for ev in [completed(id=2), completed(method="item/tool/requestUserInput"), completed(replay=True),
               {"method": "item/completed", "params": {"item": {"type": "mcpToolCall", "questions": []}}}]:
        assert reg.observe(origin, ev, process=proc, current=True) is None
    assert reg.observe(origin, completed(), process=object(), current=True) is None
    assert reg.observe(origin, completed(), process=proc, current=False) is None
    bad = completed(); bad["params"]["turnId"] = "other"
    assert reg.observe(origin, bad, process=proc, current=True) is None
    card = reg.observe(origin, completed(), process=proc, current=True)
    assert reg.observe(origin, completed(), process=proc, current=True) is None
    assert len(frames) == 1 and card.ask_id.startswith(q.PREFIX)
    assert card.origin.binding.connection == "connection"


def test_multi_question_mapping_uses_index_not_duplicate_titles():
    reg, origin, proc, frames, _, _ = registry()
    card = reg.observe(origin, completed([{"title": "Same title", "options": ["a", "b"]},
                                         {"title": "Same title", "options": ["c", "d"]}]), process=proc, current=True)
    req = answer(card); req["answers"].reverse()
    assert reg.claim(req, lambda _: True) is card
    assert "Q1 — Same title\n选择：a" in card.answer and "Q2 — Same title\n选择：c" in card.answer
    assert reg.claim(req, lambda _: True) is None


@pytest.mark.parametrize("questions", [[], [{"title": "private", "options": None}],
    [{"title": "private"}], [{"title": "private", "options": ["a", "a"]}],
    [{"title": "private", "options": ["a", " a "]}],
    [{"title": "private", "options": ["a", "b"], "isSecret": True}],
    [{"title": "x"*6001, "options": ["a", "b"]}],
    [{"title": "private", "options": ["a", "b"]}]*4])
def test_unsupported_shapes_no_body_or_default(questions):
    reg, origin, proc, frames, audit, _ = registry()
    assert reg.observe(origin, completed(questions), process=proc, current=True) is None
    assert not reg.cards and all(f["ev"] != "ask" for f in frames)
    assert "private" not in str(audit)+str(frames)


@pytest.mark.parametrize("mutation", ["unstamped", "nonhuman", "wrong_chat", "wrong_binding", "partial", "extra", "duplicate", "unknown_header", "default", "freeform", "ambiguous", "plain"])
def test_invalid_answer_never_claims(mutation):
    reg, _, _, _, _, _, card = make_card(); req = answer(card)
    if mutation == "unstamped": req.pop("question_answer")
    if mutation == "nonhuman": req["question_answer"]["source"] = "bot_injection"
    if mutation == "wrong_chat": req["chat_id"] = "group:ops"
    if mutation == "wrong_binding": req["question_answer"]["binding"]["item_id"] = "other"
    if mutation == "partial": req["answers"] = []
    if mutation in {"extra", "duplicate"}: req["answers"].append(dict(req["answers"][0]))
    if mutation == "unknown_header": req["answers"][0]["header"] = "Approval"
    if mutation == "default": req["answers"][0]["answer"] = ""
    if mutation == "freeform": req["text"] = "Approve ONE"
    if mutation == "ambiguous": req["answers"][0]["label"] = "Decline"
    if mutation == "plain": req["type"] = "user_message"
    assert reg.claim(req, lambda _: True) is None and card.state == "pending"


@pytest.mark.parametrize("reason", ["stopped", "session_reset", "control_changed", "runtime_changed", "unrelated_input", "new_turn", "gateway_disconnected"])
def test_cleanup_and_stale_no_restore(reason):
    reg, _, _, frames, _, _, card = make_card()
    reg.cancel_work("work", reason); reg.cancel_work("work", reason)
    assert reg.claim(answer(card), lambda _: True) is None
    assert len([f for f in frames if f["ev"] == "ask_cancel"]) == 1
    assert not q.Registry(frames.append, lambda _: None).cards


def test_expiry_first_creation_and_answer_races():
    reg, _, _, _, _, clock, card = make_card()
    deadline = card.deadline
    clock[0] = deadline
    assert reg.claim(answer(card), lambda _: True) is None
    reg, _, _, _, _, clock, card = make_card()
    assert reg.claim(answer(card), lambda _: True) is card
    assert reg.transition(card, "queued", lambda _: True)
    clock[0] = card.deadline
    assert not reg.transition(card, "dispatching", lambda _: True)
    reg, _, _, _, _, _, card = make_card()
    assert reg.claim(answer(card), lambda _: True) is card
    reg.cancel_work("work", "new_turn")
    assert not reg.transition(card, "dispatching", lambda _: True)


def test_normal_finish_expected_shutdown_only_and_conflict():
    reg, origin, proc, _, _, _, card = make_card()
    reg.finish(origin, normal=True); reg.disconnect(origin, expected=True)
    assert reg.claim(answer(card), lambda _: True) is card
    for finish, expected in [(False, False), (False, True), (True, False)]:
        reg, origin, _, _, _, _, card = make_card()
        reg.finish(origin, normal=finish); reg.disconnect(origin, expected=expected)
        assert reg.claim(answer(card), lambda _: True) is None
    reg, origin, proc, _, _, _, card = make_card()
    newer = completed(); newer["params"]["item"]["id"] = "conflict"
    assert reg.observe(origin, newer, process=proc, current=True) is None
    assert not reg.cards and origin.failed


def source_namespace(monkeypatch):
    """Compile selected real production definitions without importing application/SDK."""
    source = ast.parse((ROOT / "src/feishu_bridge/bridge/fleet_bridge.py").read_text())
    names = {"_observe_question_event", "_question_runtime_binding", "_question_valid", "_question_origin", "_question_dispatch_guard", "_question_live_dispatch_guard",
             "_dispatch_question_answer", "_app_answer_codex_question", "_question_task_done", "_app_answer_ask", "_run_turn_codex",
             "_CodexActiveTurn", "_CodexRpcError", "_codex_unusable_session", "_codex_resume_guard", "_codex_rpc_send", "_codex_rpc_response", "_session_chat_key", "_busy_guard", "run_turn",
             "_do_interrupt", "_new_session", "handle_command", "handle_message", "_write_runtime_field",
             "_codex_archived_binding", "_codex_archived_reply"}
    # Bind real dependencies locally for the existing AST-extracted consumers.
    # Receipt root is separate; it must not overwrite the storage child Scope.
    import tempfile
    from feishu_bridge.codex import codex_archived_resume
    archive_temp = tempfile.TemporaryDirectory(prefix="SYNTHETIC-archive-fixture-")
    archive_root = Path(archive_temp.name).resolve()
    archive_root.chmod(0o700)
    events, logs, sessions = [], [], {"work::codex": "thread"}
    from codex_storage_fixture import question_storage
    from feishu_bridge.codex import fleet_codex_storage as storage
    fixture = question_storage(monkeypatch, sessions)
    front = SimpleNamespace(chat_id="work", broadcast=events.append,
        resolve_work_display_ex=lambda d: ("work", d, ""),
        _push_chat_context=lambda *args: (), _pop_chat_context=lambda _: None)
    async def noop(*a, **kw): return None
    class Usage:
        def __init__(self, *a, **kw): self.seen = []
        def feed(self, ev, **kw): self.seen.append(ev); return False
        def usage(self): return {}
    ns = dict(Path=Path, _codex_archived=codex_archived_resume,
        _CODEX_ARCHIVED=codex_archived_resume.Store(archive_root), _ARCHIVE_SOURCE_TEMP=archive_temp,
        asyncio=asyncio, contextlib=contextlib, contextvars=contextvars, hashlib=hashlib,
        json=json, os=os, re=re, time=time, codex_questions=q, codex_approvals=codex_approvals,
        _codex_storage=storage, _fs=fixture.fs, _storage_fixture=fixture,
        app_frontend=app_frontend,
        APP_FRONT=front, _BOT_NAME="backend", _QUESTION_CONTINUATION=contextvars.ContextVar("test", default=None),
        _QUESTION_DEFAULT_INTENTS=None, _QUESTION_TASKS=set(), _QUESTION_DRAIN_WORKS={}, CODEX_PROCS={}, CODEX_TURNS={}, CODEX_APPROVALS={}, ACTIVE=set(), TURN_DISPLAY={},
        INTERRUPTED=set(), COALESCE={}, _read_runtime=lambda: {}, _runtime_snapshot=lambda: ("gpt-synthetic", "xhigh", "codex"),
        CODEX_BIN="/synthetic/codex", CODEX_SANDBOX="danger-full-access", CODEX_APPROVAL_POLICY="never",
        CODEX_APPROVALS_REVIEWER="", CODEX_PERMISSIONS_PROBE="", CODEX_ENABLED=True, CODEX_STREAM_LIMIT=2**24,
        CODEX_REASONING_SUMMARY="detailed", REPO="/synthetic", _BRIDGE_HOME=Path("/synthetic"),
        DEFAULT_CODEX_MODEL="gpt-synthetic", log=logs.append, _load_session=sessions.get,
        _read_sessions=lambda: {}, _record_session=lambda key, sid, **kw: sessions.update({key:sid}),
        _display_chat_for=lambda _: "group:fleet-product", _drop_client=noop,
        _ro=SimpleNamespace(peek_pending=lambda *a, **kw: None, take_pending=lambda *a, **kw: None),
        _quote_prefix=lambda _: "", SENDER=SimpleNamespace(begin_busy=lambda _: None,
        send_reply=lambda *a, **kw: None, feishu=SimpleNamespace(send=lambda *a: None)),
        _remember=lambda *a: None, _strip_leaked_toolcall=lambda x: x, _extract_media_markers=lambda x:(x,[]),
        _extract_pin_marker=lambda x:(x,False,None), _app_pin_last=lambda *a: None,
        _write_codex_status=lambda *a, **kw: None, _append_codex_ledger=lambda *a, **kw: None,
        _codex_error_event=lambda ev,*a: ("synthetic provider failure",False) if ev.get("method")=="error" else None,
        _codex_is_informational=lambda _: False, _codex_reasoning_text=lambda _: ("",False),
        _codex_usage_from=lambda ev,u:u, _codex_usage_total=lambda _:0, _CodexTurnUsage=Usage,
        _codex_tool_brief=lambda _:None, _steer_pending=noop, _drain_coalesce=noop,
        inflight_mark=lambda *a: None, inflight_clear=lambda *a:None, ASK_PENDING={}, ASK_DISPLAY={},
        CLIENTS={}, IDLE_ACTIVE=set(), ROLLOVER_HOLD=set(), _clear_session=lambda key:sessions.pop(key,None), _session_tasks=lambda *a:None,
        _INTERRUPT_WAIT_S=5, PENDING={}, _YES={"y"}, _NO={"n"}, _INTERRUPT={"stop"})
    ns['SENDER'].send=lambda *a:None
    ns['_MANAGED_ENTRY'] = None  # Explicit unselected dependency for extracted real seams.
    ns['_resolve_session']=lambda *a:[]
    ns["CODEX_QUESTIONS"] = q.Registry(events.append, logs.append)
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]+
                      [n for n in source.body if
                       (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name in names)
                       or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name)
                           and t.id in {"_REPLY_DELIVERY_NOTE", "_CODEX_QUESTION_NOTE"}
                           for t in n.targets))], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree),str(ROOT/"src/feishu_bridge/bridge/fleet_bridge.py"),"exec"), ns)
    monkeypatch.setattr(q, "selected_protocol", lambda _:q.PROTOCOL)
    return ns, events, logs, sessions


class SyntheticProcess:
    def __init__(self, frames, *, thread="thread", clean=True):
        self.returncode = None
        self.queue = asyncio.Queue()
        self.frames, self.thread, self.clean = frames, thread, clean
        self.writes=[]
        self.stdin=self
        self.stdout=self
        self.stderr=SimpleNamespace(read=self.read)
    async def read(self): return b""
    def write(self, raw):
        msg=json.loads(raw);self.writes.append(msg)
        method=msg.get("method")
        if method=="initialize": self.put({"id":1,"result":{}})
        if method=="config/read":
            from feishu_bridge.codex import fleet_codex_storage as storage
            scope=storage.select()
            self.put({"id":msg["id"],"result":{"config":dict(sqlite_home=str(scope.home),forced_login_method="chatgpt",cli_auth_credentials_store="file",model_provider="openai",shell_environment_policy={"set":{**{k:str(scope.home) for k in storage._RESERVED},storage.DESKTOP_KEY:str(scope.desktop_home)}})}})
        if method in {"thread/resume","thread/start"}: self.put({"id":2,"result":{"thread":{"id":self.thread}}})
        if method=="turn/start":
            self.put({"id":3,"result":{"turn":{"id":"turn"}}})
            for frame in self.frames:self.put(frame)
        if method=="turn/steer": self.put({"id":msg["id"],"result":{"turnId":"turn"}})
    def put(self, msg):self.queue.put_nowait((json.dumps(msg)+"\n").encode())
    async def drain(self):await asyncio.sleep(0)
    async def readline(self):return await self.queue.get()
    def __aiter__(self):return self
    async def __anext__(self):
        await asyncio.sleep(0)
        if self.queue.empty():raise StopAsyncIteration
        return await self.queue.get()
    def close(self):pass
    async def wait(self):self.returncode=0 if self.clean else 1;return self.returncode
    def terminate(self):raise AssertionError("No synthetic signal path")


def normal_end(status="completed"):
    return {"method":"turn/completed","params":{"threadId":"thread","turn":{"id":"turn","status":status}}}


@pytest.mark.parametrize("supported", [True, False])
def test_real_turn_input_explains_question_routes_only_for_selected_producer(monkeypatch, supported):
    ns, events, _, _ = source_namespace(monkeypatch)
    monkeypatch.setattr(q, "selected_protocol", lambda _: q.PROTOCOL if supported else None)
    proc = SyntheticProcess([normal_end()])
    async def launch(*args, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    asyncio.run(ns["_run_turn_codex"]("work", "original synthetic user prompt"))
    starts = [m for m in proc.writes if m.get("method") == "turn/start"]
    assert len(starts) == 1
    text = starts[0]["params"]["input"][0]["text"]
    assert text.startswith(ns["_REPLY_DELIVERY_NOTE"] + "\n\n")
    assert text.endswith("original synthetic user prompt")
    if supported:
        assert "request_user_input_async" in text and "不得省略 options" in text
        assert "1–3 题" in text and "2–8 个" in text
        assert "开放式问题用普通文字询问" in text
        assert "保留完整范围与风险说明" in text
    else:
        assert "request_user_input_async" not in text
    assert not ns["CODEX_QUESTIONS"].cards
    assert all(e.get("ev") != "ask" for e in events)


def test_real_stdout_loop_streamed_questions_and_security_lifecycle(monkeypatch):
    ns, events, logs, _=source_namespace(monkeypatch)
    shared={"id":88,"method":"item/tool/requestUserInput","params":{"threadId":"thread","turnId":"turn","itemId":"item","questions":[]}}
    proc=SyntheticProcess([{"method":"item/agentMessage/delta","params":{"delta":"already streamed"}},completed(),shared,
        {"method":"serverRequest/resolved","params":{"threadId":"thread","requestId":88}},normal_end()])
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,"create_subprocess_exec",launch)
    asyncio.run(ns["_run_turn_codex"]("work","synthetic prompt"))
    asks=[e for e in events if e["ev"]=="ask"]
    assert len(asks)==1 and any(e.get("text")=="already streamed" for e in events)
    card=ns["CODEX_QUESTIONS"].cards[asks[0]["ask_id"]]
    assert card.origin.normal and card.origin.disconnected and card.origin.process is None
    assert any(e.get("id")==88 and e.get("error",{}).get("code")==-32601 for e in proc.writes)
    assert not any(e.get("method")=="item/tool/requestUserInput" for e in proc.writes)


@pytest.mark.parametrize("frames,clean", [([completed(),normal_end("failed")],True),
    ([completed(),{"method":"error","params":{}},normal_end()],True),([completed()],True),([completed(),normal_end()],False)])
def test_real_stdout_failure_disconnect_cleans(monkeypatch,frames,clean):
    ns,events,_,_=source_namespace(monkeypatch);proc=SyntheticProcess(frames,clean=clean)
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,"create_subprocess_exec",launch)
    asyncio.run(ns["_run_turn_codex"]("work","prompt"))
    assert not ns["CODEX_QUESTIONS"].cards
    assert any(e["ev"]=="ask_cancel" for e in events)


def test_public_valid_normal_answer_continues_same_registered_thread(monkeypatch):
    ns,events,logs,_=source_namespace(monkeypatch)
    initial=SyntheticProcess([completed(),normal_end()]);continuation=SyntheticProcess([normal_end()]);launched=[]
    async def launch(*a,**kw):
        proc=initial if not launched else continuation;launched.append(proc);return proc
    monkeypatch.setattr(asyncio,"create_subprocess_exec",launch)
    async def scenario():
        await ns["_run_turn_codex"]("work","prompt")
        card=next(iter(ns["CODEX_QUESTIONS"].cards.values()))
        ns["_app_answer_codex_question"](answer(card));ns["_app_answer_codex_question"](answer(card))
        await asyncio.gather(*ns["_QUESTION_TASKS"])
    asyncio.run(scenario())
    assert len(launched)==2
    assert [m["params"]["threadId"] for m in continuation.writes if m.get("method")=="thread/resume"]==["thread"]
    assert not any(m.get("method")=="thread/start" for m in continuation.writes)
    assert any("state=sent_same_thread" in s for s in logs)
    assert not ns["CODEX_QUESTIONS"].cards


def test_queued_answer_binding_drift_prevents_any_launch(monkeypatch):
    ns,_,logs,sessions=source_namespace(monkeypatch)
    reg,origin,_,_,_,_,card=make_card();ns["CODEX_QUESTIONS"]=reg
    origin.binding=q.Binding("work","group:fleet-product","thread","turn",ns["_question_runtime_binding"](),q.PROTOCOL,"c")
    origin.normal=True
    card.deadline=time.monotonic()+600
    ns["ACTIVE"].add("work")
    async def no_launch(*a,**kw):raise AssertionError("Cannot start after drift")
    monkeypatch.setattr(asyncio,"create_subprocess_exec",no_launch)
    async def scenario():
        ns["_app_answer_codex_question"](answer(card))
        await asyncio.sleep(0)
        sessions["work::codex"]="other"
        ns["ACTIVE"].clear()
        await asyncio.gather(*ns["_QUESTION_TASKS"])
    asyncio.run(scenario())
    assert not reg.cards


def test_active_answer_guard_after_write_lock_and_single_send(monkeypatch):
    ns,_,logs,_=source_namespace(monkeypatch)
    proc=SyntheticProcess([])
    origin=q.Origin(q.Binding("work","group:fleet-product","thread","turn",ns["_question_runtime_binding"](),q.PROTOCOL,"c"),proc)
    control=ns["_CodexActiveTurn"](proc,"thread","turn")
    ns["CODEX_PROCS"]["work"]=proc;ns["CODEX_TURNS"]["work"]=control
    card=ns["CODEX_QUESTIONS"].observe(origin,completed(),process=proc,current=True)
    async def scenario():
        ns["_app_answer_codex_question"](answer(card))
        await asyncio.sleep(0)
        while not any(m.get("method")=="turn/steer" for m in proc.writes):await asyncio.sleep(0)
        control.resolve(json.loads(await proc.readline()))
        await asyncio.gather(*ns["_QUESTION_TASKS"])
    asyncio.run(scenario())
    assert len([m for m in proc.writes if m.get("method")=="turn/steer"])==1
    assert any("state=sent_same_thread" in s for s in logs)


@pytest.mark.parametrize("ack,accepted", [
    ({"result": {"turnId": "turn"}}, True),
    ({"result": {"turnId": "other"}}, False),
    ({}, False),
    ({"result": {}}, False),
    ({"result": None}, False),
    ({"result": []}, False),
    ({"result": "turn"}, False),
    ({"result": {"turnId": 1}}, False),
    ({"result": {"turnId": True}}, False),
    ({"result": {"turnId": ""}}, False),
    ({"error": {"code": -32000, "message": "synthetic error"}}, False),
    ({"error": "invalid error shape", "result": {"turnId": "turn"}}, False),
    ({"error": None, "result": {"turnId": "turn"}}, False),
])
def test_actual_steer_ack_requires_exact_selected_turn_response(monkeypatch, ack, accepted):
    """Public answer -> real dispatch/request/steer/resolve; one possible write only."""
    ns, _, logs, _ = source_namespace(monkeypatch)
    class AckProcess(SyntheticProcess):
        def write(self, raw):
            message = json.loads(raw)
            self.writes.append(message)
            assert message["method"] == "turn/steer"
            self.put({"id": message["id"], **copy.deepcopy(ack)})
    proc = AckProcess([])
    control = ns["_CodexActiveTurn"](proc, "thread", "turn")
    ns["CODEX_PROCS"]["work"] = proc
    ns["CODEX_TURNS"]["work"] = control
    origin = q.Origin(q.Binding("work", "group:fleet-product", "thread", "turn",
        ns["_question_runtime_binding"](), q.PROTOCOL, "c"), proc)
    card = ns["CODEX_QUESTIONS"].observe(origin, completed(), process=proc, current=True)
    async def forbidden(*args, **kwargs):
        raise AssertionError("No retry/new-turn fallback after a possible steering write")
    ns["run_turn"] = forbidden
    async def scenario():
        ns["_app_answer_codex_question"](answer(card))
        while not proc.writes:
            await asyncio.sleep(0)
        assert control.resolve(json.loads(await proc.readline()))
        await asyncio.gather(*ns["_QUESTION_TASKS"])
        ns["_app_answer_codex_question"](answer(card))
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert len(proc.writes) == 1 and proc.writes[0]["params"]["expectedTurnId"] == "turn"
    assert not ns["CODEX_QUESTIONS"].cards
    assert not any("normal_completion_before_write" in s for s in logs)
    sent = [s for s in logs if "state=sent_same_thread" in s]
    unknown = [s for s in logs if "state=delivery_failed_or_unknown" in s]
    assert (len(sent), len(unknown)) == ((1, 0) if accepted else (0, 1))


def test_frontend_reserved_namespace_never_generic_fallback():
    module=ast.parse((ROOT/"src/feishu_bridge/bridge/app_frontend.py").read_text())
    cls=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name=="AppFrontend")
    method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="_handle_frame")
    ns={};exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),method],type_ignores=[])),"app_frontend.py","exec"),ns)
    received=[]
    obj=SimpleNamespace(_question_answer=received.append)
    asyncio.run(ns["_handle_frame"](obj,{"type":"answer","ask_id":q.PREFIX+"stale","text":"Approve ONE"},None))
    assert len(received)==1  # Missing generic callbacks would fail if branch fell through.
    obj._question_answer=None
    asyncio.run(ns["_handle_frame"](obj,{"type":"answer","ask_id":q.PREFIX+"stale"},None))


@pytest.mark.parametrize("action", ["stop", "new", "fork", "resume", "plain", "runtime"])
def test_real_public_cleanup_paths(monkeypatch,tmp_path,action):
    ns,_,_,_=source_namespace(monkeypatch)
    reg,origin,_,_,_,_,card=make_card();ns["CODEX_QUESTIONS"]=reg
    if action=="stop":asyncio.run(ns["_do_interrupt"]("work"))
    if action=="new":asyncio.run(ns["_new_session"]("work"))
    if action in {"fork","resume"}:asyncio.run(ns["handle_command"]("work","/"+action))
    if action=="plain":
        async def scenario():
            ns["PENDING"]["work"]=asyncio.get_running_loop().create_future()
            await ns["handle_message"]("work","y")
        asyncio.run(scenario())
    if action=="runtime":
        ns["_RUNTIME_FILE"]=tmp_path/'runtime.json'
        ns["fcntl"]=SimpleNamespace(flock=lambda *a:None,LOCK_EX=1)
        ns["_write_runtime_field"]("effort","xhigh",("default",),"error")
    assert not reg.cards


@pytest.mark.parametrize("drift", ["runtime", "thread", "work", "group", "rollover"])
def test_actual_binding_recheck_refuses_drift(monkeypatch,drift):
    ns,_,_,sessions=source_namespace(monkeypatch)
    reg,origin,_,_,_,_,card=make_card();ns["CODEX_QUESTIONS"]=reg
    origin.binding=q.Binding("work","group:fleet-product","thread","turn",ns["_question_runtime_binding"](),q.PROTOCOL,"c")
    origin.normal=True
    assert ns["_question_valid"](card)
    if drift=="runtime":ns["_read_runtime"]=lambda:{"effort":"high"}
    if drift=="thread":sessions['work::codex']='replacement'
    if drift=="work":ns['APP_FRONT'].resolve_work_display_ex=lambda d:('replacement',d,'')
    if drift=="group":ns['APP_FRONT'].resolve_work_display_ex=lambda d:('work','work','not_member')
    if drift=="rollover":ns['ROLLOVER_HOLD'].add('work')
    assert not ns["_question_valid"](card)
    assert reg.claim(answer(card),ns["_question_valid"]) is None


def test_completion_wins_before_answer_task_uses_normal_continuation(monkeypatch):
    ns,_,logs,_=source_namespace(monkeypatch)
    proc=SyntheticProcess([])
    control=ns['_CodexActiveTurn'](proc,'thread','turn')
    ns['CODEX_PROCS']['work']=proc;ns['CODEX_TURNS']['work']=control
    origin=q.Origin(q.Binding('work','group:fleet-product','thread','turn',ns['_question_runtime_binding'](),q.PROTOCOL,'c'),proc)
    card=ns['CODEX_QUESTIONS'].observe(origin,completed(),process=proc,current=True)
    replacement=SyntheticProcess([normal_end()]);launches=[]
    async def launch(*a,**kw):launches.append(replacement);return replacement
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    async def scenario():
        ns['_app_answer_codex_question'](answer(card))
        ns['CODEX_QUESTIONS'].finish(origin,normal=True)
        ns['CODEX_QUESTIONS'].disconnect(origin,expected=True)
        ns['CODEX_TURNS'].clear();ns['CODEX_PROCS'].clear()
        await asyncio.gather(*ns['_QUESTION_TASKS'])
    asyncio.run(scenario())
    assert len(launches)==1 and not proc.writes
    assert any('state=sent_same_thread' in s for s in logs)


def test_active_write_lock_drift_cancels_without_any_rpc(monkeypatch):
    ns,_,_,sessions=source_namespace(monkeypatch)
    proc=SyntheticProcess([]);control=ns['_CodexActiveTurn'](proc,'thread','turn')
    ns['CODEX_PROCS']['work']=proc;ns['CODEX_TURNS']['work']=control
    origin=q.Origin(q.Binding('work','group:fleet-product','thread','turn',ns['_question_runtime_binding'](),q.PROTOCOL,'c'),proc)
    card=ns['CODEX_QUESTIONS'].observe(origin,completed(),process=proc,current=True)
    async def scenario():
        await control._write_lock.acquire()
        ns['_app_answer_codex_question'](answer(card));await asyncio.sleep(0)
        sessions['work::codex']='other'
        control._write_lock.release()
        await asyncio.gather(*ns['_QUESTION_TASKS'])
    asyncio.run(scenario())
    assert not proc.writes and not ns['CODEX_QUESTIONS'].cards


def test_new_resume_thread_mismatch_never_sends_turn_start(monkeypatch):
    ns,_,logs,_=source_namespace(monkeypatch)
    reg,origin,_,_,_,_,card=make_card();ns['CODEX_QUESTIONS']=reg
    origin.binding=q.Binding('work','group:fleet-product','thread','turn',ns['_question_runtime_binding'](),q.PROTOCOL,'c')
    origin.normal=True;card.deadline=time.monotonic()+600
    proc=SyntheticProcess([normal_end()],thread='different')
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    async def scenario():
        ns['_app_answer_codex_question'](answer(card))
        await asyncio.gather(*ns['_QUESTION_TASKS'])
    asyncio.run(scenario())
    assert not any(m.get('method')=='turn/start' for m in proc.writes)
    assert not reg.cards


def test_selected_protocol_refuses_unpinned_binary(tmp_path):
    path=tmp_path/'codex';path.write_bytes(b'synthetic unapproved executable')
    assert q.selected_protocol(str(path)) is None


@pytest.mark.parametrize('method',sorted(codex_approvals.METHODS))
def test_never_security_handlers_receive_typed_requests_separately(monkeypatch,method):
    ns,events,_,_=source_namespace(monkeypatch)
    params={'threadId':'thread','turnId':'turn','itemId':'tool','command':'synthetic',
            'permissions':{'fileSystem':{'read':['/synthetic']}}}
    frames=[completed(),{'id':7,'method':method,'params':params},
            {'id':'7','method':method,'params':params},normal_end()]
    proc=SyntheticProcess(frames)
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','prompt'))
    replies=[w for w in proc.writes if w.get('id') in (7,'7') and 'method' not in w]
    assert len(replies)==2 and {type(w['id']) for w in replies}=={int,str}
    assert all(w.get('result',{})==({'permissions':{},'scope':'turn'} if method==codex_approvals.PERMISSIONS_METHOD else {'decision':'decline'}) for w in replies)
    assert all(e.get('ask_id','').startswith(q.PREFIX) for e in events if e['ev']=='ask')


def test_frontend_gateway_disconnect_public_cleanup(monkeypatch):
    module=ast.parse((ROOT/'src/feishu_bridge/bridge/app_frontend.py').read_text())
    cls=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name=='AppFrontend')
    defs=[copy.deepcopy(n) for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name in {'_on_conn','_handle_frame'}]
    ns={'asyncio':asyncio,'json':json}
    tree=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+defs,type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree),'app_frontend.py','exec'),ns)
    reg,_,_,_,_,_,card=make_card()
    class Writer:
        def close(self):pass
    class Reader:
        def __init__(self):self.lines=[b'{"type":"hello"}\n',b'']
        async def readline(self):return self.lines.pop(0)
    front=SimpleNamespace(_managed_ingress=None,_writers=set(),_gateway_writers=set(),_log=lambda *a:None,
        _greet=lambda *a:None,_question_disconnect=lambda:reg.cancel_work(None,'gateway_disconnected'))
    front._handle_frame=lambda req,writer:ns['_handle_frame'](front,req,writer)
    asyncio.run(ns['_on_conn'](front,Reader(),Writer()))
    assert not reg.cards and not front._gateway_writers


def test_completed_answer_never_enters_durable_inflight_replay(monkeypatch):
    ns,_,_,_=source_namespace(monkeypatch)
    first=SyntheticProcess([completed(),normal_end()]);second=SyntheticProcess([normal_end()]);launches=[]
    async def launch(*a,**kw):
        p=first if not launches else second;launches.append(p);return p
    def forbidden(*a):raise AssertionError('No durable card replay storage')
    ns['inflight_mark']=forbidden;ns['inflight_clear']=forbidden
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    async def scenario():
        await ns['_run_turn_codex']('work','prompt')
        card=next(iter(ns['CODEX_QUESTIONS'].cards.values()))
        ns['_app_answer_codex_question'](answer(card))
        await asyncio.gather(*ns['_QUESTION_TASKS'])
    asyncio.run(scenario())
    assert len(launches)==2 and not ns['CODEX_QUESTIONS'].cards


def test_wrong_thread_completed_does_not_preserve_card(monkeypatch):
    ns,_,_,_=source_namespace(monkeypatch)
    end=normal_end();end['params']['threadId']='wrong-thread'
    proc=SyntheticProcess([completed(),end])
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','prompt'))
    assert not ns['CODEX_QUESTIONS'].cards


def test_new_thread_is_registered_only_when_genuine_question_arrives(monkeypatch):
    ns,events,_,sessions=source_namespace(monkeypatch);sessions.clear()
    ns['_fs'].SESSIONS_FILE.write_text('{}')
    proc=SyntheticProcess([completed(),normal_end()])
    async def launch(*a,**kw):return proc
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    asyncio.run(ns['_run_turn_codex']('work','prompt'))
    assert sessions['work::codex']=='thread'
    assert len([e for e in events if e['ev']=='ask'])==1
    assert len(ns['CODEX_QUESTIONS'].cards)==1


def test_completion_during_steer_lock_wait_queues_once_before_any_write(monkeypatch):
    ns,_,logs,_=source_namespace(monkeypatch)
    proc=SyntheticProcess([]);control=ns['_CodexActiveTurn'](proc,'thread','turn')
    ns['CODEX_PROCS']['work']=proc;ns['CODEX_TURNS']['work']=control
    origin=q.Origin(q.Binding('work','group:fleet-product','thread','turn',ns['_question_runtime_binding'](),q.PROTOCOL,'c'),proc)
    card=ns['CODEX_QUESTIONS'].observe(origin,completed(),process=proc,current=True)
    replacement=SyntheticProcess([normal_end()]);launches=[]
    async def launch(*a,**kw):launches.append(replacement);return replacement
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    async def scenario():
        await control._write_lock.acquire()
        ns['_app_answer_codex_question'](answer(card));await asyncio.sleep(0)
        ns['CODEX_QUESTIONS'].finish(origin,normal=True)
        ns['CODEX_QUESTIONS'].disconnect(origin,expected=True)
        ns['CODEX_TURNS'].clear();ns['CODEX_PROCS'].clear()
        control._write_lock.release()
        await asyncio.gather(*ns['_QUESTION_TASKS'])
    asyncio.run(scenario())
    assert not proc.writes and len(launches)==1
    assert len([m for m in replacement.writes if m.get('method')=='turn/start'])==1
    assert any('normal_completion_before_write' in s for s in logs)


def test_card_byte_limit_and_pending_bound_refuse_without_truncation():
    reg,origin,proc,frames,logs,_=registry()
    qs=[{'title':'风'*6000,'options':['a','b']} for _ in range(3)]
    assert reg.observe(origin,completed(qs),process=proc,current=True) is None
    assert not reg.cards and not any(e['ev']=='ask' for e in frames)
    reg,_,_,frames,logs,_=registry()
    for i in range(q.MAX_PENDING+1):
        p=object();origin=q.Origin(q.Binding(f'work{i}','group:fleet-product','thread','turn','runtime',q.PROTOCOL,str(i)),p)
        result=reg.observe(origin,completed(),process=p,current=True)
        assert (result is not None)==(i<q.MAX_PENDING)
    assert len(reg.cards)==q.MAX_PENDING


def test_cancel_after_dispatch_never_retries_and_keeps_ack_unknown_distinct():
    reg,_,_,_,logs,_,card=make_card()
    assert reg.claim(answer(card),lambda _:True) is card
    assert reg.transition(card,'dispatching',lambda _:True)
    reg.cancel_work('work','new_turn')
    reg.delivered(card,acknowledged=True);reg.delivered(card,acknowledged=False)
    assert card.state=='cancelled' and len([s for s in logs if 'acknowledged_after_cancel' in s])==1
    assert reg.claim(answer(card),lambda _:True) is None

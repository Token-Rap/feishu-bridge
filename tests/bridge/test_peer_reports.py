"""Real source paths with SYNTHETIC originals; no model/native/human writes."""
from __future__ import annotations
import ast
import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.bridge import app_frontend as af
from feishu_bridge.bridge import peer_reports as pr
from feishu_bridge.questions import codex_questions as q
from tests.codex.test_codex_questions import source_namespace, make_card, answer

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(os.environ.get("FLEET_PEER_BASELINE", ROOT))
HELPER = os.environ.get("FLEET_PEER_HELPER_SOURCE")


def config(tmp_path):
    fleet, domains = tmp_path / "fleet.json", tmp_path / "fleet_domains.json"
    fleet.write_text(json.dumps({b: {"home": str(tmp_path / b)} for b in
                                ("sample-web", "backend", "sample-manager", "mac-ops")}))
    domains.write_text(json.dumps({"domains": {
        "fleet-product": {"bots": ["sample-web", "backend"], "lead": "backend"},
        "ops": {"bots": ["sample-manager", "mac-ops"], "lead": "sample-manager"}},
        "related": [["ops", "fleet-product", "SYNTHETIC permitted lead route"]]}))
    return fleet, domains


def origin():
    return dict(version=1, kind="report", source_bot="sample-web", source_chat="group:fleet-product",
                target_bot="backend", target_chat="group:fleet-product", id="a" * 32)


def compile_functions(path, names, ns):
    tree = ast.parse(path.read_text())
    picked = [copy.deepcopy(n) for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + picked, type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), ns)


def harness(monkeypatch, tmp_path, normal=False):
    ns, events, logs, _ = source_namespace(monkeypatch)
    compile_functions(BASE / "src/feishu_bridge/bridge/fleet_bridge.py", {"handle_message", "_app_inject", "_defer_peer_report",
        "_drain_peer_reports", "_question_expiry_loop", "_stamp", "_route_marker", "_queued_parts"}, ns)
    fleet, domains = config(tmp_path)
    ns.update(PEER_REPORTS=pr.Journal(tmp_path / "peer_reports.json"), _PEER_DRAINS=set(), _PEER_TASKS=set(),
              peer_reports=pr, datetime=datetime, timezone=timezone, timedelta=timedelta,
              _stale_card_note=lambda: "", _NEW_SESSION_WORDS={"/new", "/reset", "/clear"})
    ns["_peer_qualified"] = lambda v, d: pr.qualify(v, receiver="backend", display=d, registry=fleet, domains=domains)
    reg, owner, proc, _, _, clock, card = make_card()
    owner.binding = q.Binding("work", "group:fleet-product", "thread", "turn",
                             ns["_question_runtime_binding"](), q.PROTOCOL, "connection")
    ns["CODEX_QUESTIONS"] = reg
    ns["CODEX_PROCS"]["work"] = proc
    ns["CODEX_TURNS"]["work"] = SimpleNamespace(proc=proc, thread_id="thread", turn_id="turn")
    if normal:
        reg.finish(owner, normal=True)
        reg.disconnect(owner, expected=True)
    calls = []
    async def run(work, prompt, *args):
        calls.append((work, af.DISPLAY_CHAT.get(), prompt))
    ns["run_turn"] = run
    async def noop(*a, **kw): pass
    ns["_steer_pending"] = noop
    ns["_drain_coalesce"] = noop
    front = ns["APP_FRONT"]
    front._push_chat_context = lambda *args: af.AppFrontend._push_chat_context(front, *args)
    front._pop_chat_context = af.AppFrontend._pop_chat_context
    front._gateway_writers = set()
    front._bg = set()
    front._peer_report = ns["_peer_qualified"]
    front._inject = ns["_app_inject"]
    front._inject_ack = lambda *a: None
    front._spawn = lambda coro: af.AppFrontend._spawn(front, coro)
    obj = next(n for n in ast.parse((BASE / "src/feishu_bridge/bridge/app_frontend.py").read_text()).body if isinstance(n, ast.ClassDef) and n.name == "AppFrontend")
    method = copy.deepcopy(next(n for n in obj.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_frame"))
    scope = vars(af).copy()
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(BASE / "src/feishu_bridge/bridge/app_frontend.py"), "exec"), scope)
    async def submit(frame, gateway=False):
        writer = object()
        if gateway: front._gateway_writers.add(writer)
        await scope["_handle_frame"](front, frame, writer)
        if front._bg: await asyncio.gather(*front._bg)
    return ns, reg, card, clock, calls, submit, logs, domains


def produced_frame(monkeypatch, tmp_path, *, kind="report"):
    if not HELPER:
        pytest.skip("FLEET_PEER_HELPER_SOURCE is required for producer integration")
    path = tmp_path / "session_inject.py"
    path.write_bytes(Path(HELPER).read_bytes())
    scope = {"__name__": "SYNTHETIC_producer", "__file__": str(path)}
    exec(compile(path.read_text(), str(path), "exec"), scope)
    fleet, _ = config(tmp_path)
    scope["FLEET"] = fleet
    scope["pending_ask"] = lambda _: None
    scope["_warn_if_busy"] = lambda _: None
    target = tmp_path / "synthetic.sock"; target.touch()
    scope["resolve_target"] = lambda _: target
    frames = []
    class Socket:
        def settimeout(self, *a): pass
        def connect(self, *a): pass
        def sendall(self, raw): frames.append(json.loads(raw))
        def shutdown(self, *a): pass
        def recv(self, *a):
            if getattr(self, "read", False): return b""
            self.read = True
            return b'{"type":"inject_ack","chat_id":"group:fleet-product"}\n'
        def close(self): pass
    scope["socket"] = SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, SHUT_WR=1, socket=lambda *a: Socket())
    monkeypatch.setenv("FLEET_CHAT_ID", "group:fleet-product")
    monkeypatch.setattr(sys, "argv", [str(path), "--bot", "backend", "--from", "sample-web",
        "--group", "fleet-product", "--kind", kind, "--text", "SYNTHETIC original report"])
    scope["main"]()
    return frames


def test_parent_contract_actual_producer_frame(monkeypatch, tmp_path):
    frames = produced_frame(monkeypatch, tmp_path)
    assert len(frames) == 1  # No --task: no group_event/ledger/terminal.
    assert frames[0]["peer_report"]["kind"] == "report"
    assert frames[0]["text"].endswith("\nSYNTHETIC original report")


@pytest.mark.parametrize("normal", [False, True])
def test_parent_contract_public_producer_frontend_handle_preserves_card(monkeypatch, tmp_path, normal):
    frames = produced_frame(monkeypatch, tmp_path)
    ns, reg, card, _, calls, submit, _, _ = harness(monkeypatch, tmp_path, normal)
    asyncio.run(submit(frames[0]))
    assert reg.cards.get(card.ask_id) is card
    assert not calls and not ns["COALESCE"]
    row = ns["PEER_REPORTS"].held()[0]
    assert row["text"] == frames[0]["text"] and row["display"] == "group:fleet-product"
    assert row["work"] == "work" and row["origin"] == frames[0]["peer_report"]
    assert row["expires"] == card.expires_at and row["arrived"] > 0


@pytest.mark.parametrize("mutation", ["legacy", "unknown", "body_marker", "gateway", "actor", "human_origin", "bad_route", "bad_shape"])
def test_unqualified_human_legacy_forged_inputs_still_cancel(monkeypatch, tmp_path, mutation):
    ns, reg, card, _, _, submit, _, _ = harness(monkeypatch, tmp_path)
    frame = {"type": "user_message", "text": "📨 [来自 sample-web bot]\nSYNTHETIC", "chat_id": "group:fleet-product", "peer_report": origin()}
    if mutation in {"legacy", "body_marker"}: frame.pop("peer_report")
    if mutation == "unknown": frame["peer_report"]["source_bot"] = "unknown"
    if mutation == "actor": frame["from"] = "human"
    if mutation == "human_origin": frame["message_origin"] = {"version": 1, "kind": "gateway_human"}
    if mutation == "bad_route": frame["peer_report"]["source_chat"] = "group:ops"
    if mutation == "bad_shape": frame["peer_report"]["extra"] = True
    # PENDING proves the exact old cancellation branch without any model call.
    async def run():
        ns["PENDING"]["work"] = asyncio.get_running_loop().create_future()
        await submit(frame, gateway=mutation == "gateway")
    asyncio.run(run())
    assert card.ask_id not in reg.cards and not ns["PEER_REPORTS"].rows


@pytest.mark.parametrize("release", ["expired", "cancel", "abnormal_disconnect", "binding_changed", "answer_ack", "answer_possible_write"])
def test_original_release_drain_once_route_time_no_retry(monkeypatch, tmp_path, release):
    ns, reg, card, clock, calls, submit, _, _ = harness(monkeypatch, tmp_path, normal=release.startswith("answer"))
    frame = {"type": "user_message", "text": "SYNTHETIC report", "chat_id": "group:fleet-product", "peer_report": origin()}
    async def run():
        await submit(frame)
        row = copy.deepcopy(ns["PEER_REPORTS"].held()[0])
        await ns["_drain_peer_reports"]("work")
        assert not calls
        if release == "expired": clock[0] = card.deadline
        if release == "cancel": reg.cancel(card, "unrelated_input")
        if release == "abnormal_disconnect": reg.disconnect(card.origin, expected=False)
        if release == "binding_changed": card.origin.failed = True
        if release.startswith("answer"):
            assert reg.claim(answer(card), ns["_question_valid"]) is card
            reg.transition(card, "queued", ns["_question_valid"])
            reg.transition(card, "dispatching", ns["_question_valid"])
            await ns["_drain_peer_reports"]("work")
            assert not calls  # received/queued/possible-write window remains protected.
            reg.delivered(card, acknowledged=release == "answer_ack")
        await ns["_drain_peer_reports"]("work")
        await ns["_drain_peer_reports"]("work")
        await submit(frame)  # Exact notification duplicate is consumed, not another turn.
        assert len(calls) == 1 and calls[0][1] == row["display"]
        assert calls[0][2] == ns["_stamp"](row["text"], row["arrived"], row["display"])
        assert ns["PEER_REPORTS"].rows[0]["state"] == "completed"
        assert ns["PEER_REPORTS"].rows[0]["expires"] == row["expires"]
    asyncio.run(run())


def test_active_origin_normal_completion_does_not_steer_or_launch_report(monkeypatch, tmp_path):
    ns, reg, card, _, calls, submit, _, _ = harness(monkeypatch, tmp_path)
    ns["ACTIVE"].add("work")
    async def run():
        await submit({"type": "user_message", "text": "SYNTHETIC report", "chat_id": "group:fleet-product", "peer_report": origin()})
        reg.finish(card.origin, normal=True); reg.disconnect(card.origin, expected=True)
        ns["ACTIVE"].clear()
        await ns["_drain_peer_reports"]("work")
    asyncio.run(run())
    assert reg.cards.get(card.ask_id) is card and not calls and not ns["COALESCE"]


def test_source_revocation_blocks_original_without_private_fallback(monkeypatch, tmp_path):
    ns, reg, card, _, calls, submit, _, domains = harness(monkeypatch, tmp_path)
    async def run():
        await submit({"type": "user_message", "text": "SYNTHETIC", "chat_id": "group:fleet-product", "peer_report": origin()})
        domains.write_text('{}'); reg.cancel(card, "expired")
        await ns["_drain_peer_reports"]("work")
    asyncio.run(run())
    assert not calls and ns["PEER_REPORTS"].rows[0]["state"] == "blocked"


def test_journal_restart_keeps_originals_without_card_or_possible_write_replay(tmp_path):
    journal = pr.Journal(tmp_path / "journal.json")
    for n in range(2):
        p = origin(); p["id"] = str(n) * 32
        journal.hold(p, work="work", display=p["target_chat"], text="SYNTHETIC original", arrived=123., visible=True, cards=["non-authoritative old ID"], expires=600.)
    journal.transition("1" * 32, "claimed")
    reopened = pr.Journal(journal.path)
    assert [r["state"] for r in reopened.rows] == ["recovery_required", "delivery_unknown"]
    assert not reopened.held() and all(r["text"] == "SYNTHETIC original" for r in reopened.rows)


def test_possible_model_write_failure_claim_prevents_any_retry(monkeypatch, tmp_path):
    ns, reg, card, _, calls, submit, _, _ = harness(monkeypatch, tmp_path)
    async def fail(*a): calls.append(a); raise RuntimeError("SYNTHETIC possible write")
    ns["run_turn"] = fail
    async def run():
        await submit({"type": "user_message", "text": "SYNTHETIC", "chat_id": "group:fleet-product", "peer_report": origin()})
        reg.cancel(card, "expired")
        await ns["_drain_peer_reports"]("work"); await ns["_drain_peer_reports"]("work")
    asyncio.run(run())
    assert len(calls) == 1 and ns["PEER_REPORTS"].rows[0]["state"] == "delivery_unknown"


def test_journal_precommit_refusal_retains_card_rule_and_original_normal_queue(monkeypatch, tmp_path):
    ns, reg, card, _, _, submit, logs, _ = harness(monkeypatch, tmp_path)
    ns["ACTIVE"].add("work")
    ns["inflight_queue"] = lambda *a: None
    ns["_visible_queue_count"] = lambda xs: len(xs)
    monkeypatch.setattr(ns["PEER_REPORTS"], "_store", lambda _: (_ for _ in ()).throw(OSError("SYNTHETIC precommit refusal")))
    asyncio.run(submit({"type": "user_message", "text": "SYNTHETIC original", "chat_id": "group:fleet-product", "peer_report": origin()}))
    assert card.ask_id not in reg.cards and ns["COALESCE"]["work"][0][0] == "SYNTHETIC original"
    assert any("ordinary input path" in s for s in logs)


def test_cross_domain_reports_require_current_related_leads(tmp_path):
    fleet, domains = config(tmp_path)
    value = origin(); value.update(source_bot="backend", target_bot="sample-manager", target_chat="group:ops")
    assert pr.qualify(value, receiver="sample-manager", display="group:ops", registry=fleet, domains=domains)
    value["source_bot"] = "sample-web"
    assert pr.qualify(value, receiver="sample-manager", display="group:ops", registry=fleet, domains=domains) is None


def test_dispatch_producer_is_not_report_exemption(monkeypatch, tmp_path):
    assert "peer_report" not in produced_frame(monkeypatch, tmp_path, kind="dispatch")[0]


@pytest.mark.parametrize("outcome", ["ack", "possible_write", "completed_before_write", "cancel_after_write"])
def test_actual_answer_dispatch_peer_arrival_race_never_retries_answer(monkeypatch, tmp_path, outcome):
    ns, reg, card, clock, calls, submit, _, _ = harness(monkeypatch, tmp_path)
    ns["time"] = SimpleNamespace(time=af.time.time, monotonic=lambda: clock[0])
    writes = []
    async def run():
        entered, released = asyncio.Event(), asyncio.Event()
        async def steer(text, guard):
            entered.set(); await released.wait()
            if outcome == "completed_before_write":
                reg.finish(card.origin, normal=True)
                guard()  # Actual pre-write guard raises CompletedBeforeWrite.
            guard(); writes.append(text)
            if outcome == "cancel_after_write":
                reg.cancel(card, "gateway_disconnected")
                ns["ACTIVE"].clear()
                await ns["_drain_peer_reports"]("work")
                assert not calls and ns["_QUESTION_DRAIN_WORKS"]["work"] == 1
            if outcome == "possible_write": raise RuntimeError("SYNTHETIC write outcome unknown")
        control = ns["CODEX_TURNS"]["work"]; control.steer = steer
        ns["ACTIVE"].add("work")
        first = {"type": "user_message", "text": "SYNTHETIC first original", "chat_id": "group:fleet-product", "peer_report": origin()}
        await submit(first)
        ns["_app_answer_codex_question"](answer(card))
        ns["_app_answer_codex_question"](answer(card))
        await entered.wait()
        second = copy.deepcopy(first); second["peer_report"]["id"] = "b" * 32; second["text"] = "SYNTHETIC second original"
        await submit(second)
        assert len(ns["PEER_REPORTS"].held()) == 2 and card.state == "dispatching"
        await ns["_drain_peer_reports"]("work"); assert not calls
        # Another work's ordinary answer path proceeds without a global/group lock.
        ns["PENDING"]["other-work"] = asyncio.get_running_loop().create_future()
        await ns["handle_message"]("other-work", "y")
        assert ns["PENDING"]["other-work"].result() is True
        released.set()
        if outcome == "completed_before_write":
            await asyncio.sleep(0); ns["ACTIVE"].clear()
        await asyncio.gather(*ns["_QUESTION_TASKS"])
        assert len(writes) == (0 if outcome == "completed_before_write" else 1)
        assert card.ask_id not in reg.cards
        ns["ACTIVE"].clear()
        await ns["_drain_peer_reports"]("work"); await ns["_drain_peer_reports"]("work")
        reports = [v for v in calls if "SYNTHETIC" in v[2]]
        assert len(reports) == 2 and len(writes) <= 1
        assert all(r["state"] == "completed" for r in ns["PEER_REPORTS"].rows)
    asyncio.run(run())


def test_real_expiry_scheduler_releases_once_at_first_creation_deadline(monkeypatch, tmp_path):
    ns, reg, card, clock, calls, submit, _, _ = harness(monkeypatch, tmp_path)
    async def run():
        await submit({"type": "user_message", "text": "SYNTHETIC", "chat_id": "group:fleet-product", "peer_report": origin()})
        original = card.deadline
        reg.finish(card.origin, normal=True); reg.disconnect(card.origin, expected=True)
        assert card.deadline == original
        clock[0] = original
        count = 0
        async def sleep(_):
            nonlocal count
            count += 1
            if count > 1:
                await asyncio.sleep(0)
                raise asyncio.CancelledError()
        ns["asyncio"] = SimpleNamespace(sleep=sleep, create_task=asyncio.create_task, CancelledError=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError): await ns["_question_expiry_loop"]()
        await asyncio.sleep(0)
    asyncio.run(run())
    assert len(calls) == 1 and card.ask_id not in reg.cards


def test_normal_shutdown_during_claim_records_unknown_and_retains_original(monkeypatch, tmp_path):
    ns, reg, card, _, _, submit, _, _ = harness(monkeypatch, tmp_path)
    async def run():
        await submit({"type": "user_message", "text": "SYNTHETIC preserved", "chat_id": "group:fleet-product", "peer_report": origin()})
        reg.cancel(card, "gateway_disconnected")
        entered = asyncio.Event()
        async def wait(*args): entered.set(); await asyncio.Event().wait()
        ns["run_turn"] = wait
        task = asyncio.create_task(ns["_drain_peer_reports"]("work"))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert not ns["ACTIVE"] and ns["PEER_REPORTS"].rows[0]["state"] == "delivery_unknown"
    assert ns["PEER_REPORTS"].rows[0]["text"] == "SYNTHETIC preserved"


def test_corrupt_journal_refuses_without_rewriting_or_restoring_authority(tmp_path):
    path = tmp_path / "journal.json"
    raw = b'{"version":1,"reports":[{"state":"held"}]}'
    path.write_bytes(raw)
    with pytest.raises(ValueError): pr.Journal(path)
    assert path.read_bytes() == raw


def test_bounded_capacity_refusal_keeps_all_originals(tmp_path):
    journal = pr.Journal(tmp_path / "journal.json"); journal.MAX_RECORDS = 1
    p = origin()
    journal.hold(p, work="work", display=p["target_chat"], text="SYNTHETIC first", arrived=123., visible=True, cards=["old-correlation"], expires=600.)
    before = journal.path.read_bytes(); second = dict(p, id="b" * 32)
    with pytest.raises(ValueError):
        journal.hold(second, work="work", display=p["target_chat"], text="SYNTHETIC second", arrived=124., visible=True, cards=["old-correlation"], expires=600.)
    assert journal.path.read_bytes() == before and len(journal.rows) == 1


def test_qualified_peer_cannot_claim_choices_or_human_authority(monkeypatch, tmp_path):
    ns, reg, card, _, calls, submit, _, _ = harness(monkeypatch, tmp_path)
    frame = answer(card)
    frame.update(type="user_message", text="SYNTHETIC Q1: Approve ONE", peer_report=origin())
    asyncio.run(submit(frame))
    assert reg.cards.get(card.ask_id) is card and card.state == "pending" and card.answer == ""
    assert not calls and len(ns["PEER_REPORTS"].held()) == 1


async def current_decision_drain_fixture(monkeypatch, tmp_path, *, fresh_work="work"):
    """Real frontend/registry/drain/ordinary run_turn; memory-only provider sink."""
    from tests.codex.test_codex_questions import completed
    ns, reg, old_card, clock, calls, submit, logs, _ = harness(monkeypatch, tmp_path)
    compile_functions(BASE / "src/feishu_bridge/bridge/fleet_bridge.py", {"run_turn"}, ns)
    fresh = {}
    if fresh_work != "work":
        ns["_load_session"].__self__[fresh_work + "::codex"] = "thread"
        rows=json.loads(ns["_fs"].SESSIONS_FILE.read_text())
        rows[fresh_work+"::codex"]=rows["work::codex"]
        ns["_fs"].SESSIONS_FILE.write_text(json.dumps(rows))
        ns["APP_FRONT"].resolve_work_display_ex = lambda d: (
            (fresh_work, d, "") if d == "group:ops" else ("work", d, ""))
    async def provider_sink(work, prompt, *args):
        calls.append((work, af.DISPLAY_CHAT.get(), prompt))
        if len(calls) == 1:
            clock[0] += 1
            display = "group:fleet-product" if fresh_work == "work" else "group:ops"
            proc = object()
            owner = q.Origin(q.Binding(fresh_work, display, "thread", "fresh-turn",
                ns["_question_runtime_binding"](), q.PROTOCOL, "fresh-connection"), proc)
            ns["CODEX_PROCS"][fresh_work] = proc
            ns["CODEX_TURNS"][fresh_work] = SimpleNamespace(proc=proc, thread_id="thread", turn_id="fresh-turn")
            event = completed(); event["params"]["turnId"] = "fresh-turn"
            event["params"]["item"]["id"] = "SYNTHETIC-fresh-item"
            card = reg.observe(owner, event, process=proc, current=True)
            assert card is not None and ns["_question_valid"](card)
            reg.finish(owner, normal=True); reg.disconnect(owner, expected=True)
            assert ns["_question_valid"](card)
            fresh.update(card=card, deadline=card.deadline, expires=card.expires_at,
                         binding=card.origin.binding, questions=card.questions)
    ns["_run_turn_codex"] = provider_sink
    for ident, text in [("a" * 32, "SYNTHETIC original A"), ("b" * 32, "SYNTHETIC original B")]:
        value = origin(); value["id"] = ident
        await submit({"type": "user_message", "text": text, "chat_id": "group:fleet-product", "peer_report": value})
    originals = copy.deepcopy(ns["PEER_REPORTS"].rows)
    reg.cancel(old_card, "unrelated_input")
    await ns["_drain_peer_reports"]("work")
    return ns, reg, clock, calls, originals, fresh


def assert_original_report_metadata(ns, originals):
    for before, after in zip(originals, ns["PEER_REPORTS"].rows):
        assert {k: v for k, v in before.items() if k not in {"state", "reason"}} == {
            k: v for k, v in after.items() if k not in {"state", "reason"}}


@pytest.mark.parametrize("phase", ["pending", "received", "queued", "dispatching"])
def test_current_decision_drain_rechecks_between_each_original(monkeypatch, tmp_path, phase):
    async def run():
        ns, reg, clock, calls, originals, fresh = await current_decision_drain_fixture(monkeypatch, tmp_path)
        assert len(calls) == 1  # Parent fb2510 launches B and cancels the fresh card.
        card = fresh["card"]
        assert reg.cards.get(card.ask_id) is card and card.origin.normal and card.origin.disconnected
        if phase != "pending":
            assert reg.claim(answer(card), ns["_question_valid"]) is card
        if phase in {"queued", "dispatching"}: assert reg.transition(card, "queued", ns["_question_valid"])
        if phase == "dispatching": assert reg.transition(card, "dispatching", ns["_question_valid"])
        await ns["_drain_peer_reports"]("work")
        assert len(calls) == 1 and card.state == phase
        assert [r["state"] for r in ns["PEER_REPORTS"].rows] == ["completed", "held"]
        assert card.deadline == fresh["deadline"] and card.expires_at == fresh["expires"]
        assert card.origin.binding == fresh["binding"] and card.questions == fresh["questions"]
        assert_original_report_metadata(ns, originals)
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["ack", "possible_write", "cancel", "expired"])
def test_current_decision_drain_release_waits_exact_answer_task_then_once(monkeypatch, tmp_path, outcome):
    async def run():
        ns, reg, clock, calls, originals, fresh = await current_decision_drain_fixture(monkeypatch, tmp_path)
        assert len(calls) == 1
        card = fresh["card"]
        settled = asyncio.Event()
        task = asyncio.create_task(settled.wait())
        ns["_QUESTION_TASKS"].add(task); ns["_QUESTION_DRAIN_WORKS"]["work"] = 1
        task.add_done_callback(lambda t: ns["_question_task_done"]("work", t))
        if outcome in {"ack", "possible_write"}:
            assert reg.claim(answer(card), ns["_question_valid"]) is card
            assert reg.transition(card, "queued", ns["_question_valid"])
            assert reg.transition(card, "dispatching", ns["_question_valid"])
            reg.delivered(card, acknowledged=outcome == "ack")
        elif outcome == "cancel": reg.cancel(card, "unrelated_input")
        else:
            clock[0] = fresh["deadline"]
            reg.sweep(ns["_question_valid"])
        assert card.ask_id not in reg.cards
        await ns["_drain_peer_reports"]("work")
        assert len(calls) == 1 and ns["PEER_REPORTS"].rows[1]["state"] == "held"
        settled.set(); await task; await asyncio.sleep(0)
        assert not ns["_QUESTION_DRAIN_WORKS"] and not ns["_QUESTION_TASKS"]
        await ns["_drain_peer_reports"]("work"); await ns["_drain_peer_reports"]("work")
        assert len(calls) == 2 and all(r["state"] == "completed" for r in ns["PEER_REPORTS"].rows)
        assert calls[1] == (originals[1]["work"], originals[1]["display"],
                            ns["_stamp"](originals[1]["text"], originals[1]["arrived"], originals[1]["display"]))
        assert_original_report_metadata(ns, originals)
    asyncio.run(run())


def test_current_decision_drain_other_work_remains_independent(monkeypatch, tmp_path):
    async def run():
        ns, reg, clock, calls, originals, fresh = await current_decision_drain_fixture(
            monkeypatch, tmp_path, fresh_work="other-work")
        card = fresh["card"]
        assert len(calls) == 2 and reg.cards.get(card.ask_id) is card and ns["_question_valid"](card)
        assert all(r["state"] == "completed" for r in ns["PEER_REPORTS"].rows)
        assert_original_report_metadata(ns, originals)
    asyncio.run(run())

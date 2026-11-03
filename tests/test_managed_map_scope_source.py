"""Actual normal helpers/coordinator with fresh synthetic maps only."""
import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_bridge import fleet_managed_ingress as ingress
from feishu_bridge import fleet_managed_runtime as runtime
from feishu_bridge import fleet_sessions as sessions
from feishu_bridge import fleet_target_control as control


@pytest.fixture
def bound(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    home = tmp_path / "synthetic-home"; home.mkdir(mode=0o700)
    c = control.create(tmp_path / "control", str(home), "fixture-selector")
    b = {"release_id": "fixture", "closure_sha256": "a" * 64, "namespace": "fixture-ns"}
    epoch, op = c.enter_maintenance(0, "operator")
    startup = c.fork_startup(op, "startup", b)
    c.register_runtime(startup, b); c.finish_startup(startup)
    epoch = c.finish_maintenance(epoch, op); c.reopen(epoch, b, runtime_ready=True)
    r = runtime.Runtime(ingress.Selection(tmp_path / "control", "fixture-selector", home))
    p = home / "sessions.json"
    data = {"work::codex": {"active": None, "sessions": {
        "old": {"name": "\ufeffe\u0301旧", "ts": "retained", "detail": {"known": True}}}},
        "other::claude": {"active": "other-old", "sessions": {
            "other-old": {"name": "other", "ts": "retained"}}, "count": 1}}
    p.write_text(json.dumps(data, ensure_ascii=False)); p.chmod(0o600)
    monkeypatch.setattr(sessions, "SESSIONS_FILE", p)
    monkeypatch.setattr(sessions, "MANAGED_TRANSACTION", r.map_transaction)
    req, scope = r.prepare({"type": "user_message", "text": "synthetic work",
        "work_id": "fixture-work", "work_actor": "fixture-user", "client": "web.1009b",
        "message_origin": {"version": 1, "kind": "gateway_human"}},
        work_chat="work::codex", display_chat="group:fixture", engine="codex",
        bot="shopping", gateway_connection=True)
    r.admit(req, scope)
    return r, c, p, req, scope, data


def run(bound, fn):
    r, c, p, req, scope, data = bound
    async def execute(payload):
        fn()
    return asyncio.run(r.execute_new(req, scope, execute, lambda _: "completed"))


@pytest.mark.parametrize("key", ["other::claude", "work", "unreviewed::codex"])
def test_actual_record_cannot_change_other_chat_engine_or_namespace(bound, key):
    r, c, p, req, scope, data = bound
    old = p.read_bytes()
    with pytest.raises(control.Refused, match="database_unknown"):
        run(bound, lambda: sessions.record_session(key, "must-not-register"))
    assert p.read_bytes() == old
    assert not list(p.parent.glob("*.managed-*.tmp"))
    assert c.snapshot()["state"] == "UNKNOWN" and c.snapshot()["unresolved"] == 1
    assert c.snapshot()["active_leases"] == 2


def test_actual_record_preserves_all_history_and_works_during_drain(bound):
    r, c, p, req, scope, data = bound
    def update():
        c.begin_drain(c.snapshot()["epoch"])
        sessions.record_session("work::codex", "genuine-synthetic-new", "new work")
    run(bound, update)
    after = json.loads(p.read_bytes())
    assert after["other::claude"] == data["other::claude"]
    assert after["work::codex"]["sessions"]["old"] == data["work::codex"]["sessions"]["old"]
    assert set(after["work::codex"]["sessions"]) == {"old", "genuine-synthetic-new"}
    assert c.snapshot()["state"] == "DRAINING" and c.snapshot()["active_leases"] == 0
    c.seal_offline(c.snapshot()["epoch"])


def test_existing_selected_sid_updates_normal_time_without_losing_history(bound):
    r, c, p, req, scope, data = bound
    run(bound, lambda: sessions.record_session("work::codex", "old"))
    after = json.loads(p.read_bytes())
    assert after["work::codex"]["active"] == "old"
    old = after["work::codex"]["sessions"]["old"]
    assert old["name"].encode() == data["work::codex"]["sessions"]["old"]["name"].encode()
    assert old["detail"] == {"known": True} and type(old["detail"]["known"]) is bool
    assert after["other::claude"] == data["other::claude"]
    assert c.snapshot()["active_leases"] == 0


@pytest.mark.parametrize("mutation", [
    lambda d: d["work::codex"]["sessions"].pop("old"),
    lambda d: d["work::codex"]["sessions"]["old"].update(name="é旧"),
    lambda d: d["other::claude"].update(count=True),
    lambda d: d["other::claude"].update(count=1.0),
    lambda d: d["work::codex"]["sessions"].update(extra={"name": "fake", "ts": "fake"}),
    lambda d: d["work::codex"].update(rollover_pending=True),
])
def test_whole_map_candidate_cannot_delete_normalize_or_expand_records(bound, mutation):
    r, c, p, req, scope, data = bound
    old = p.read_bytes()
    candidate = copy.deepcopy(data); mutation(candidate)
    def update():
        with sessions.mutation():
            sessions.write_sessions(candidate)
    with pytest.raises(control.Refused, match="database_unknown"):
        run(bound, update)
    assert p.read_bytes() == old and c.snapshot()["active_leases"] == 2


def test_nested_helpers_use_one_original_writer_scope(bound):
    r, c, p, req, scope, data = bound
    def update():
        with sessions.mutation():
            sessions.record_session("work::codex", "genuine-synthetic-new")
            sessions.name_active("work::codex", "named")
            assert c.db.exists()
    run(bound, update)
    assert json.loads(p.read_bytes())["work::codex"]["sessions"]["genuine-synthetic-new"]["name"] == "named"
    assert c.snapshot()["active_leases"] == 0


def test_escaped_guard_is_invalid_after_original_transaction(bound):
    r, c, p, req, scope, data = bound
    kept = []
    def capture():
        with sessions.mutation():
            kept.append(sessions._MAP_TRANSACTION.get()[1])
    run(bound, capture)
    assert kept[0].active is False
    old = p.read_bytes()
    with pytest.raises(ingress.Refused, match="managed_map_scope_unknown"):
        kept[0](p, copy.deepcopy(data))
    assert p.read_bytes() == old and c.snapshot()["active_leases"] == 0


def test_wrong_home_mapping_refuses_before_read_or_child_lease(bound, monkeypatch):
    r, c, p, req, scope, data = bound
    foreign = p.parent.parent / "foreign.json"; foreign.write_text("DO NOT READ")
    monkeypatch.setattr(sessions, "SESSIONS_FILE", foreign)
    with pytest.raises(ingress.Refused, match="managed_map_home_unknown"):
        run(bound, lambda: sessions.record_session("work::codex", "new"))
    assert foreign.read_text() == "DO NOT READ"
    assert c.snapshot()["state"] == "UNKNOWN" and c.snapshot()["active_leases"] == 1


@pytest.mark.parametrize("active", [True, 17, "missing-old"])
def test_invalid_current_active_pointer_cannot_be_implicitly_repaired(bound, active):
    r, c, p, req, scope, data = bound
    data["work::codex"]["active"] = active
    p.write_text(json.dumps(data)); old = p.read_bytes()
    with pytest.raises(control.Refused):
        run(bound, lambda: sessions.record_session("work::codex", "new"))
    assert p.read_bytes() == old and c.snapshot()["active_leases"] == 2


def test_actual_legacy_rollover_stops_before_model_map_or_note_effects():
    source = Path(__file__).resolve().parents[1] / "src/feishu_bridge/fleet_bridge.py"
    tree = ast.parse(source.read_bytes())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_rollover_one")
    ns = {"Path": Path, "_fs": SimpleNamespace(MANAGED_TRANSACTION=object())}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), ns)
    # All legacy dependencies are deliberately absent: no dependency may be
    # touched before this actual selected guard returns.
    assert asyncio.run(ns["_rollover_one"]("work::codex", "old", Path("unused"), 3)) is False

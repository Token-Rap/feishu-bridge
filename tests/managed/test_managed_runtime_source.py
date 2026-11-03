import asyncio
import json

import pytest

from feishu_bridge.sessions import fleet_sessions as sessions
from feishu_bridge.managed import fleet_managed_ingress as ingress
from feishu_bridge.managed import fleet_managed_runtime as runtime
from feishu_bridge.sessions import fleet_target_control as control


@pytest.fixture
def managed(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    directory = tmp_path / "coordinator"
    c = control.create(directory, str(home), "fixture-selector")
    binding = {"release_id": "fixture-release", "closure_sha256": "a" * 64, "namespace": "fixture-ns"}
    epoch, operator = c.enter_maintenance(0, "operator")
    startup = c.fork_startup(operator, "startup", binding)
    c.register_runtime(startup, binding); c.finish_startup(startup)
    epoch = c.finish_maintenance(epoch, operator)
    c.reopen(epoch, binding, runtime_ready=True)  # Synthetic loader report only.
    r = runtime.Runtime(ingress.Selection(directory, "fixture-selector", home))
    path = home / "sessions.json"
    path.write_text(json.dumps({"work::codex": {"active": None, "sessions": {"old": {"name": "preserve"}}}}))
    path.chmod(0o600)
    monkeypatch.setattr(sessions, "SESSIONS_FILE", path)
    monkeypatch.setattr(sessions, "MANAGED_TRANSACTION", r.map_transaction)
    return r, c, path


def prepare(r, work_id="request-1"):
    return r.prepare({"type": "user_message", "text": "完整正文 " + "中" * 1000,
        "work_id": work_id, "work_actor": "fixture-user", "client": "web.1009b",
        "message_origin": {"version": 1, "kind": "gateway_human"}},
        work_chat="work::codex", display_chat="group:fixture", engine="codex",
        bot="shopping", gateway_connection=True)


def test_real_existing_parent_map_can_settle_during_drain(managed):
    r, c, path = managed
    req, scope = prepare(r)
    r.admit(req, scope)

    async def execute(payload):
        assert payload["text"] == json.loads(req.payload)["text"]
        c.begin_drain(c.snapshot()["epoch"])
        sessions.record_session("work::codex", "genuine-fixture-new")
        assert c.snapshot()["active_leases"] == 1

    asyncio.run(r.execute_new(req, scope, execute, lambda _: "completed"))
    snapshot = c.snapshot()
    assert snapshot["state"] == "DRAINING" and snapshot["active_leases"] == 0
    assert set(sessions.read_sessions()["work::codex"]["sessions"]) == {"old", "genuine-fixture-new"}
    c.seal_offline(snapshot["epoch"])


def test_map_without_exact_existing_work_grant_refuses_before_write(managed):
    r, c, path = managed
    original = path.read_bytes()
    with pytest.raises(ingress.Refused, match="managed_writer_unknown"):
        sessions.clear_session("work::codex")
    assert path.read_bytes() == original and c.snapshot()["active_leases"] == 0


def test_unknown_lifecycle_retains_dispatch_and_never_replays(managed):
    r, c, _ = managed
    req, scope = prepare(r)
    r.admit(req, scope)
    calls = []

    async def execute(payload):
        calls.append(payload)

    with pytest.raises(ingress.Refused, match="managed_lifecycle_unknown"):
        asyncio.run(r.execute_new(req, scope, execute, lambda _: None))
    snapshot = c.snapshot()
    assert snapshot["state"] == "UNKNOWN" and snapshot["unresolved"] == 1
    assert snapshot["active_leases"] == 1 and len(calls) == 1
    with pytest.raises(control.Refused):
        asyncio.run(r.execute_new(req, scope, execute, lambda _: "completed"))
    assert len(calls) == 1


def test_real_child_blocks_early_parent_settlement(managed):
    r, c, _ = managed
    req, scope = prepare(r)
    r.admit(req, scope)

    async def execute(payload):
        parent = runtime.CURRENT.get().parent
        c.fork_writer(parent, "still-alive-question", "question_continuation")

    with pytest.raises(control.Refused, match="descendants_unsettled"):
        asyncio.run(r.execute_new(req, scope, execute, lambda _: "completed"))
    assert c.snapshot()["state"] == "UNKNOWN" and c.snapshot()["active_leases"] == 2


def test_map_failure_retains_exact_child_and_sticky_hold(managed):
    r, c, path = managed
    req, scope = prepare(r)
    r.admit(req, scope)

    async def execute(payload):
        path.write_bytes(b"corrupt synthetic map")
        sessions.record_session("work::codex", "must-not-register")

    # The coordinator fault-isolates OSError-derived map refusal as its fixed
    # database_unknown; it does not export the private underlying exception.
    with pytest.raises(control.Refused, match="database_unknown"):
        asyncio.run(r.execute_new(req, scope, execute, lambda _: "completed"))
    assert path.read_bytes() == b"corrupt synthetic map"
    assert c.snapshot()["state"] == "UNKNOWN" and c.snapshot()["active_leases"] == 2


def test_boot_bind_never_claims_existing_queued_work(managed):
    r, c, _ = managed
    req, scope = prepare(r)
    r.admit(req, scope)
    before = c.snapshot()
    again = runtime.Runtime(r.selection)
    assert again.control.snapshot() == before
    assert before["queued"] == 1 and before["unresolved"] == 0


def test_newer_request_never_skips_retained_older_head(managed):
    r, c, _ = managed
    old, scope = prepare(r, "retained-older")
    new, _ = prepare(r, "new-user-request")
    r.admit(old, scope); r.admit(new, scope)

    async def forbidden(payload):
        pytest.fail("must not dispatch newer or replay older")

    with pytest.raises(control.Refused, match="claim_not_queued_head"):
        asyncio.run(r.execute_new(new, scope, forbidden, lambda _: "completed"))
    assert c.snapshot()["queued"] == 2 and c.snapshot()["unresolved"] == 0

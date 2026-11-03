"""Protocol cases matched against CLI 0.159.2's exported permission schemas."""
import asyncio
import copy

import pytest

from test_codex_approvals import answer, flush, harness, request
from feishu_bridge.codex import codex_approvals as ca


def permission_request(permissions, **extra):
    event = request(permissions=permissions, environmentId="local-fixture", startedAtMs=1, **extra)
    event["method"] = ca.PERMISSIONS_METHOD
    event["params"].pop("command")
    return event


@pytest.mark.parametrize("permissions", [
    {"network": {"enabled": True}},
    {"fileSystem": {"read": ["/fixture/read"], "write": ["/fixture/write"]}},
    {"fileSystem": {"write": ["C:\\fixture\\write"]}},
    {"fileSystem": {"entries": [{"access": "read", "path": {"type": "path", "path": "/fixture"}}]}},
    {"network": {"enabled": False}, "fileSystem": {"read": ["/fixture"], "write": None}},
])
def test_grant_only_exact_displayed_permissions_for_current_turn(permissions):
    async def run():
        m, sent, events, _ = harness()
        original = copy.deepcopy(permissions)
        ev = permission_request(permissions)
        assert m.feed(ev)
        card = events[0]
        q = card["questions"][0]
        assert "当前回合" in q["question"] and "local-fixture" in q["question"]
        assert q["options"][1]["label"] == ca.GRANT
        assert sent == []
        # Request buffers and submitted answers cannot expand the captured grant.
        permissions["network"] = {"enabled": True, "unexpected": True}
        m.answer(answer(card["ask_id"], ca.GRANT, permissions={"fileSystem": {"write": ["/"]}}, scope="session"))
        await flush()
        assert sent == [{"id": 10, "result": {"permissions": original, "scope": "turn"}}]
        assert "strictAutoReview" not in sent[0]["result"]
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("label", [ca.DECLINE, ca.ACCEPT, "好的", "grantForSession"])
def test_permission_denial_grants_nothing(label):
    async def run():
        m, sent, events, _ = harness()
        m.feed(permission_request({"network": {"enabled": True}}))
        m.answer(answer(events[0]["ask_id"], label))
        await flush()
        assert sent == [{"id": 10, "result": {"permissions": {}, "scope": "turn"}}]
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("permissions", [
    {}, [], {"unknown": True}, {"network": {"enabled": "yes"}},
    {"network": {"enabled": True, "hosts": ["example.test"]}},
    {"fileSystem": {"write": ["relative/path"]}},
    {"fileSystem": {"read": "all"}},
    {"fileSystem": {"extraRoot": "/", "write": ["/fixture"]}},
    {"fileSystem": {"write": ["/fixture"], "globScanMaxDepth": True}},
    {"fileSystem": {"entries": [{"access": "write", "path": {"type": "glob_pattern", "pattern": "**"}}]}},
    {"fileSystem": {"entries": [{"access": "write", "path": {"type": "special", "value": {"kind": "root"}}}]}},
    {"fileSystem": {"entries": [{"access": "execute", "path": {"type": "path", "path": "/fixture"}}]}},
])
def test_unknown_or_unsupported_shapes_are_not_granted(permissions):
    async def run():
        m, sent, events, _ = harness()
        m.feed(permission_request(permissions))
        await flush()
        assert not any(e["ev"] == "ask" for e in events)
        assert sent[0]["result"] == {"permissions": {}, "scope": "turn"}
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["timeout", "never", "wrong-turn", "shutdown"])
def test_permission_lifecycle_never_implicitly_grants(case):
    async def run():
        m, sent, events, _ = harness(timeout=0.001)
        if case == "never": m.enabled = False
        ev = permission_request({"network": {"enabled": True}})
        if case == "wrong-turn": ev["params"]["turnId"] = "stale"
        m.feed(ev)
        if case == "shutdown": await m.close()
        await asyncio.sleep(0.02)
        assert sent == ([] if case == "shutdown" else [{"id": 10, "result": {"permissions": {}, "scope": "turn"}}])
        await m.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", ["inProgress", "approved", "denied", "timedOut", "aborted"])
def test_auto_review_reports_only_actual_results_without_sending_accept(status):
    async def run():
        m, sent, events, _ = harness()
        event = {"method": "item/autoApprovalReview/completed", "params": {
            "threadId": "thread", "turnId": "turn", "reviewId": "review-1",
            "review": {"status": status, "rationale": "fixture rationale"}}}
        m.feed(event)
        m.feed(event)
        await flush()
        assert sent == [] and len(events) == 1
        assert events[0]["ev"] == "notice" and events[0]["chat_id"] == m.chat
        assert "fixture rationale" in events[0]["text"]
        assert not m.pending
        await m.close()
    asyncio.run(run())


def test_foreign_or_unknown_auto_review_cannot_claim_approval():
    async def run():
        m, sent, events, _ = harness()
        for turn, status in [("other", "approved"), ("turn", "new_future_status")]:
            m.feed({"method": "item/autoApprovalReview/completed", "params": {
                "threadId": "thread", "turnId": turn, "reviewId": "review-1", "review": {"status": status}}})
        assert not sent and not events
        await m.close()
    asyncio.run(run())


def test_reviewer_choice_is_only_an_official_route_not_auto_accept():
    assert ca.reviewer_params("") == {}
    assert ca.reviewer_params("user") == {"approvalsReviewer": "user"}
    assert ca.reviewer_params("auto_review") == {"approvalsReviewer": "auto_review"}
    with pytest.raises(ValueError): ca.reviewer_params("approve")
    with pytest.raises(ValueError): ca.reviewer_params("accept-all")


@pytest.mark.parametrize("observed", [None, "user", "unknown"])
def test_unconfirmed_auto_review_cannot_start_a_turn(observed):
    with pytest.raises(RuntimeError):
        ca.verify_reviewer({"approvalsReviewer": "auto_review"}, {"result": {"approvalsReviewer": observed}})


def test_verified_reviewer_and_inherited_legacy_clients():
    for value in ("auto_review", "guardian_subagent"):
        ca.verify_reviewer({"approvalsReviewer": "auto_review"}, {"result": {"approvalsReviewer": value}})
    ca.verify_reviewer({"approvalsReviewer": "user"}, {"result": {"approvalsReviewer": "user"}})
    ca.verify_reviewer({}, {"result": {}})

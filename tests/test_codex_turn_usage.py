from test_codex_adapter import b


def counts(i, o=10, r=3):
    return {"inputTokens": i, "outputTokens": o, "reasoningOutputTokens": r,
            "cachedInputTokens": i//2, "totalTokens": i+o}


def event(total, last, turn="u", thread="t"):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "turnId": turn, "tokenUsage": {"total": total, "last": last}}}


def test_multicall_new_thread_and_duplicates():
    t=b._CodexTurnUsage("t","u",new_thread=True)
    a=event(counts(100),counts(100)); t.feed(a); t.feed(a)
    t.feed(event(counts(300,30,9),counts(200,20,6)))
    assert t.usage()["totalTokens"]==330
    assert t.usage()["reasoningOutputTokens"]==9
    assert b._codex_usage_total(t.usage())==330


def test_resumed_thread_subtracts_explicit_startup_baseline():
    t=b._CodexTurnUsage("t","u",new_thread=False)
    t.feed(event(counts(1000,100,30),counts(100),turn="prior"),before_turn=True)
    t.feed(event(counts(1100,110,33),counts(100)))
    t.feed(event(counts(1300,130,39),counts(200,20,6)))
    assert t.usage()["totalTokens"]==330
    assert t.usage()["_fleetAccounting"]["baseline"]=="before-turn"


def test_resume_without_startup_uses_first_increment_not_full_history():
    t=b._CodexTurnUsage("t","u",new_thread=False)
    t.feed(event(counts(1100,110,33),counts(100)))
    t.feed(event(counts(1300,130,39),counts(200,20,6)))
    assert t.usage()["totalTokens"]==330


def test_foreign_turn_and_thread_do_not_pollute_current_delta():
    t=b._CodexTurnUsage("t","u",new_thread=True)
    assert not t.feed(event(counts(9000),counts(9000),thread="other"))
    assert not t.feed(event(counts(9000),counts(9000),turn="other"))
    t.feed(event(counts(100),counts(100)))
    assert t.usage()["totalTokens"]==110


def test_unknown_resumed_total_is_not_charged_as_current_turn():
    t=b._CodexTurnUsage("t","u",new_thread=False)
    assert not t.feed(event(counts(10000),None))
    assert t.usage()=={}


def test_reset_snapshot_keeps_known_lower_bound_without_negative_or_overcount():
    t=b._CodexTurnUsage("t","u",new_thread=True)
    t.feed(event(counts(100),counts(100)))
    t.feed(event(counts(10,1,0),counts(10,1,0)))
    assert t.usage()["totalTokens"]==110
    assert not t.usage()["_fleetAccounting"]["complete"]


def test_zero_and_invalid_values():
    t=b._CodexTurnUsage("t","u",new_thread=True)
    t.feed(event(counts(0,0,0),counts(0,0,0)))
    assert t.usage()["totalTokens"]==0
    assert t.counts({"inputTokens":True}) is None
    assert t.counts({"outputTokens":-1}) is None
    assert t.counts({"inputTokens":"99"}) is None


def test_last_only_compatibility_is_explicitly_incomplete():
    t=b._CodexTurnUsage("t","u",new_thread=False)
    t.feed(event(None,counts(100)))
    t.feed(event(None,counts(200)))
    assert t.usage()["totalTokens"]==210
    assert not t.usage()["_fleetAccounting"]["complete"]


def test_explicit_total_wins_and_reasoning_is_not_extra():
    assert b._codex_usage_total({"inputTokens":100,"outputTokens":20,"reasoningOutputTokens":7})==120
    assert b._codex_usage_total({"totalTokens":115,"inputTokens":100,"outputTokens":20})==115

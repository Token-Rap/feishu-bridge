"""Behavioral regression coverage."""
from feishu_bridge import task_card


def _track(**chats):
    return {c: {"sid": "", "items": items} for c, items in chats.items()}


def test_same_content_in_two_chats_shows_once():
    t = _track(app=[{"id": "1", "content": "等用户定范围", "status": "pending"}],
               **{"group:sample": [{"id": "1", "content": "等用户定范围", "status": "pending"}]})
    out = task_card.merged(t)
    assert len(out) == 1, f"同一条显示了 {len(out)} 次: {out}"
    assert out[0]["content"] == "等用户定范围"


def test_different_content_still_both_shown():
    """靶心的反面: 去重不许把**不同**的事吃掉 —— 那会让一条任务凭空消失。"""
    t = _track(app=[{"id": "1", "content": "改桥", "status": "pending"}],
               **{"group:sample": [{"id": "1", "content": "写契约", "status": "pending"}]})
    assert len(task_card.merged(t)) == 2


def test_done_in_one_chat_wins():
    """在群里 done 掉, 私聊那份还是 pending ⇒ 合并后必须显示**已完成**。

    不这样的话: 用户点了完成、卡上纹丝不动, 而且**没有任何报错**。
    """
    t = _track(app=[{"id": "1", "content": "同一件事", "status": "pending"}],
               **{"group:sample": [{"id": "1", "content": "同一件事", "status": "completed"}]})
    out = task_card.merged(t)
    assert len(out) == 1
    assert out[0]["status"] == "completed", f"状态取成了 {out[0]['status']}"


def test_in_progress_beats_pending():
    t = _track(app=[{"id": "1", "content": "x", "status": "in_progress"}],
               **{"group:sample": [{"id": "1", "content": "x", "status": "pending"}]})
    assert task_card.merged(t)[0]["status"] == "in_progress"


def test_order_is_stable():
    """序号要稳 —— `start N`/`done N` 认的就是用户在卡上看到的那个数。"""
    t = _track(app=[{"id": "1", "content": "a", "status": "pending"},
                    {"id": "2", "content": "b", "status": "pending"}],
               **{"group:sample": [{"id": "1", "content": "b", "status": "pending"},
                                   {"id": "2", "content": "c", "status": "pending"}]})
    assert [x["content"] for x in task_card.merged(t)] == ["a", "b", "c"]


# 广播时移除会话标签，CLI 写回仍可使用标签。
def test_broadcast_strips_chat_tag(monkeypatch):
    """合并后的任务不应携带单一会话标签。"""
    from feishu_bridge import fleet_bridge as fb
    sent = []
    monkeypatch.setattr(fb, "_TASK_TRACK",
                        {"app": {"sid": "", "items": [
                            {"id": "1", "content": "同一件事", "status": "pending"}]},
                         "group:sample": {"sid": "", "items": [
                             {"id": "1", "content": "同一件事", "status": "pending"}]}})
    monkeypatch.setattr(fb, "_card_chats", lambda: ["app"])
    monkeypatch.setattr(fb, "_task_track_mtime", lambda: 0)
    monkeypatch.setattr(fb, "_broadcast_todos",
                        lambda items, **kw: sent.append(items))
    assert fb._push_task_card() is True
    assert len(sent) == 1 and len(sent[0]) == 1, f"去重没生效: {sent}"
    assert "_chat" not in sent[0][0], f"广播里还带着 _chat: {sent[0][0]}"

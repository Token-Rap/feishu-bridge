"""Behavioral regression coverage."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import task_card

SCRIPT = str(Path(__file__).resolve().parent.parent / "task_card.py")


@pytest.fixture
def home(tmp_path):
    (tmp_path / "sessions.json").write_text(json.dumps(
        {"app": {"active": "s1"}, "group:fleet-product": {"active": "s2"}}),
        encoding="utf-8")
    return tmp_path


def _run(home, *args):
    return subprocess.run([sys.executable, SCRIPT, "--home", str(home), *args],
                          capture_output=True, text=True)


def _track(home):
    return json.loads((home / "task_track.json").read_text(encoding="utf-8"))


def _items(home, chat):
    return (_track(home).get(chat) or {}).get("items") or []


def test_two_sessions_do_not_clobber_each_other(home):
    """靶心: 两个会话各 set 各的, **谁也不覆盖谁**。"""
    assert _run(home, "--chat", "app", "set", "私聊的活").returncode == 0
    assert _run(home, "--chat", "group:fleet-product", "set", "群里的活").returncode == 0
    assert [i["content"] for i in _items(home, "app")] == ["私聊的活"]
    assert [i["content"] for i in _items(home, "group:fleet-product")] == ["群里的活"]


def test_show_merges_both_and_marks_provenance(home):
    _run(home, "--chat", "app", "set", "私聊的活")
    _run(home, "--chat", "group:fleet-product", "set", "群里的活")
    out = _run(home, "--chat", "app", "show").stdout
    assert "私聊的活" in out and "群里的活" in out, "没合并"
    assert "[group:fleet-product]" in out, \
        "别人的条目没标来源 —— 不标的话它在你卡上就是凭空冒出来的一条"
    assert "[app]" not in out, "自己的条目不该标来源"


def test_done_uses_merged_index_and_updates_the_owner(home):
    """⚠ 序号认的是**合并后**那一套 = 用户在卡上看到的那个数。

    只按自己那份编号的话, 你说 done 2 划掉的会是别的一条, **而且看起来完全正常**。
    """
    _run(home, "--chat", "app", "set", "私聊的活")
    _run(home, "--chat", "group:fleet-product", "set", "群里的活")
    # 合并序: app(第1) → group(第2)。从**私聊**这侧划掉第 2 条 = 群里那条
    assert _run(home, "--chat", "app", "done", "2").returncode == 0
    assert _items(home, "group:fleet-product")[0]["status"] == "completed"
    assert _items(home, "app")[0]["status"] == "pending", "划错人了"


def test_clear_only_clears_your_own(home):
    """换会话/清空只动自己那份 —— 不然任一边 /new 都能把对方的活清掉。"""
    _run(home, "--chat", "app", "set", "私聊的活")
    _run(home, "--chat", "group:fleet-product", "set", "群里的活")
    _run(home, "--chat", "app", "clear")
    assert _items(home, "app") == []
    assert [i["content"] for i in _items(home, "group:fleet-product")] == ["群里的活"]


def test_ambiguous_without_chat_refuses_loudly(home):
    """两个会话时不许猜 —— 猜错就是写进别人的卡, 而那是静默的。"""
    r = _run(home, "set", "谁的活?")
    assert r.returncode == 1 and "--chat" in r.stderr, r
    assert not (home / "task_track.json").exists(), "罢工了却还是写了文件"


def test_merged_order_is_stable():
    """⚠ bridge 和本脚本**共用** `merged()` —— 用户看到的序号和 done N 认的必须同一套。"""
    t = {"group:x": {"items": [{"id": "1", "content": "g"}]},
         "app": {"items": [{"id": "1", "content": "a"}]}, "_v": 2}
    assert [i["content"] for i in task_card.merged(t)] == ["a", "g"]
    assert [i["content"] for i in task_card.merged(dict(reversed(list(t.items()))))] == ["a", "g"]


def test_bridge_shares_the_same_merge_function():
    """别在桥里另写一份合并 —— 两份迟早漂开, 而漂开的表现是「我说 done 5, 划掉的是第 6 条」。"""
    src = (Path(__file__).resolve().parent.parent / "fleet_bridge.py").read_text(encoding="utf-8")
    assert "task_card.merged(" in src, "桥没用共用的 merged()"


def test_bridge_pushes_the_card_to_every_live_chat():
    """⚠ 原来写死 `APP_FRONT.chat_id` ⇒ 群聊里从来没有过任务卡。"""
    src = (Path(__file__).resolve().parent.parent / "fleet_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _push_task_card()")
    body = src[i:i + 700]
    assert "_card_chats()" in body, "没按「所有活着的会话」播"
    assert "chat=c" in body, "没逐个指定 chat_id —— 轮询在回合之外跑, CUR_CHAT 是空的"

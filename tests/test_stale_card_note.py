"""过期任务卡应在适当的回合提醒，避免状态长期失真。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-stale-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def _card(status: str, text: str = "读代码", age_s: float = 0) -> None:
    """摆一张卡: 内存里的条目 + 文件的 mtime (**判据是文件的 mtime, 不是回合数**)。"""
    b._TASK_TRACK.clear()
    b._TASK_TRACK["c1"] = {"sid": "s", "items": [{"text": text, "status": status}]}
    b._TASK_TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    b._TASK_TRACK_FILE.write_text("{}", encoding="utf-8")
    old = time.time() - age_s
    os.utime(b._TASK_TRACK_FILE, (old, old))


def test_fresh_card_is_never_nagged():
    """刚动过的卡不打扰 —— 否则提醒会变成每轮都有的噪音, 然后被无视。"""
    _card("in_progress", age_s=60)
    assert b._stale_card_note() == ""


def test_stale_card_with_running_item_gets_one_line():
    """Behavioral regression coverage."""
    _card("in_progress", text="等用户输一次密码", age_s=47 * 60)
    note = b._stale_card_note()
    assert note, "卡放旧了却没提醒"
    assert "等用户输一次密码" in note, f"没带上那条的原文: {note}"
    assert "47" in note, f"没说放了多久: {note}"


def test_completed_only_card_is_silent():
    """全做完的卡不提醒 —— 它没有"该划却没划"的东西, 提醒了就是纯噪音。"""
    _card("completed", age_s=99 * 60)
    assert b._stale_card_note() == ""


def test_no_card_file_is_silent():
    """从没写过卡 = 没有清单要维护, 别硬塞一条提醒。"""
    b._TASK_TRACK.clear()
    try:
        b._TASK_TRACK_FILE.unlink()
    except OSError:
        pass
    assert b._stale_card_note() == ""


def test_note_never_raises():
    """Behavioral regression coverage."""
    b._TASK_TRACK.clear()
    b._TASK_TRACK["c1"] = {"sid": "s", "items": ["这不是 dict"]}   # 坏数据
    b._TASK_TRACK_FILE.write_text("{}", encoding="utf-8")
    old = time.time() - 99 * 60
    os.utime(b._TASK_TRACK_FILE, (old, old))
    assert b._stale_card_note() == ""          # 不抛, 也不硬提醒


def test_stamp_carries_the_note_and_label_strips_it():
    """提醒跟着消息进这一轮, 但**不许污染人类可读标签**。

    ⚠ `_msg_label` 原来只滤 `[消息时间 ]` 那一行 —— 新加的提醒行如果不一起滤掉,
    它就会跑进日志和界面上的消息标签里, 而那是给人看"这条消息说了啥"的地方。
    """
    _card("in_progress", text="改代码", age_s=45 * 60)
    stamped = b._stamp("用户说的话")
    assert b._STALE_CARD_MARK in stamped and "改代码" in stamped
    assert "用户说的话" in stamped
    assert b._msg_label(stamped).startswith("用户说的话"), b._msg_label(stamped)

    _card("in_progress", age_s=60)             # 卡是新的 → 一个字都不加
    assert b._STALE_CARD_MARK not in b._stamp("用户说的话")

















def _real_card(*rows, age_s: float = 60 * 60) -> None:
    """摆一张**按 task_card.py 真实写盘格式**的卡 (键是 `content`)。"""
    b._TASK_TRACK.clear()
    b._TASK_TRACK["c1"] = {"sid": "s", "items": [
        {"id": str(i + 1), "content": c, "activeForm": "", "status": st}
        for i, (c, st) in enumerate(rows)]}
    b._TASK_TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    b._TASK_TRACK_FILE.write_text("{}", encoding="utf-8")
    old = time.time() - age_s
    os.utime(b._TASK_TRACK_FILE, (old, old))


def test_real_card_format_quotes_the_item():
    _real_card(("等 sample-agent 在 ConPTY 上跑对照实验", "in_progress"))
    note = b._stale_card_note()
    assert "「」" not in note, f"引号不应为空: {note!r}"
    assert "等 sample-agent" in note, note


def test_real_card_format_counts_the_others():
    _real_card(("甲", "in_progress"), ("乙", "in_progress"), ("丙", "completed"))
    note = b._stale_card_note()
    assert "甲" in note and "另有 1 条" in note, note


def test_legacy_text_key_still_read():
    """兜底: 改键名不许把可能存在的旧格式读丢了。"""
    _card("in_progress", text="老格式的条目", age_s=60 * 60)
    assert "老格式的条目" in b._stale_card_note()


def test_question_is_about_the_whole_card_not_the_one_item():
    """Behavioral regression coverage."""
    _real_card(("在做的事", "in_progress"))
    note = b._stale_card_note()
    assert "这张卡还是当前这件事吗" in note, note
    assert "整张重列" in note, note


def test_bridge_note_tells_bots_to_mark_waiting_items_pending():
    """「等别人」用 `pending` 表达, 不给 task_card 加字段。

    理由: 加一个 `waiting_on` 字段等于要求每个 bot 都维护它, 而**没维护的那些看起来
    跟维护了的一样** —— 又是"两种情况收方分辨不出来"。用现成状态表达:
    等别人 = 不是我在做 = pending; 提醒只看 in_progress ⇒ 合理的闲置自动不打扰。
    """
    note = b._BRIDGE_NOTE
    assert "pending" in note and "等谁" in note, "『等别人标 pending』这条规则没进常驻提示"

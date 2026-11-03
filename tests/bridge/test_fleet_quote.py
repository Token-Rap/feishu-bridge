"""fleet_quote 单测 (无 lark 依赖): 引用判定 + 出站正文缓存落盘。"""
from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.bridge.fleet_quote import (  # noqa: E402
    load_sent_cache,
    pick_quote_ref,
    remember_sent,
    save_sent_cache,
)


def test_no_parent_id_is_no_quote():
    # 普通消息 / 仅线程 root (root_id 不再参与) → 不前置
    assert pick_quote_ref(None, None) is None
    assert pick_quote_ref("", "om_x") is None


def test_new_parent_id_used():
    assert pick_quote_ref("om_aaa", None) == "om_aaa"
    assert pick_quote_ref("om_bbb", "om_aaa") == "om_bbb"   # 换了新引用 → 用


def test_repeated_ref_still_used():


    assert pick_quote_ref("om_x100b6c30", "om_x100b6c30") == "om_x100b6c30"


def test_last_ref_arg_optional():
    assert pick_quote_ref("om_a") == "om_a"


# ---------- remember_sent ----------

def test_remember_sent_adds_and_noops_on_empty():
    c: "OrderedDict[str, str]" = OrderedDict()
    remember_sent(c, "om_1", "你好")
    assert c["om_1"] == "你好"
    remember_sent(c, "", "x")
    remember_sent(c, "om_2", "")
    remember_sent(c, None, None)
    assert list(c.keys()) == ["om_1"]


def test_remember_sent_rewrite_refreshes_recency():
    """重写已有 id → 它变成最新, 逐出的是**次旧**那条 (LRU 而非 FIFO)。

    保留对引用行为的回归覆盖。
    """
    c: "OrderedDict[str, str]" = OrderedDict()
    remember_sent(c, "om_1", "a", cap=2)
    remember_sent(c, "om_2", "b", cap=2)
    remember_sent(c, "om_1", "a2", cap=2)      # 重写 om_1 → 它变最新
    remember_sent(c, "om_3", "c", cap=2)       # 逐出的应是 om_2, 不是 om_1
    assert "om_2" not in c
    assert c["om_1"] == "a2" and c["om_3"] == "c"


def test_remember_sent_evicts_oldest_over_cap():
    c: "OrderedDict[str, str]" = OrderedDict()
    for i in range(5):
        remember_sent(c, f"om_{i}", f"t{i}", cap=3)
    assert list(c.keys()) == ["om_2", "om_3", "om_4"]


# ---------- load / save_sent_cache (落盘 → 重启不丢) ----------

def test_save_then_load_roundtrip(tmp_path):
    c: "OrderedDict[str, str]" = OrderedDict()
    remember_sent(c, "om_1", "第一条")
    remember_sent(c, "om_2", "第二条\n带换行")
    p = tmp_path / "sub" / "sent_card_cache.json"   # 父目录不存在也要能建
    save_sent_cache(c, p)
    loaded = load_sent_cache(p)
    assert loaded == c
    assert list(loaded.keys()) == ["om_1", "om_2"]   # 顺序保持


def test_load_missing_file_returns_empty(tmp_path):
    assert load_sent_cache(tmp_path / "nope.json") == OrderedDict()


def test_load_corrupt_or_nondict_returns_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_sent_cache(bad) == OrderedDict()
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert load_sent_cache(arr) == OrderedDict()


def test_load_truncates_to_cap_newest(tmp_path):
    c = OrderedDict((f"om_{i}", f"t{i}") for i in range(5))
    p = tmp_path / "c.json"
    save_sent_cache(c, p)
    assert list(load_sent_cache(p, cap=2).keys()) == ["om_3", "om_4"]


def test_load_skips_non_string_entries(tmp_path):
    p = tmp_path / "mixed.json"
    p.write_text('{"om_1": "ok", "om_2": 123, "om_3": "good"}', encoding="utf-8")
    assert load_sent_cache(p) == OrderedDict([("om_1", "ok"), ("om_3", "good")])


def test_save_failure_is_silent(tmp_path):
    # 父级是文件 → 建目录/写入必失败, 但必须静默不抛 (绝不拖垮主消息流程)
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    save_sent_cache(OrderedDict([("a", "b")]), blocker / "nested" / "c.json")


def test_load_notify_entry(tmp_path):
    from feishu_bridge.bridge.fleet_quote import load_notify_entry
    p = tmp_path / "notify_card_cache.json"
    assert load_notify_entry(p, "om_x") is None            # 文件不存在
    p.write_text('{"om_x": "**待办**\\n内容"}', encoding="utf-8")
    assert load_notify_entry(p, "om_x") == "**待办**\n内容"  # 命中
    assert load_notify_entry(p, "om_y") is None            # id 不在
    p.write_text("not json", encoding="utf-8")
    assert load_notify_entry(p, "om_x") is None            # 坏 JSON 静默


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

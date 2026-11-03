"""Tests for 多会话持久 (fleet_sessions.py) — 纯 stdlib, 无 lark/sdk 依赖.

覆盖 Phase 3 多会话逻辑 (record/active/name/list/resolve 歧义/clear-保留/迁移/session_exists),
含对抗审查命中的边界: resolve 精确 id 优先于前缀歧义、旧扁平格式迁移、fork 多会话.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_sessions as fs  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """每个测试用独立 tmp sessions.json."""
    monkeypatch.setattr(fs, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(fs, "LOG", None)
    return tmp_path


# ── record / load / active ───────────────────────────────────────────
def test_record_then_load_active(store):
    assert fs.load_session("c") is None
    fs.record_session("c", "sid1", preview="第一句")
    assert fs.load_session("c") == "sid1"


def test_record_default_name_from_preview_truncated(store):
    fs.record_session("c", "sid1", preview="x" * 50 + "\n第二行")
    _, items = fs.list_sessions("c")
    name = items[0][1]
    assert len(name) == 24 and "\n" not in name


def test_record_existing_keeps_name_updates_ts(store):
    fs.record_session("c", "sid1", preview="原名")
    fs.name_active("c", "我命名的")
    fs.record_session("c", "sid1", preview="不该覆盖")    # 已存在 → 不改 name
    _, items = fs.list_sessions("c")
    assert items[0][1] == "我命名的"


# ── name ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("active", ["bad", "other"])
def test_discard_only_unusable_id(store, active):
    other = {"active": "claude", "sessions": {"claude": {"name": "keep"}}}
    fs.write_sessions({"c::codex": {
        "active": active, "sessions": {"bad": {}, "other": {"name": "keep"}},
        "custom": "preserve",
    }, "c": other})
    fs.discard_session("c::codex", "bad")
    data = fs.read_sessions()
    assert data["c::codex"] == {
        "active": None if active == "bad" else "other",
        "sessions": {"bad": {}, "other": {"name": "keep"}}, "custom": "preserve",
    }
    assert data["c"] == other
    assert fs.resolve_session("c::codex", "bad") == ["bad"]


def test_discard_legacy_session(store):
    fs.write_sessions({"c::codex": "bad"})
    fs.discard_session("c::codex", "bad")
    assert fs.read_sessions() == {"c::codex": {
        "active": None, "sessions": {"bad": {"name": "", "ts": ""}}}}


def test_discard_different_active_does_not_write(store):
    fs.record_session("c", "current")
    revision = fs.sessions_revision()
    fs.discard_session("c", "other")
    assert fs.sessions_revision() == revision


def test_record_replacement_preserves_all_original_metadata(store):
    original = {"active": "old", "sessions": {
        "old": {"name": "keep", "ts": "original", "extra": [1, 2]}}, "custom": True}
    fs.write_sessions({"c": original, "other": {"untouched": 1}})
    assert fs.record_replacement("c", "new", expected="old",
                                  revision=fs.sessions_revision(), preview="new question")
    data = fs.read_sessions()
    assert data["c"]["sessions"]["old"] == original["sessions"]["old"]
    assert data["c"]["custom"] is True and data["other"] == {"untouched": 1}
    assert data["c"]["active"] == "new"
    assert fs.resolve_session("c", "old") == ["old"]


@pytest.mark.parametrize("change", ["active", "aba", "metadata", "malformed"])
def test_replacement_refuses_observed_registry_change(store, change):
    fs.record_session("c", "old")
    revision = fs.sessions_revision()
    if change in {"active", "aba"}:
        fs.set_active("c", "other")
        if change == "aba":
            fs.set_active("c", "old")
    elif change == "metadata":
        fs.name_active("c", "changed")
    else:
        fs.SESSIONS_FILE.write_text("incomplete json")
    before = fs.SESSIONS_FILE.read_bytes()
    assert not fs.record_replacement("c", "new", expected="old", revision=revision)
    assert fs.SESSIONS_FILE.read_bytes() == before


def test_replacement_does_not_reuse_an_existing_historical_id(store):
    fs.record_session("c", "historical")
    fs.record_session("c", "old")
    before = fs.SESSIONS_FILE.read_bytes()
    assert not fs.record_replacement("c", "historical", expected="old",
                                      revision=fs.sessions_revision())
    assert fs.SESSIONS_FILE.read_bytes() == before


def test_replacement_write_failure_retains_original(store, monkeypatch):
    fs.record_session("c", "old")
    before = fs.SESSIONS_FILE.read_bytes()
    monkeypatch.setattr(fs, "write_sessions", lambda data: None)
    assert not fs.record_replacement("c", "new", expected="old", revision=fs.sessions_revision())
    assert fs.SESSIONS_FILE.read_bytes() == before


def test_name_active(store):
    fs.record_session("c", "sid1")
    assert fs.name_active("c", "回测线程") is True
    assert fs.list_sessions("c")[1][0][1] == "回测线程"


def test_name_active_no_active_returns_false(store):
    assert fs.name_active("c", "x") is False


# ── clear (/new) 保留历史 ─────────────────────────────────────────────
def test_clear_sets_active_none_keeps_list(store):
    fs.record_session("c", "sid1")
    fs.record_session("c", "sid2")
    fs.clear_session("c")
    assert fs.load_session("c") is None
    assert len(fs.list_sessions("c")[1]) == 2


# ── fork 多会话 ───────────────────────────────────────────────────────
def test_fork_two_sessions_active_switches(store):
    fs.record_session("c", "sid1", preview="原")
    fs.record_session("c", "sid2", preview="分叉")      # 模拟 fork: 新 sid
    active, items = fs.list_sessions("c")
    assert active == "sid2" and len(items) == 2
    assert {s for s, _, _ in items} == {"sid1", "sid2"}


# ── set_active / list 排序 ────────────────────────────────────────────
def test_set_active_and_list_sorted_desc(store):
    # 显式 ts (避免同秒内 record 导致 ts 相等、排序退化为插入序)
    fs.write_sessions({"c": {"active": "new", "sessions": {
        "old": {"name": "老", "ts": "2026-01-01T00:00:00+00:00"},
        "new": {"name": "新", "ts": "2026-06-01T00:00:00+00:00"}}}})
    active, items = fs.list_sessions("c")
    assert active == "new"
    assert items[0][0] == "new"          # ts 倒序, 最新在前
    fs.set_active("c", "old")
    assert fs.load_session("c") == "old"


# ── resolve 歧义 (对抗审查命中) ───────────────────────────────────────
def test_resolve_exact_id_unique_over_prefix(store):
    fs.record_session("c", "sid-aaa")
    fs.record_session("c", "sid-aaa-extra")              # sid-aaa 是 sid-aaa-extra 的前缀
    assert fs.resolve_session("c", "sid-aaa") == ["sid-aaa"]   # 精确命中→唯一


def test_resolve_prefix_ambiguous(store):
    fs.record_session("c", "sid-trade-1")
    fs.record_session("c", "sid-trade-2")
    assert len(fs.resolve_session("c", "sid-trade")) == 2


def test_resolve_name_unique_and_ambiguous(store):
    fs.record_session("c", "s1", preview="trading idea")
    fs.record_session("c", "s2", preview="trading idea v2")
    fs.record_session("c", "s3", preview="ops 线程")
    assert len(fs.resolve_session("c", "trading")) == 2   # 名字子串歧义
    assert fs.resolve_session("c", "ops") == ["s3"]       # 唯一
    assert fs.resolve_session("c", "zzz") == []           # 无
    assert fs.resolve_session("c", "") == []


# ── chat_entry / 旧扁平迁移 ───────────────────────────────────────────
def test_chat_entry_migrates_old_flat_str():
    e = fs.chat_entry({"c": "old-sid"}, "c")
    assert e["active"] == "old-sid" and "old-sid" in e["sessions"]


def test_chat_entry_missing_and_dict():
    assert fs.chat_entry({}, "c") == {"active": None, "sessions": {}}
    e = fs.chat_entry({"c": {"active": "x", "sessions": {"x": {}}}}, "c")
    assert e["active"] == "x"


def test_migrate_once_persists_normalized(store):
    fs.write_sessions({"c": "flat-sid", "d": {"active": "y", "sessions": {"y": {}}}})
    fs.migrate_sessions_once()
    raw = fs.read_sessions()
    assert isinstance(raw["c"], dict) and raw["c"]["active"] == "flat-sid"
    assert raw["d"]["active"] == "y"     # 已是新格式不动


def test_migrate_once_noop_when_no_flat(store):
    fs.write_sessions({"c": {"active": "x", "sessions": {"x": {}}}})
    before = fs.read_sessions()
    fs.migrate_sessions_once()
    assert fs.read_sessions() == before


# ── session_exists (PROJECTS_DIR 可覆盖) ──────────────────────────────
def test_session_exists(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    (proj / "some-cwd-slug").mkdir(parents=True)
    (proj / "some-cwd-slug" / "abc-123.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fs, "PROJECTS_DIR", proj)
    assert fs.session_exists("abc-123") is True
    assert fs.session_exists("nope-999") is False
    assert fs.session_exists("") is False


# ── 原子写 / 损坏兜底 ─────────────────────────────────────────────────
def test_read_corrupt_returns_empty(store):
    (store / "sessions.json").write_text("{ not json", encoding="utf-8")
    assert fs.read_sessions() == {}


def test_record_empty_sid_noop(store):
    fs.record_session("c", "", preview="x")
    assert fs.load_session("c") is None


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

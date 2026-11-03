"""Behavioral regression coverage."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
_HOME = tempfile.mkdtemp(prefix="bridge-tasktrack-")
sys.argv = [_argv[0], _HOME]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "oc_tasktrack"


def _write_file(items, v=None):
    """模拟**外部**写入 (人手改 / 别的进程)。"""
    payload = {CHAT: {"sid": "s1", "items": items}, "_v": v if v is not None else b._TASK_TRACK_V}
    b._TASK_TRACK_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # mtime 分辨率兜底: 同一秒内的两次写在某些文件系统上看不出差别
    st = b._TASK_TRACK_FILE.stat()
    import os
    os.utime(b._TASK_TRACK_FILE, (st.st_atime, st.st_mtime + 1))


def _setup(mem_items):
    b._TASK_TRACK.clear()
    b._TASK_TRACK[CHAT] = {"sid": "s1", "items": list(mem_items)}
    b._TASK_TRACK_SEEN_MTIME = 0.0
    b._TASK_TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)


# ── 外部改动要被认到 ────────────────────────────────────────────────
def test_external_edit_is_picked_up():
    """靶心: 手改文件把 13 标成 completed, 内存要跟着变。"""
    _setup([{"id": 13, "status": "in_progress", "content": "SDK 全铺"}])
    _write_file([{"id": 13, "status": "completed", "content": "SDK 全铺"}])
    assert b._reload_task_track_if_changed() is True
    assert b._TASK_TRACK[CHAT]["items"][0]["status"] == "completed"


def test_change_is_reported_once_not_every_tick():
    """变化只报一次 —— 每 5 秒播一次清单就是自己制造噪音。

    (原来这条我写成「第一次必 True」, 但当时文件内容和内存**一模一样** ——
     那种情况返回 False 才是对的: 判据是"内容变了", 不是"文件被写过"。
     测试写错了, 不是代码错了。)"""
    _setup([{"id": 1, "status": "pending", "content": "x"}])
    _write_file([{"id": 1, "status": "completed", "content": "x"}])   # 真的变了
    assert b._reload_task_track_if_changed() is True
    assert b._reload_task_track_if_changed() is False


def test_same_content_new_mtime_is_not_a_change():
    """touch 一下文件但内容一样 → 不算变化 (避免噪音广播)。"""
    _setup([{"id": 1, "status": "pending", "content": "x"}])
    _write_file([{"id": 1, "status": "pending", "content": "x"}])
    b._reload_task_track_if_changed()
    _write_file([{"id": 1, "status": "pending", "content": "x"}])   # 同内容, 新 mtime
    assert b._reload_task_track_if_changed() is False


def test_our_own_write_is_skipped():
    """Behavioral regression coverage."""
    _setup([{"id": 1, "status": "pending", "content": "x"}])
    b._save_task_track(CHAT)
    assert b._reload_task_track_if_changed() is False



def test_broken_json_keeps_memory():
    """一次手滑写坏文件, 不能变成"任务卡全没了"。"""
    _setup([{"id": 1, "status": "pending", "content": "重要"}])
    b._TASK_TRACK_FILE.write_text("{ 这不是 json", encoding="utf-8")
    assert b._reload_task_track_if_changed() is False
    assert b._TASK_TRACK[CHAT]["items"][0]["content"] == "重要"


def test_version_mismatch_keeps_memory():
    """版本不符与启动时同口径: 丢弃这份文件, 但**别动内存**。"""
    _setup([{"id": 1, "status": "pending", "content": "重要"}])
    _write_file([{"id": 9, "status": "completed", "content": "旧格式"}], v=1)
    assert b._reload_task_track_if_changed() is False
    assert b._TASK_TRACK[CHAT]["items"][0]["content"] == "重要"


def test_missing_file_is_not_a_change():
    _setup([{"id": 1, "status": "pending", "content": "x"}])
    b._TASK_TRACK_FILE.unlink(missing_ok=True)
    assert b._reload_task_track_if_changed() is False
    assert b._TASK_TRACK[CHAT]["items"]


# ── 重载之后, 会话拿到的就是新的 ────────────────────────────────────
def test_session_tasks_sees_reloaded_items(monkeypatch):
    _setup([{"id": 13, "status": "in_progress", "content": "SDK 全铺"}])
    _write_file([{"id": 13, "status": "completed", "content": "SDK 全铺"}])
    b._reload_task_track_if_changed()
    monkeypatch.setattr(b, "_load_session", lambda _c: "s1")
    assert b._session_tasks(CHAT)[0]["status"] == "completed"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

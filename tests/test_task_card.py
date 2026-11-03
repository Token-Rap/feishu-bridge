"""Behavioral regression coverage."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "src/feishu_bridge/task_card.py"
CHAT = "oc_abc123"
SID = "sid-99999999"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "sessions.json").write_text(json.dumps(
        {CHAT: {"active": SID, "sessions": {SID: {"name": "x", "ts": "t"}}}}),
        encoding="utf-8")
    return tmp_path


def _run(home, *args):
    return subprocess.run([sys.executable, str(_SCRIPT), "--home", str(home), *args],
                          capture_output=True, text=True)


def _track(home) -> dict:
    return json.loads((home / "task_track.json").read_text(encoding="utf-8"))


def test_set_writes_the_format_bridge_accepts(home):
    """靶心之一: _v 必须是 2 —— 版本不符时 bridge **整份丢弃且不报错**。"""
    r = _run(home, "set", "读代码|in_progress", "改代码")
    assert r.returncode == 0, r.stderr
    t = _track(home)
    assert t["_v"] == 2, t
    assert [i["content"] for i in t[CHAT]["items"]] == ["读代码", "改代码"]
    assert [i["status"] for i in t[CHAT]["items"]] == ["in_progress", "pending"]


def test_sid_matches_the_active_session(home):
    """靶心之二: sid 对不上, bridge 的 _session_tasks 下一次就当换会话把清单清掉。"""
    _run(home, "set", "一件事")
    assert _track(home)[CHAT]["sid"] == SID, _track(home)


def test_items_have_ids(home):
    """id 空着的话 TaskUpdate 那条老退路对不上号 —— 给稳定序号。"""
    _run(home, "set", "a", "b", "c")
    assert [i["id"] for i in _track(home)[CHAT]["items"]] == ["1", "2", "3"]


def test_start_and_done_flip_one_entry(home):
    _run(home, "set", "a", "b")
    assert _run(home, "done", "1").returncode == 0
    assert _run(home, "start", "2").returncode == 0
    assert [i["status"] for i in _track(home)[CHAT]["items"]] == ["completed", "in_progress"]


def test_clear_empties_but_keeps_the_file(home):
    _run(home, "set", "a")
    assert _run(home, "clear").returncode == 0
    assert _track(home)[CHAT]["items"] == []


def test_broken_file_is_not_treated_as_empty(home):
    """坏文件当空 = 把一次手滑放大成「任务卡全没了」, 与 bridge 的口径一致。"""
    (home / "task_track.json").write_text("{ 半截", encoding="utf-8")
    r = _run(home, "set", "新的")
    assert r.returncode == 1, r.stdout
    assert (home / "task_track.json").read_text(encoding="utf-8") == "{ 半截"


def test_bad_status_is_rejected(home):
    """状态写错不能静默存进去 —— 客户端渲染时只会当成 pending, 看不出是写错了。"""
    r = _run(home, "set", "a|干完了")
    assert r.returncode == 1 and "不认识" in r.stderr, r


def test_index_out_of_range_is_rejected(home):
    _run(home, "set", "a")
    r = _run(home, "done", "5")
    assert r.returncode == 1 and "没有第 5 条" in r.stderr, r


def test_multiple_chats_require_explicit_choice(home):
    """两个会话时别猜 —— 猜错就是把清单写到另一个会话的卡上。"""
    (home / "task_track.json").write_text(json.dumps(
        {CHAT: {"sid": SID, "items": []}, "oc_other": {"sid": "s2", "items": []}, "_v": 2}),
        encoding="utf-8")
    r = _run(home, "set", "a")
    assert r.returncode == 1 and "--chat" in r.stderr, r
    assert _run(home, "--chat", CHAT, "set", "a").returncode == 0


def test_show_does_not_write(home):
    _run(home, "set", "a")
    before = (home / "task_track.json").read_text(encoding="utf-8")
    assert _run(home, "show").returncode == 0
    assert (home / "task_track.json").read_text(encoding="utf-8") == before













def _run_env(home, *args, enc="gbk"):
    import os
    env = {**os.environ, "PYTHONIOENCODING": enc}
    return subprocess.run([sys.executable, str(_SCRIPT), "--home", str(home), *args],
                          capture_output=True, env=env)


def test_stdout_is_utf8_even_when_the_console_says_otherwise(home):
    """靶心: 调用方把编码压成 gbk, 脚本照样输出 utf-8。"""
    _run_env(home, "set", "读代码|in_progress", "改代码")
    r = _run_env(home, "show")
    assert r.returncode == 0, r.stderr
    assert "读代码" in r.stdout.decode("utf-8"), r.stdout


def test_stderr_is_utf8_too(home):
    """Behavioral regression coverage."""
    _run_env(home, "set", "a")
    r = _run_env(home, "done", "99")
    assert r.returncode == 1
    assert "没有第 99 条" in r.stderr.decode("utf-8"), r.stderr


def test_marks_that_gbk_cannot_encode_do_not_crash(home):
    """▶ ✓ · 这些标记 gbk 编不出来 —— 不接管的话这里是 UnicodeEncodeError 而不是乱码。"""
    _run_env(home, "set", "一|in_progress", "二|completed", "三")
    r = _run_env(home, "show")
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8")
    assert "▶" in out and "✓" in out, out


def test_missing_home_fails_loudly(tmp_path):
    r = subprocess.run([sys.executable, str(_SCRIPT), "--home",
                        str(tmp_path / "nope"), "show"], capture_output=True, text=True)
    assert r.returncode == 1 and "不是目录" in r.stderr, r


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

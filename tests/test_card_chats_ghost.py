"""桥应向历史会话推送任务卡状态，以便网关清除不再活跃的卡片。"""
import json
import sys

import pytest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
# ⚠ 必须在 import 之前把 argv[1] 换成一个真目录 —— fleet_bridge 拿它当 home。
# 不换的话 pytest 的 argv[1] 是**测试文件路径**, 建目录时 NotADirectoryError,
# 避免将 pytest 参数误当成 home 目录。
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv



# ── 网关里的"幽灵卡": 桥不再推的会话, 那份永远不会被更新也不会被清 ──────────
# 保留历史会话可确保网关收到清理更新。

def test_card_chats_includes_sessions_this_bot_has_ever_had(tmp_path, monkeypatch):
    """靶心: 会话在 `sessions.json` 里就要推 —— 它跨重启保留, 是"曾经在哪"的权威来源。"""
    (tmp_path / "sessions.json").write_text(json.dumps(
        {"app": {"active": "s1"}, "group:career": {"active": "s2"}}), encoding="utf-8")
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(b, "CLIENTS", {})
    monkeypatch.setattr(b, "_TASK_TRACK", {"app": {"items": []}})   # 本地已经没有群那份
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    assert "group:career" in b._card_chats(), b._card_chats()


def test_unreadable_sessions_file_does_not_break_the_push(tmp_path, monkeypatch):
    """Behavioral regression coverage."""
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(b, "CLIENTS", {})
    monkeypatch.setattr(b, "_TASK_TRACK", {"app": {"items": []}})
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    assert b._card_chats() == ["app"]


def test_private_only_bot_is_unaffected(tmp_path, monkeypatch):
    """没进过任何群的 bot, 推送目标一字不变。"""
    (tmp_path / "sessions.json").write_text(json.dumps({"app": {"active": "s1"}}),
                                            encoding="utf-8")
    monkeypatch.setattr(b._fs, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(b, "CLIENTS", {})
    monkeypatch.setattr(b, "_TASK_TRACK", {"app": {"items": []}})
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "app")
    assert b._card_chats() == ["app"]

"""Behavioral regression coverage."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="cut-turn-")]
try:
    from feishu_bridge.bridge import fleet_bridge as FB
finally:
    sys.argv = _argv


_PROD = {"_v": 1, "domains": {
    "fleet-product": {"label": "x", "bots": ["sample-mobile", "backend"], "lead": "backend"},
    "career": {"label": "y", "bots": ["sample-agent"]},        # 没有 lead ⇒ 不是群
}}


def _home(tmp_path: Path) -> Path:
    ops = tmp_path / ".fleet-ops"
    ops.mkdir(parents=True, exist_ok=True)
    (ops / "fleet_domains.json").write_text(json.dumps(_PROD), encoding="utf-8")
    (ops / "session_inject.py").write_text("# 占位, 存在即可", encoding="utf-8")
    return tmp_path


class _Run:
    """替身: 记下 session_inject 被怎么调的, 不真的起进程。"""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        class R:  # noqa: D401
            returncode, stdout, stderr = 0, "", ""
        return R()


def _fire(tmp_path, cid, bot="sample-mobile", entry=None):
    import subprocess
    run = _Run()
    old_home, old_name, old_run = FB.Path.home, FB._BOT_NAME, subprocess.run
    FB.Path.home = staticmethod(lambda: _home(tmp_path))
    FB._BOT_NAME = bot
    previous_hook = os.environ.get("FLEET_SESSION_INJECT_HOOK")
    os.environ["FLEET_SESSION_INJECT_HOOK"] = str(_home(tmp_path) / ".fleet-ops" / "session_inject.py")
    subprocess.run = run
    logs = []
    old_log = FB.log
    FB.log = logs.append
    try:
        FB._tell_lead_turn_was_cut(cid, entry or {"text": "📨 [来自 backend bot] #T003 …"})
    finally:
        FB.Path.home, FB._BOT_NAME, subprocess.run, FB.log = old_home, old_name, old_run, old_log
        if previous_hook is None:
            os.environ.pop("FLEET_SESSION_INJECT_HOOK", None)
        else:
            os.environ["FLEET_SESSION_INJECT_HOOK"] = previous_hook
    return run.calls, logs


def _tmp():
    return Path(tempfile.mkdtemp(prefix="cut-home-"))


def test_group_turn_tells_the_lead():
    """靶心: 群回合被掐 ⇒ lead 收到一条。"""
    calls, _ = _fire(_tmp(), "group:fleet-product")
    assert len(calls) == 1, calls
    argv = calls[0]
    assert "--bot" in argv and argv[argv.index("--bot") + 1] == "backend", argv
    assert "--from" in argv and argv[argv.index("--from") + 1] == "sample-mobile", argv


def test_message_says_it_did_not_rerun():
    """⚠ 被掐的回合可能已经写了文件/提交了代码 —— lead 必须知道**没有**自动重跑,
    否则它会假设活已经在跑。"""
    calls, _ = _fire(_tmp(), "group:fleet-product")
    text = calls[0][calls[0].index("--text") + 1]
    assert "没有" in text and "重跑" in text, text
    assert "fleet-product" in text


def test_private_turn_tells_nobody():
    """私聊被掐只发给用户那条提示就够 —— 没有 lead 这个角色。"""
    calls, _ = _fire(_tmp(), "oc_0388d7043")
    assert calls == [], calls


def test_domain_without_lead_tells_nobody():
    """没有 lead 的域不是群 (协议 groups/README.md)。"""
    calls, _ = _fire(_tmp(), "group:career")
    assert calls == [], calls


def test_lead_does_not_tell_itself():
    """lead 自己的回合被掐, 别递给自己 —— 那是一条永远没人处理的消息。"""
    calls, _ = _fire(_tmp(), "group:fleet-product", bot="backend")
    assert calls == [], calls


def test_unreadable_domains_file_is_silent_not_crash():
    """读不到 = 不猜 lead, 且**绝不能拦下启动** (这段跑在 bridge 启动路径上)。"""
    empty = Path(tempfile.mkdtemp(prefix="no-ops-"))
    import subprocess
    run = _Run()
    old_home, old_name, old_run = FB.Path.home, FB._BOT_NAME, subprocess.run
    FB.Path.home = staticmethod(lambda: empty)
    FB._BOT_NAME = "sample-mobile"
    subprocess.run = run
    old_log, logs = FB.log, []
    FB.log = logs.append
    try:
        FB._tell_lead_turn_was_cut("group:fleet-product", {"text": "x"})   # 不许抛
    finally:
        FB.Path.home, FB._BOT_NAME, subprocess.run, FB.log = old_home, old_name, old_run, old_log
    assert run.calls == []


def test_unconfigured_hook_is_skipped(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setattr(FB.Path, "home", staticmethod(lambda: _home(tmp_path)))
    monkeypatch.setattr(FB, "_BOT_NAME", "sample-mobile")
    monkeypatch.delenv("FLEET_SESSION_INJECT_HOOK", raising=False)
    run = _Run()
    monkeypatch.setattr(subprocess, "run", run)
    FB._tell_lead_turn_was_cut("group:fleet-product", {"text": "sample"})
    assert run.calls == []


def test_it_is_actually_wired_into_the_startup_path():
    """Behavioral regression coverage."""
    src = Path(FB.__file__).read_text(encoding="utf-8")
    i = src.index("async def _report_interrupted_turns")
    assert "_tell_lead_turn_was_cut(cid, entry)" in src[i:i + 2000], "写了但没接进启动路径"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

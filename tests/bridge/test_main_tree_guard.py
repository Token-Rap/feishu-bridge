"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-guard-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b
finally:
    sys.argv = _argv


def _call(tool, tin):
    m = b._main_tree_guard_matcher()
    return asyncio.run(m.hooks[0]({"tool_name": tool, "tool_input": tin}, "t", None))


def test_guard_is_registered_on_every_session():
    names = [m.matcher for m in b._pretool_matchers("c")]
    assert "AskUserQuestion" in names and "Edit|Write|MultiEdit|NotebookEdit" in names


def test_edit_in_a_main_tree_is_denied(monkeypatch, tmp_path):
    monkeypatch.setattr(b._fleet_wt, "REPOS", {"bridge": tmp_path})
    out = _call("Edit", {"file_path": str(tmp_path / "src/feishu_bridge/bridge/fleet_bridge.py")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "start bridge" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_relative_path_is_resolved_against_the_bridge_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(b._fleet_wt, "REPOS", {"bridge": tmp_path})
    monkeypatch.setattr(b, "REPO", str(tmp_path))
    assert _call("Write", {"file_path": "x.py"}).get("hookSpecificOutput")


def test_notes_and_outside_paths_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(b._fleet_wt, "REPOS", {"bridge": tmp_path})
    assert _call("Edit", {"file_path": str(tmp_path / "NOTES.md")}) == {}
    assert _call("Edit", {"file_path": "/tmp/somewhere/else.py"}) == {}


def test_a_broken_guard_fails_open(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("guard bug")
    monkeypatch.setattr(b._fleet_wt, "main_tree_denial", boom)
    monkeypatch.setattr(b, "log", lambda *_a, **_k: None)
    assert _call("Edit", {"file_path": "/x"}) == {}

"""一任务一 worktree 工具 (契约 contracts/task_worktree.md) —— 全部走真 git。

每条都在临时目录里建真仓、真 worktree、真快进: 这个工具的全部价值在于它**不做**的那些事
(不 stash 别人的改动、窗口里不合、不删新鲜的 worktree), 桩掉 git 就什么都验不到。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.sessions import fleet_worktree as W  # noqa: E402


def _git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **(env or {})}).stdout.strip()


def _commit(cwd, name, body, msg=None, env=None):
    (Path(cwd) / name).write_text(body)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-qm", msg or f"edit {name}", env=env)


@pytest.fixture
def env(tmp_path, monkeypatch):
    main = tmp_path / "repo"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    _commit(main, ".gitignore", "venv/\n")               # same line as both real repos
    _commit(main, "a.txt", "one\n")
    (main / "venv").mkdir()                         # untracked runtime dir, like the real repos
    (main / "venv" / "sentinel").write_text("keep")
    root = tmp_path / "worktrees"
    monkeypatch.setattr(W, "ROOT", root)
    monkeypatch.setattr(W, "REPOS", {"demo": main})
    return main, root


def _start(task="T1", bot="sample-web", **kw):
    return W.cmd_start("demo", task, bot, **kw)


# ── start ─────────────────────────────────────────────────────────────
def test_start_creates_worktree_branch_and_a_clean_venv_link(env):
    main, root = env
    wt = _start()
    assert wt == (root / "demo" / "sample-web" / "T1" / "a1").resolve()
    assert _git(wt, "branch", "--show-current") == "wt/sample-web/T1/a1"
    assert (wt / "venv").is_symlink()
    assert _git(wt, "status", "--porcelain") == "", "venv 软链没被排除 —— git add -A 会把它提交"
    assert _start() == wt, "start 要幂等"


def test_start_refuses_bad_names(env):
    for task in ("../x", "a/b", "", "x" * 49):
        with pytest.raises(W.Refused):
            _start(task=task)
    with pytest.raises(W.Refused):
        _start(bot="Web App")


def test_start_refuses_when_disk_is_low(env, monkeypatch):
    monkeypatch.setattr(W, "MIN_FREE_BYTES", 1 << 62)
    with pytest.raises(W.Refused, match="磁盘"):
        _start()


def test_venv_with_editable_install_pointing_at_main_is_not_linked(env, capsys):
    """review #6: 可编辑安装会让 worktree 里的测试导入主树代码而假通过。"""
    main, _ = env
    sp = main / "venv" / "lib" / "python3.14" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "__editable__.demo.pth").write_text(str(main.resolve()))
    wt = _start()
    assert not (wt / "venv").exists()
    assert "假通过" in capsys.readouterr().err


# ── finish ────────────────────────────────────────────────────────────
def test_finish_fast_forwards_main_and_cleans_up(env):
    main, root = env
    wt = _start()
    _commit(wt, "b.txt", "web\n")
    msg = W.cmd_finish(str(wt), "sample-web")
    assert "1 个提交" in msg
    assert (main / "b.txt").read_text() == "web\n", "主树没收到快进"
    assert not wt.exists() and not (root / "demo" / "sample-web").exists(), "空父目录要一起清"
    assert "wt/sample-web/T1/a1" not in _git(main, "branch", "--list")
    assert (main / "venv" / "sentinel").read_text() == "keep", "删 worktree 顺着软链删到了主树 venv"


def test_finish_refuses_uncommitted_work(env):
    wt = _start()
    (wt / "b.txt").write_text("wip")
    with pytest.raises(W.Refused, match="没提交"):
        W.cmd_finish(str(wt), "sample-web")


def test_finish_rebases_when_main_moved(env):
    main, _ = env
    wt = _start()
    _commit(wt, "b.txt", "web\n")
    other = _start(task="T2", bot="sample-mobile")
    _commit(other, "c.txt", "ios\n")
    W.cmd_finish(str(other), "sample-mobile")
    W.cmd_finish(str(wt), "sample-web")
    assert (main / "b.txt").exists() and (main / "c.txt").exists()
    assert _git(main, "log", "--merges", "--oneline") == "", "只许快进, 不许产生合并提交"


def test_rebase_conflict_stops_and_leaves_main_alone(env):
    main, _ = env
    wt = _start()
    _commit(wt, "a.txt", "web version\n")
    other = _start(task="T2", bot="sample-mobile")
    _commit(other, "a.txt", "ios version\n")
    W.cmd_finish(str(other), "sample-mobile")
    before = _git(main, "rev-parse", "main")
    with pytest.raises(W.Refused) as ei:
        W.cmd_finish(str(wt), "sample-web")
    assert ei.value.code == 2 and "a.txt" in str(ei.value)
    assert _git(main, "rev-parse", "main") == before
    assert wt.exists(), "冲突时 worktree 必须留着让 owner 解决"


def test_someone_elses_uncommitted_edit_in_main_is_never_touched(env):
    """Behavioral regression coverage."""
    main, _ = env
    wt = _start()
    _commit(wt, "a.txt", "web\n")
    (main / "a.txt").write_text("ios half-done\n")          # someone still editing in main
    before = _git(main, "rev-parse", "main")
    with pytest.raises(W.Refused) as ei:
        W.cmd_finish(str(wt), "sample-web")
    assert ei.value.code == 3
    assert (main / "a.txt").read_text() == "ios half-done\n", "别人的未提交改动被动了"
    assert _git(main, "stash", "list") == "", "不许 stash 别人的改动"
    assert _git(main, "rev-parse", "main") == before


def test_finish_refuses_inside_a_rollout_window(env):
    main, _ = env
    wt = _start()
    _commit(wt, "b.txt", "x\n")
    target = main / "a.txt"
    os.chflags(target, os.stat(target).st_flags | stat.UF_IMMUTABLE)
    try:
        with pytest.raises(W.Refused) as ei:
            W.cmd_finish(str(wt), "sample-web")
        assert ei.value.code == 4
    finally:
        os.chflags(target, os.stat(target).st_flags & ~stat.UF_IMMUTABLE)
    assert not (main / "b.txt").exists()


def test_finish_waits_then_refuses_while_the_lock_is_held(env, monkeypatch):
    """review #4: rollout 整个窗口都持有同一把锁 —— finish 抢不到就退, 不硬合。"""
    import fcntl
    main, _ = env
    wt = _start()
    _commit(wt, "b.txt", "x\n")
    monkeypatch.setattr(W, "LOCK_WAIT_S", 0.6)
    with open(W.lock_path(main), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(W.Refused) as ei:
            W.cmd_finish(str(wt), "sample-web")
    assert ei.value.code == 4 and not (main / "b.txt").exists()


def test_only_the_owner_can_finish_or_abort(env):
    wt = _start()
    _commit(wt, "b.txt", "x\n")
    with pytest.raises(W.Refused, match="sample-web 的 worktree"):
        W.cmd_finish(str(wt), "sample-mobile")
    with pytest.raises(W.Refused, match="sample-web 的 worktree"):
        W.cmd_abort(str(wt), "sample-mobile", discard=True)


# ── abort ─────────────────────────────────────────────────────────────
def test_abort_refuses_to_drop_unmerged_work_without_discard(env):
    main, _ = env
    wt = _start()
    _commit(wt, "b.txt", "x\n")
    with pytest.raises(W.Refused, match="--discard"):
        W.cmd_abort(str(wt), "sample-web")
    W.cmd_abort(str(wt), "sample-web", discard=True)
    assert not wt.exists() and not (main / "b.txt").exists()


# ── reap ──────────────────────────────────────────────────────────────
def test_reap_never_removes_a_fresh_worktree(env):
    """review #2: 刚开、还没提交的分支也是 main 的祖先 —— 看起来和「已合并」一模一样。"""
    wt = _start()
    out = W.cmd_reap()
    assert wt.exists(), out


def test_reap_removes_a_merged_but_forgotten_worktree(env):
    main, _ = env
    wt = _start()
    _commit(wt, "b.txt", "x\n")
    _git(main, "merge", "--ff-only", "-q", "wt/sample-web/T1/a1")      # merged by hand, never finished
    assert "REMOVED" in W.cmd_reap()
    assert not wt.exists()


def test_reap_reports_stale_unmerged_work_without_deleting_it(env):
    wt = _start()
    old = {"GIT_COMMITTER_DATE": "2026-01-01T00:00:00", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00"}
    _commit(wt, "b.txt", "x\n", env=old)
    os.utime(wt, (0, 0))
    out = W.cmd_reap()
    assert "NOTIFY sample-web" in out and wt.exists()


def test_reap_flags_main_tree_dirt_but_not_notes(env):
    main, _ = env
    (main / "a.txt").write_text("bash wrote this\n")
    (main / "NOTES.md").write_text("notes are fine\n")
    out = W.cmd_reap()
    assert "MAIN-DIRTY" in out and "a.txt" in out and "NOTES.md" not in out


# ── bridge guard ──────────────────────────────────────────────────────
def test_guard_denies_edits_in_a_main_tree_including_relative_paths(env):
    main, root = env
    assert W.main_tree_denial("Edit", {"file_path": str(main / "a.txt")}, "/") is not None
    assert W.main_tree_denial("Write", {"file_path": "sub/new.py"}, main) is not None
    assert W.main_tree_denial("NotebookEdit", {"notebook_path": str(main / "n.ipynb")}, "/")


def test_guard_allows_worktrees_notes_and_other_tools(env):
    main, root = env
    wt = _start()
    assert W.main_tree_denial("Edit", {"file_path": str(wt / "a.txt")}, main) is None
    for name in ("NOTES.md", "CLAUDE.md", "web/UNKNOWNS.md"):
        assert W.main_tree_denial("Edit", {"file_path": str(main / name)}, "/") is None, name
    assert W.main_tree_denial("Read", {"file_path": str(main / "a.txt")}, "/") is None
    assert W.main_tree_denial("Edit", {"file_path": "/tmp/elsewhere.txt"}, "/") is None

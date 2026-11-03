#!/usr/bin/env python3
"""一任务一 worktree (契约 ~/.fleet-ops/contracts/task_worktree.md)。

不变量只有一条: **bot 不在主树里改文件**, 主树只接收 `finish` 快进进来的提交。

    fleet_worktree.py start  <repo> <task> --bot <bot> [--attempt N]
    fleet_worktree.py finish <worktree-path> --bot <bot>
    fleet_worktree.py abort  <worktree-path> --bot <bot> [--discard]
    fleet_worktree.py list   [--repo <repo>]
    fleet_worktree.py reap   [--dry-run]

Exit codes: 0 ok · 1 refused/usage · 2 rebase conflict · 3 main tree blocked · 4 rollout window open.

⚠ This tool never stashes, resets or checks out anybody's uncommitted changes in the main tree.
When they block a merge it reports them and stops — silently moving someone's half-done work
could otherwise destroy or hide concurrent work.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.getenv("FLEET_WT_ROOT") or Path.home() / "dev" / "worktrees")
_DEFAULT_REPOS = {"fleet-app": Path.home() / "dev" / "fleet-app",
                  "bridge": Path.home() / "dev" / "feishu-claude-bridge"}
REPOS = ({k: Path(v) for k, v in json.loads(os.environ["FLEET_WT_REPOS"]).items()}
         if os.getenv("FLEET_WT_REPOS") else _DEFAULT_REPOS)
MAIN = "main"
LINKED_ENV_DIRS = ("venv",)          # untracked runtime dirs the tests need
EXCLUDED = ("/venv", "/.dd")         # .dd = per-worktree Xcode DerivedData (dies with the worktree)
MIN_FREE_BYTES = 5 * 1024 ** 3
LOCK_WAIT_S = 30
# Main-tree files a bot may still edit in place (memory/notes the harness asks for every turn).
# Everything else in a main tree goes through a worktree; see main_tree_denial().
MAIN_TREE_EDITABLE = frozenset({"NOTES.md", "CLAUDE.md", "UNKNOWNS.md"})
GUARDED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
STALE_S = 7 * 86400
MAX_ACTIVE_PER_BOT = 5
FINISH_RETRIES = 3

_TASK_RE = re.compile(r"^[A-Za-z0-9._-]{1,48}$")
_BOT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Refused(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Refused(f"git {' '.join(args)} 失败:\n{(r.stderr or r.stdout).strip()}")
    return r


def _out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _branch(bot: str, task: str, attempt: int) -> str:
    return f"wt/{bot}/{task}/a{attempt}"


def _common_dir(main: Path) -> Path:
    return (main / _out(main, "rev-parse", "--git-common-dir")).resolve()


def _registered(main: Path) -> dict[Path, str]:
    """worktree path → branch (refs/heads stripped), from git's own registry."""
    out, cur = {}, None
    for line in _out(main, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = Path(line[9:]).resolve()
            out[cur] = ""
        elif line.startswith("branch ") and cur is not None:
            out[cur] = line[7:].removeprefix("refs/heads/")
    return out


def _parse(path: str, as_bot: str | None = None) -> tuple[str, Path, Path, str, str]:
    """worktree path → (repo, main dir, wt dir, bot, branch). Refuses anything not ours."""
    wt = Path(path).expanduser().resolve()
    try:
        repo, bot, task, att = wt.relative_to(ROOT.resolve()).parts
    except ValueError:
        raise Refused(f"{wt} 不在 {ROOT}/<repo>/<bot>/<task>/a<N> 下 —— 不是 wt 建的 worktree")
    if repo not in REPOS or not att.startswith("a") or not att[1:].isdigit():
        raise Refused(f"{wt}: 路径形状不对 (repo={repo}, attempt={att})")
    if as_bot is not None and as_bot != bot:
        raise Refused(f"{wt} 是 {bot} 的 worktree, 你是 {as_bot} —— 别人的活不替他合/丢")
    main = REPOS[repo]
    branch = _registered(main).get(wt)
    if branch is None:
        raise Refused(f"{wt} 没在 {repo} 的 worktree 登记里 (目录还在但 git 不认它? 试 `reap`)")
    if branch != _branch(bot, task, int(att[1:])):
        raise Refused(f"{wt} 检出的是 {branch!r}, 不是 {_branch(bot, task, int(att[1:]))!r} —— 不碰")
    return repo, main, wt, bot, branch


def _dirty(cwd: Path) -> list[str]:
    # Not _out(): stripping the whole output eats the first line's leading status column,
    # and every path parsed from it shifts by one character.
    return [ln for ln in _git(cwd, "status", "--porcelain").stdout.splitlines() if ln.strip()]


def _ahead(main: Path, branch: str) -> int:
    return int(_out(main, "rev-list", "--count", f"{MAIN}..{branch}"))


def _immutable_files(main: Path) -> list[str]:
    """rollout.py locks the bridge tree with `chflags uchg`; any UF_IMMUTABLE file ⇒ window open."""
    hits = []
    for rel in _out(main, "ls-files").splitlines():
        with contextlib.suppress(OSError):
            if os.lstat(main / rel).st_flags & stat.UF_IMMUTABLE:
                hits.append(rel)
    return hits


def lock_path(main: Path) -> Path:
    """The one lock both `finish` and rollout.py hold (rollout: for its whole window)."""
    return _common_dir(main) / "fleet-wt.lock"


@contextlib.contextmanager
def _repo_lock(main: Path, wait_s: float | None = None):
    import fcntl                     # lazy: the bridge imports this module on Windows too
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s   # read at call time, not def time
    with open(lock_path(main), "a") as fh:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused(f"{lock_path(main)} 被占着 {wait_s:.0f}s —— rollout 发布窗口开着, "
                                  "或别人正在合并; 稍后再 finish", 4)
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _link_env(main: Path, wt: Path) -> None:
    exclude = _common_dir(main) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    # `.gitignore`'s `venv/` does NOT match a symlink (verified) —
    # without this line `git add -A` would commit the link.
    for pat in EXCLUDED:
        if pat not in have:
            have.append(pat)
    exclude.write_text("\n".join(have) + "\n", encoding="utf-8")
    for name in LINKED_ENV_DIRS:
        src = main / name
        if not src.exists() or (wt / name).exists():
            continue
        leaks = _points_at_main(src, main)
        if leaks:
            # An editable install would make worktree tests import main-tree code and pass falsely.
            print(f"⚠ 没软链 {name}: 它里面有指向主树的路径 ({leaks[0]}) —— 在 worktree 里的测试会"
                  f"导入主树代码而假通过。请在 {wt} 里自建 {name}。", file=sys.stderr)
            continue
        (wt / name).symlink_to(src)


def _points_at_main(venv: Path, main: Path) -> list[str]:
    """.pth / egg-link / editable finder files inside `venv` that mention the main tree."""
    needle, hits = str(main.resolve()), []
    for pat in ("*.pth", "*.egg-link", "__editable__*"):
        for f in venv.glob(f"lib/python*/site-packages/{pat}"):
            with contextlib.suppress(OSError):
                if f.is_file() and needle in f.read_text(encoding="utf-8", errors="replace"):
                    hits.append(str(f))
    return hits


def _remove(main: Path, wt: Path, branch: str, force: bool) -> None:
    _git(main, "worktree", "remove", *(["--force"] if force else []), str(wt))
    _git(main, "branch", "-D" if force else "-d", branch)
    for parent in (wt.parent, wt.parent.parent):          # task dir, then bot dir
        with contextlib.suppress(OSError):
            parent.rmdir()


def main_tree_dirt(main: Path) -> list[str]:
    """Uncommitted paths in the main tree, minus the in-place editable notes."""
    out = []
    for ln in _dirty(main):
        rel = ln[3:].split(" -> ")[-1].strip('"')
        if Path(rel).name not in MAIN_TREE_EDITABLE:
            out.append(rel)
    return out


def main_tree_denial(tool: str, tool_input: dict, cwd: str | Path) -> str | None:
    """Bridge PreToolUse guard: why this edit must not happen in a main tree, or None.

    Only Edit/Write-family tools — Bash writes can't be seen here; `finish`/`reap` report the
    main-tree dirt they leave behind. Callers must fail open on exceptions (fail-soft rule).
    """
    if tool not in GUARDED_TOOLS:
        return None
    raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    p = (p if p.is_absolute() else Path(cwd) / p).resolve()
    for name, main in REPOS.items():
        main = main.resolve()
        if main not in p.parents:
            continue
        rel = p.relative_to(main)
        if p.name in MAIN_TREE_EDITABLE:
            return None
        return (f"⛔ {rel} 在 {name} 的**主树**里 —— 主树就是线上代码, 不直接改 (契约 contracts/task_worktree.md)。\n"
                f"先: cd ~ && {sys.executable} {Path(__file__).resolve()} start {name} <task> --bot <你的名字>\n"
                f"再改它打印的 <worktree>/{rel}; 跑命令一律 `cd <worktree> && …`。\n"
                f"例外只有 {' / '.join(sorted(MAIN_TREE_EDITABLE))} (改完当轮提交)。")
    return None


# ── commands ──────────────────────────────────────────────────────────
def cmd_start(repo: str, task: str, bot: str, attempt: int = 1) -> Path:
    if repo not in REPOS:
        raise Refused(f"未知 repo {repo!r}; 可选: {', '.join(REPOS)}")
    if not _TASK_RE.match(task) or task in (".", ".."):
        raise Refused(f"task 名只许 ASCII [A-Za-z0-9._-], ≤48 字: {task!r}")
    if not _BOT_RE.match(bot):
        raise Refused(f"bot 名不合法: {bot!r}")
    if attempt < 1:
        raise Refused("--attempt 从 1 开始")
    main, branch = REPOS[repo], _branch(bot, task, attempt)
    wt = (ROOT / repo / bot / task / f"a{attempt}").resolve()
    if _registered(main).get(wt) == branch:
        return wt                                        # idempotent
    if wt.exists():
        raise Refused(f"{wt} 已存在但不是 {branch} 的 worktree —— 不覆盖, 先看一眼")
    free = shutil.disk_usage(ROOT if ROOT.exists() else ROOT.parent).free
    if free < MIN_FREE_BYTES:
        raise Refused(f"磁盘只剩 {free / 1024 ** 3:.1f} GB (< {MIN_FREE_BYTES / 1024 ** 3:.0f} GB) —— 先清理再开 worktree")
    wt.parent.mkdir(parents=True, exist_ok=True)
    has_branch = _git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                      check=False).returncode == 0
    if has_branch:
        _git(main, "worktree", "add", str(wt), branch)
    else:
        _git(main, "worktree", "add", "-b", branch, str(wt), MAIN)
        # creation base: reap must tell "no commits yet" from "merged" — both are ancestors of main.
        _git(main, "config", f"branch.{branch}.fleetbase", _out(main, "rev-parse", MAIN))
    _link_env(main, wt)
    return wt


def cmd_finish(path: str, as_bot: str) -> str:
    repo, main, wt, bot, branch = _parse(path, as_bot)
    dirty = _dirty(wt)
    if dirty:
        raise Refused("worktree 里还有没提交的改动 (wt 不替你 commit):\n  " + "\n  ".join(dirty[:20]))
    if _ahead(main, branch) == 0:
        _remove(main, wt, branch, force=False)
        return f"ℹ {branch} 没有新提交, 已直接清理"
    for _ in range(FINISH_RETRIES):
        r = _git(wt, "rebase", MAIN, check=False)
        if r.returncode != 0:
            conflicted = _out(wt, "diff", "--name-only", "--diff-filter=U").splitlines()
            raise Refused(
                "rebase 冲突, worktree 停在 rebase 中:\n  " + "\n  ".join(conflicted or ["(见 git status)"])
                + f"\n自己域的文件: 在 {wt} 解决后 `git rebase --continue`, 再 finish;"
                  "\n别人域的文件: `git rebase --abort` 并找那个域的 owner —— 别改别人的文件去解决。", 2)
        with _repo_lock(main):
            locked = _immutable_files(main)
            if locked:
                raise Refused(f"rollout 发布窗口开着 ({len(locked)} 个文件带 uchg, 如 {locked[0]}) "
                              "—— 等「发布窗口结束」通知后再 finish", 4)
            head = _out(main, "symbolic-ref", "--quiet", "--short", "HEAD")
            if head != MAIN:
                raise Refused(f"主树检出的是 {head!r} 不是 {MAIN} —— 不动它", 3)
            if _git(main, "merge-base", "--is-ancestor", MAIN, branch, check=False).returncode != 0:
                continue                                  # main moved since the rebase
            old = _out(main, "rev-parse", MAIN)
            m = _git(main, "merge", "--ff-only", "--quiet", branch, check=False)
            if m.returncode != 0:
                raise Refused("主树快进被挡 —— 多半是有人在主树里留了未提交改动 (wt 不会动它们):\n"
                              + (m.stderr or m.stdout).strip()
                              + "\n由那些文件的 owner 把改动挪进自己的 worktree 后再 finish。", 3)
            new = _out(main, "rev-parse", MAIN)
        n = int(_out(main, "rev-list", "--count", f"{old}..{new}"))
        _remove(main, wt, branch, force=False)
        dirt = main_tree_dirt(main)
        tail = (f"\n⚠ 主树里还有 {len(dirt)} 处未提交改动 (不是 wt 留的, 没动它们): "
                + ", ".join(dirt[:5]) if dirt else "")
        return (f"✅ {branch} 已快进进 {repo}/{MAIN}: {old[:7]}..{new[:7]} ({n} 个提交), worktree 已清理\n"
                "   上线动作照各域规矩: server/ → kickstart 网关; web/ → 已生效; bridge → 递「铺 "
                f"{new[:7]}」; 测过了吗?" + tail)
    raise Refused(f"{MAIN} 连续 {FINISH_RETRIES} 次在 rebase 后又前进了 —— 稍后再 finish", 3)


def cmd_abort(path: str, as_bot: str, discard: bool = False) -> str:
    repo, main, wt, bot, branch = _parse(path, as_bot)
    ahead, dirty = _ahead(main, branch), _dirty(wt)
    if (ahead or dirty) and not discard:
        raise Refused(f"{branch} 有 {ahead} 个未合并提交、{len(dirty)} 处未提交改动 —— "
                      "确定要丢就加 --discard")
    _remove(main, wt, branch, force=True)
    return f"🗑 {branch} 已丢弃 (未合并提交 {ahead} 个, 未提交改动 {len(dirty)} 处)"


def _ours(repo: str) -> list[tuple[Path, str, str, str]]:
    """(wt, bot, task, branch) for every worktree of `repo` that lives under ROOT."""
    base = (ROOT / repo).resolve()
    out = []
    for wt, branch in _registered(REPOS[repo]).items():
        with contextlib.suppress(ValueError):
            parts = wt.relative_to(base).parts
            if len(parts) == 3:
                out.append((wt, parts[0], parts[1], branch))
    return out


def _last_commit_ts(main: Path, branch: str) -> float:
    return float(_out(main, "log", "-1", "--format=%ct", branch) or 0)


def cmd_list(repo: str | None = None) -> str:
    rows = []
    for r in ([repo] if repo else REPOS):
        main = REPOS[r]
        for wt, bot, task, branch in _ours(r):
            behind, ahead = _out(main, "rev-list", "--left-right", "--count",
                                 f"{MAIN}...{branch}").split()
            age_h = (time.time() - _last_commit_ts(main, branch)) / 3600
            dirty = len(_dirty(wt)) if wt.exists() else -1
            rows.append(f"{r:10} {bot:14} {task}/{wt.name:<28} +{ahead}/-{behind}  "
                        f"{age_h:6.1f}h  {'dirty=' + str(dirty) if dirty else 'clean'}")
    return "\n".join(rows) or "(没有活跃的 worktree)"


def cmd_reap(dry_run: bool = False) -> str:
    lines = []
    for r, main in REPOS.items():
        _git(main, "worktree", "prune")
        per_bot: dict[str, int] = {}
        for wt, bot, task, branch in _ours(r):
            merged = _git(main, "merge-base", "--is-ancestor", branch, MAIN,
                          check=False).returncode == 0
            clean = wt.exists() and not _dirty(wt)
            base = _git(main, "config", "--get", f"branch.{branch}.fleetbase", check=False).stdout.strip()
            fresh = not base or _out(main, "rev-parse", branch) == base   # no commits yet ≠ merged
            if merged and clean and not fresh:
                lines.append(f"REMOVED {r} {branch} (已并入 {MAIN}, 忘了 finish)")
                if not dry_run:
                    _remove(main, wt, branch, force=False)
                continue
            per_bot[bot] = per_bot.get(bot, 0) + 1
            idle = time.time() - max(_last_commit_ts(main, branch),
                                     wt.stat().st_mtime if wt.exists() else 0)
            if idle > STALE_S:
                lines.append(f"NOTIFY {bot}: {r} {branch} 未合并且 {idle / 86400:.0f} 天没动 "
                             f"({wt}) —— finish 或 abort --discard")
        dirt = main_tree_dirt(main)
        if dirt:
            lines.append(f"MAIN-DIRTY {r}: 主树有 {len(dirt)} 处未提交改动 (不变量被破了, 多半是 Bash 写的): "
                         + ", ".join(dirt[:8]))
        for bot, n in per_bot.items():
            if n > MAX_ACTIVE_PER_BOT:
                lines.append(f"NOTIFY {bot}: {r} 里有 {n} 个活跃 worktree (> {MAX_ACTIVE_PER_BOT}), "
                             "多半有忘了 abort 的")
    return "\n".join(lines) or "(没有要清的)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet_worktree.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("repo")
    s.add_argument("task")
    s.add_argument("--bot", required=True)
    s.add_argument("--attempt", type=int, default=1)
    f = sub.add_parser("finish")
    f.add_argument("path")
    f.add_argument("--bot", required=True)
    a = sub.add_parser("abort")
    a.add_argument("path")
    a.add_argument("--bot", required=True)
    a.add_argument("--discard", action="store_true")
    li = sub.add_parser("list")
    li.add_argument("--repo")
    rp = sub.add_parser("reap")
    rp.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "start":
            print(cmd_start(ns.repo, ns.task, ns.bot, ns.attempt))
        elif ns.cmd == "finish":
            print(cmd_finish(ns.path, ns.bot))
        elif ns.cmd == "abort":
            print(cmd_abort(ns.path, ns.bot, ns.discard))
        elif ns.cmd == "list":
            print(cmd_list(ns.repo))
        else:
            print(cmd_reap(ns.dry_run))
    except Refused as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())

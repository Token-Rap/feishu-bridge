"""Behavioral regression coverage."""
from __future__ import annotations

import ast
from pathlib import Path

_SRC_FILE = Path(__file__).resolve().parent.parent / "fleet_bridge.py"
_TREE = ast.parse(_SRC_FILE.read_text(encoding="utf-8"))


def _log_def_line() -> int:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == "log":
            return node.lineno
    raise AssertionError("找不到 def log —— 这条测试的前提没了, 先看它是不是改名了")


def _toplevel_funcs() -> dict:
    return {n.name: n for n in _TREE.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_before(line: int) -> set:
    """在 `line` 之前、**模块顶层真正执行**的那些语句里, 调用了哪些本模块函数。"""
    names = set()
    funcs = _toplevel_funcs()
    for node in _TREE.body:
        if node.lineno >= line:
            break
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                      # 定义不等于执行
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id in funcs:
                    names.add(call.func.id)
    return names


def test_importtime_functions_do_not_use_log():
    """靶心: 导入期跑到的函数, body 里不许有 log —— 那时它还不存在。"""
    log_line = _log_def_line()
    funcs = _toplevel_funcs()
    offenders = []
    for name in _calls_before(log_line):
        body = funcs[name]
        if any(isinstance(n, ast.Name) and n.id == "log" for n in ast.walk(body)):
            offenders.append(f"{name}() (第 {body.lineno} 行) 在 def log (第 {log_line} 行) 之前被顶层调用, 却用了 log")
    assert not offenders, "导入期会 NameError:\n  " + "\n  ".join(offenders)


def test_log_is_defined_before_any_toplevel_call():
    """更强的一条: 顶层调用本模块函数之前, log 就该已经存在。
    这样以后往任何导入期函数里加一行日志都是安全的, 不用逐个检查。"""
    log_line = _log_def_line()
    called = _calls_before(log_line)
    assert not called, (
        f"这些函数在 def log (第 {log_line} 行) 之前就被顶层调用了: {sorted(called)} —— "
        "把 log 的定义挪到它们之前, 别让「导入期能不能记日志」变成每次都要想的事")


def test_stderr_utf8_setup_comes_before_log():
    """log 会打中文/emoji —— Windows 上 stdout 默认 GBK, 不先 reconfigure 就是
    UnicodeEncodeError 崩进程 (那段注释自己写着)。所以顺序是: 编码设置 → log → 其它。"""
    src = _SRC_FILE.read_text(encoding="utf-8")
    assert src.index("sys.stdout.reconfigure") < src.index("def log(msg: str)")


def test_unreadable_claude_md_degrades_instead_of_crashing(tmp_path):
    """Behavioral regression coverage."""
    import subprocess, sys as _sys
    repo = tmp_path / "repo"
    (repo / "CLAUDE.md").mkdir(parents=True)          # 目录: exists() 真, read_text 炸
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run(
        [_sys.executable, "-c",
         # ⚠ 先把两个参数取出来**再**改 sys.argv —— 反过来写会把第二个参数弄丢
         # (我第一版就是这么写的, 测试自己炸在这儿)
         "import sys; home, repo = sys.argv[1], sys.argv[2];"
         "sys.argv = ['x', home]; sys.path.insert(0, repo);"
         "import fleet_bridge as b;"
         "print('OK', repr(b._CLAUDE_MD))",
         str(home), str(_SRC_FILE.parent)],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "FEISHU_BRIDGE_REPO": str(repo)})
    assert r.returncode == 0, f"导入期崩了:\n{r.stderr[-800:]}"
    assert "OK ''" in r.stdout, r.stdout
    assert "读不出" in r.stdout or "读不出" in r.stderr, "降级了但没留痕, 那是另一种坏"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

"""Behavioral regression coverage."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet_env import env, env_set_with  # noqa: E402

_S = "PYTEST_DUAL_X"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for p in ("FLEET_BRIDGE_", "FEISHU_BRIDGE_"):
        monkeypatch.delenv(p + _S, raising=False)


def test_new_name_wins_when_both_set(monkeypatch):
    """靶心: 两个都在时新名赢 —— 否则「我改了 .env 却没生效」会查不出原因。"""
    monkeypatch.setenv("FEISHU_BRIDGE_" + _S, "old")
    monkeypatch.setenv("FLEET_BRIDGE_" + _S, "new")
    assert env(_S) == "new"


def test_old_name_still_works(monkeypatch):
    """还没迁的实例必须照常跑 —— 这条就是整场迁移的安全网本身。"""
    monkeypatch.setenv("FEISHU_BRIDGE_" + _S, "old")
    assert env(_S) == "old"


def test_new_name_alone_works(monkeypatch):
    monkeypatch.setenv("FLEET_BRIDGE_" + _S, "new")
    assert env(_S) == "new"


def test_neither_falls_back_to_default():
    assert env(_S) == ""
    assert env(_S, "D") == "D"


def test_empty_counts_as_unset(monkeypatch):
    """空串当没设 —— 原来那些调用点写的是 getenv(x, "").strip(), 空串本来就走默认路径。
    不这样的话, 一个手滑写成 `FLEET_BRIDGE_X=` 的 .env 会把旧名**屏蔽掉**, 而且不报错。"""
    monkeypatch.setenv("FLEET_BRIDGE_" + _S, "")
    monkeypatch.setenv("FEISHU_BRIDGE_" + _S, "old")
    assert env(_S) == "old", "新名为空串时该继续回落旧名"


def test_diagnostic_tells_which_one_won(monkeypatch):
    """迁移期最难查的是「我改了怎么没生效」—— 这个函数让进程能自报读的是哪个名字。"""
    assert env_set_with(_S) == ""
    monkeypatch.setenv("FEISHU_BRIDGE_" + _S, "old")
    assert env_set_with(_S) == "FEISHU_BRIDGE_" + _S
    monkeypatch.setenv("FLEET_BRIDGE_" + _S, "new")
    assert env_set_with(_S) == "FLEET_BRIDGE_" + _S


def test_module_imports_with_only_the_new_names():
    """Behavioral regression coverage."""
    import subprocess
    import tempfile
    root = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("FEISHU_BRIDGE_")}
    e["FLEET_BRIDGE_CLAUDE_DIR"] = tempfile.mkdtemp()
    e["FLEET_BRIDGE_HOME"] = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, "-c", "import fleet_bridge"],
                       cwd=root, env=e, capture_output=True, text=True)
    assert r.returncode == 0, f"只设新名时导入失败:\n{r.stderr[-800:]}"


def test_no_raw_old_reads_left_in_source():
    """兜底: 三种读环境变量的写法都不许出现旧名字面量。

    子进程那条钉的是"必需的读取"; 这条兜住"可选的读取" —— 后者不会让导入失败, 只会在
    迁移完成后**静默拿到空值**, 那种错要等某个功能莫名其妙不工作才被发现。
    **写入端 (生成 .env 的地方) 故意不在此列**: 那是阶段 2.5 的活, 现在改会让新建的 bot
    拿到还没铺双轨的代码读不懂的名字。
    """
    import re
    root = Path(__file__).resolve().parent.parent
    reads = (r'getenv\(\s*["\']FEISHU_BRIDGE_',
             r'environ\[\s*["\']FEISHU_BRIDGE_',
             r'environ\.get\(\s*["\']FEISHU_BRIDGE_')
    for name in ("fleet_bridge.py", "app_frontend.py", "fleet_app_provision.py"):
        src = root.joinpath(name).read_text(encoding="utf-8")
        for pat in reads:
            hit = re.search(pat, src)
            assert not hit, f"{name} 还有裸读旧名: {src[hit.start():hit.start()+60]!r}"














def test_provision_writes_new_names():
    """配应用时写回 .env 的那处 —— 它在 bridge 仓, 会跟着 rollout 铺。"""
    src = (Path(__file__).resolve().parent.parent / "fleet_app_provision.py"
           ).read_text(encoding="utf-8")
    assert '"FLEET_BRIDGE_APP_ID"' in src and '"FLEET_BRIDGE_APP_SECRET"' in src
    assert '"FEISHU_BRIDGE_' not in src, "写入端还有旧名"


def test_dual_read_still_accepts_what_provision_writes(monkeypatch):
    """闭环: provision 写出去的名字, helper 读得回来。

    分开测两端会漏掉"两边各自都对、但拼不上"的情况 —— 这个仓今天刚踩过一次同形的
    (守卫走 helper、取值裸读, 两半各自都"改过了")。
    """
    monkeypatch.setenv("FLEET_BRIDGE_APP_ID", "cli_xxx")
    assert env("APP_ID") == "cli_xxx"















def test_env_any_tries_names_in_order(monkeypatch):
    from fleet_env import env_any  # noqa: PLC0415
    monkeypatch.setenv("PYTEST_OLD_N", "old")
    assert env_any("PYTEST_NEW_N", "PYTEST_OLD_N") == "old"
    monkeypatch.setenv("PYTEST_NEW_N", "new")
    assert env_any("PYTEST_NEW_N", "PYTEST_OLD_N") == "new"
    monkeypatch.delenv("PYTEST_NEW_N")
    monkeypatch.delenv("PYTEST_OLD_N")
    assert env_any("PYTEST_NEW_N", "PYTEST_OLD_N", default="D") == "D"


def test_watchdog_reads_both_names():
    """watchdog 三个变量都要双轨 —— 它是**救援机制**, 迁移期间失灵的话
    没有任何东西会来救那些挂死的 bot, 而它自己不出声。"""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    for var, val in (("FLEET_HEARTBEAT_STALE_S", "99"), ("FEISHU_HEARTBEAT_STALE_S", "88")):
        e = {k: v for k, v in os.environ.items()
             if k not in ("FLEET_HEARTBEAT_STALE_S", "FEISHU_HEARTBEAT_STALE_S")}
        e[var] = val
        r = subprocess.run([sys.executable, "-c",
                            "import fleet_watchdog as w; print(w.STALE_S)"],
                           cwd=root, env=e, capture_output=True, text=True)
        assert r.stdout.strip() == val, (var, r.stdout, r.stderr[-300:])


def test_registry_path_changed_both_prefix_and_suffix():
    """Behavioral regression coverage."""
    src = (Path(__file__).resolve().parent.parent / "fleet_env.py"
           ).read_text(encoding="utf-8")
    assert 'env_any("FLEET_REGISTRY_JSON", "FEISHU_FLEET_JSON")' in src
    # 顺带钉住"只有一处": 收口的意义就在这儿
    for f in ("fleet_watchdog.py", "fleet_admin.py"):
        other = (Path(__file__).resolve().parent.parent / f).read_text(encoding="utf-8")
        assert 'env_any("FLEET_REGISTRY_JSON"' not in other, (
            f"{f} 又自己算了一遍 fleet.json 路径 —— 走 fleet_env.fleet_registry_path()")

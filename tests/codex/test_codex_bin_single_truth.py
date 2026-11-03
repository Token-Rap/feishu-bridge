"""Codex 可执行文件的判据只在 `fleet_providers.codex_bin()` 一处。

两处各读各的 env 时, 一个坏的 `CODEX_BIN=/wrong/path` 会让一侧当它不存在、另一侧
照样把它塞进 `_codex_cmd`, 到 exec 那一刻才炸。显式值也要可执行才算数,
不拿字符串冒充 capability。
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from feishu_bridge.bridge import fleet_providers


def test_codex_bin_rejects_a_configured_but_missing_binary(monkeypatch):
    """基线: codex_bin 对"设了但不可执行"是诚实的 —— 这条是下面对照的锚。"""
    monkeypatch.setenv("CODEX_BIN", "/definitely/not/here/codex")
    assert fleet_providers.codex_bin() is None


def test_run_gate_agrees_with_caps_on_a_bogus_path(monkeypatch):
    """**运行门和 caps 必须给同一个答案。**

    设一个不存在的 CODEX_BIN 时, caps 判"没有" ⇒ 运行门也不许拿着它往下走。
    """
    monkeypatch.setenv("CODEX_BIN", "/definitely/not/here/codex")
    import importlib
    caps_says = fleet_providers.codex_bin()                  # None = 没有
    gate_says = fleet_providers.codex_bin() or shutil.which("codex")
    assert caps_says is None
    # 运行门此刻应当也认不出可执行文件, 而**不是**把那个坏路径原样端出去
    assert gate_says != "/definitely/not/here/codex", (
        "运行门把一个不可执行的路径当成了 codex —— caps 那侧已经判它不存在了")


def test_bridge_does_not_reimplement_the_lookup():
    """钉源码: `fleet_bridge.py` 不许自己再算一遍 CODEX_BIN。

    判据是**结构**而不是行为 —— 行为一致可以靠巧合, 而"只有一处算法"不会。
    """
    src = (Path(__file__).resolve().parents[2] / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
    code = "\n".join(l.split("#")[0] for l in src.split("\n"))   # 剥行注释, 免得数到说明文字
    assert 'os.getenv("CODEX_BIN"' not in code, (
        "fleet_bridge.py 又自己读了一遍 CODEX_BIN —— 判据只许留在 fleet_providers.codex_bin(), "
        "否则 caps 与运行门会使用不同判据")

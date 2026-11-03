"""跑测试的 venv 必须和 bot 实际运行的 venv 用同一个 SDK。

两个 venv 的 claude-agent-sdk 版本曾差出几十个版本而无人察觉, 期间的 API 变化测试一条都照不出来。
本条让这个分叉自己报警, 不靠谁记得去比对。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import claude_agent_sdk

# LaunchAgent 里写死的那个解释器 —— bot 真正跑的
_PROD_PY = Path.home() / ".fleet-claude-bridge" / "venv-next" / "bin" / "python"


def _prod_sdk_version() -> str | None:
    if not _PROD_PY.exists():
        return None
    try:
        r = subprocess.run(
            [str(_PROD_PY), "-c",
             "import claude_agent_sdk as s; print(s.__version__)"],
            capture_output=True, text=True, timeout=30)
    except Exception:       # noqa: BLE001
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def test_sdk_version_matches_the_one_bots_actually_run():
    prod = _prod_sdk_version()
    if prod is None:
        # 单机 checkout / CI 上没有生产 venv —— 跳过而不是假绿。
        # ⚠ 不用静默 return: 那样"没验过"和"验过了"长得一样。
        import pytest
        pytest.skip(f"生产 venv 不在 ({_PROD_PY}) —— 本条只在有它的机器上有意义")
    mine = claude_agent_sdk.__version__
    assert mine == prod, (
        f"测试跑在 SDK {mine}, 而 bot 实际跑 {prod} —— **这两者之间的 API 变化, "
        f"全量测试一条都照不出来**。\n"
        f"修法: 把 {sys.prefix} 的 claude-agent-sdk 升/降到 {prod}, 与生产对齐。\n"
        f"请确认测试环境与生产依赖版本一致。")


def test_the_probe_itself_still_works():
    """自证: 探针本身要能拿到版本号, 否则上面那条会靠 skip 蒙混过去。"""
    prod = _prod_sdk_version()
    if prod is None:
        import pytest
        pytest.skip("生产 venv 不在, 同上")
    assert prod and prod[0].isdigit(), f"读回来的版本号不像版本号: {prod!r}"

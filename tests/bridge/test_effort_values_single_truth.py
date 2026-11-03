"""Verify effort value definitions and their public interface."""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# bridge 在 import 期读 sys.argv[1] 当 HOME, 必须先接管 (同 test_app_frontend)
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
from feishu_bridge.bridge import fleet_bridge  # noqa: E402
sys.argv = _argv

_SRC = (Path(__file__).resolve().parents[2] / "src/feishu_bridge/bridge/fleet_bridge.py").read_text(encoding="utf-8")
_CODE = "\n".join(l.split("#")[0] for l in _SRC.split("\n"))   # 剥行注释, 免得数到说明文字


def test_the_set_is_defined_once():
    """字面量集合只许出现在定义处那一行。"""
    hits = re.findall(r'"low"\s*,\s*"medium"\s*,\s*"high"', _CODE)
    assert len(hits) == 1, (
        f"effort 合法值在 fleet_bridge.py 里写了 {len(hits)} 遍 —— "
        "只许留 EFFORT_ACCEPTED 一处 (fleet_env.py 模块头: 两份会分叉)")


def test_both_call_sites_use_the_constant():
    """两个用点 (`/effort` 斜杠命令 与 App 静默通道) 都得走同一个常量。"""
    assert _CODE.count("EFFORT_ACCEPTED") >= 3, (
        "至少要有 1 处定义 + 2 处引用; 现在少于 3, 说明有调用点没接上")


def test_the_constant_still_contains_what_users_type():
    """内容自证 —— 别让上面两条靠"把集合改空"作弊通过。"""
    for v in ("low", "medium", "high", "xhigh", "max", "default", "默认"):
        assert v in fleet_bridge.EFFORT_ACCEPTED, f"{v!r} 从合法值里掉了"


def test_usage_text_is_generated_not_hardcoded():
    """Behavioral regression coverage."""
    assert "用法: /effort <low|medium|high|xhigh|max|default>" not in _CODE, (
        "用法提示又写死了一份档位列表 —— 它会和 EFFORT_LEVELS 分叉")
    assert "'|'.join(EFFORT_LEVELS" in _CODE, (
        "用法提示不再从 EFFORT_LEVELS 生成了 —— 先看这条测试的 docstring 再改")


def test_generated_usage_text_only_lists_valid_values():
    """真的把提示生成一遍, 逐个 token 验 —— 结构对了也要看结果对不对。"""
    shown = "|".join(fleet_bridge.EFFORT_LEVELS + ("default",))
    for token in shown.split("|"):
        assert token in fleet_bridge.EFFORT_ACCEPTED, (
            f"生成出来的提示里有 {token!r}, 但它不是合法值")

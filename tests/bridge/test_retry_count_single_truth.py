"""Behavioral regression coverage."""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TARGETS = {"fleet_bridge.py": "_FEISHU_RETRY_MAX", "app_frontend.py": "_UPLOAD_RETRY_MAX"}


def _code(name: str) -> str:
    src = (_ROOT / "src/feishu_bridge/bridge" / name).read_text(encoding="utf-8")
    return "\n".join(l.split("#")[0] for l in src.split("\n"))   # 剥注释, 免得数到说明


def test_no_bare_retry_range():
    """循环次数不许写裸字面量。"""
    for name, const in _TARGETS.items():
        assert "for attempt in range(3)" not in _code(name), (
            f"{name} 的重试循环还写着 range(3) —— 应为 range({const})")


def test_no_bare_last_attempt_check():
    """"最后一次不退避"那道判断不许写裸字面量。

    它是最容易漏的一个: 次数改了而它没改 ⇒ 多出来的那几次**不退避**。
    """
    for name, const in _TARGETS.items():
        assert "if attempt < 2:" not in _code(name), (
            f"{name} 还写着 attempt < 2 —— 应为 attempt < {const} - 1")


def test_message_text_is_generated_from_the_constant():
    """文案里的次数必须算出来, 不许再写死一个"3"。"""
    for name, const in _TARGETS.items():
        code = _code(name)
        assert "(重试 3 次)" not in code, f"{name} 的失败文案还写死了 3 次"
        assert f"{{{const}}}" in code, f"{name} 的文案没有从 {const} 生成"


def test_the_constants_exist_and_are_sane():
    """自证 + 合理性: 扫不到常量就该红, 别让上面几条靠"文件被清空"通过。"""
    for name, const in _TARGETS.items():
        m = re.search(rf"^{const}\s*=\s*(\d+)", _code(name), re.M)
        assert m, f"{name} 里找不到 {const} 的定义 —— 先修扫描, 别改断言"
        n = int(m.group(1))
        assert 2 <= n <= 5, f"{const} = {n}, 超出合理区间 (太少救不了瞬断, 太多是在打上游)"

"""`runtime.json` 的两个写入器必须共用同一套锁与原子写 (审计 T022)。

`_write_runtime_effort` 和 `_write_runtime_model` 原来**逐行几乎相同**:
同一把 flock、同一套 `tmp.write_text` + `tmp.replace`, 只差清空条件和键名。

⚠ 收它不只是"少几行" —— 原子写是个**三步动作** (写临时文件 → 落盘 → rename 替换)。
散成两份, 哪天有人只在一处改, 另一处就变成非原子写, 而**表现是"偶尔 runtime.json
是半截的"** —— 只在崩溃/断电那一刻显形, 平时完全看不出来。

⚠ 顺带修一处**脆弱性**, 这条是真行为改动:
    effort: clear = effort in ("default", "默认")                       ← 不 strip 不 lower
    model:  clear = model.strip().lower() in ("default","默认","none","")
effort 那个能工作, **只因为两个调用点都先 lower 了** (`arg.lower()` / `effort.lower()`)。
⇒ 依赖调用点替自己做归一; 新增一个调用点忘了, `/effort DEFAULT` 就**静默不清空** ——
  用户以为恢复默认了, 实际还压着旧值。现在写入器自己归一, 不看调用点脸色。
**清空词表本身没动** (effort 仍是 default/默认两个词, 没有跟着 model 扩成四个) ——
别在收口里顺手改语义。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv

_SRC = (Path(__file__).resolve().parents[1] / "fleet_bridge.py").read_text(encoding="utf-8")
_CODE = "\n".join(l.split("#")[0] for l in _SRC.split("\n"))


def _runtime() -> dict:
    try:
        return json.loads(b._RUNTIME_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _reset() -> None:
    b._RUNTIME_FILE.unlink(missing_ok=True)


# ── 结构: 锁与原子写只许有一份 ──────────────────────────────────────
def test_atomic_write_is_implemented_once():
    n = len(re.findall(r"\.replace\(_RUNTIME_FILE\)", _CODE))
    assert n == 1, (
        f"runtime.json 的原子替换写了 {n} 遍 —— 原子写是三步动作, 散成多份就会"
        "只改一处, 而漏的那处变成非原子写, 只在崩溃那一刻显形")


def test_the_lock_is_taken_once():
    n = len(re.findall(r"_RUNTIME_FILE\.with_suffix\(\"\.lock\"\)", _CODE))
    assert n == 1, f"runtime.lock 开了 {n} 处 —— 两份锁逻辑迟早只改一处"


# ── 行为: 写入器自己归一, 不依赖调用点 ──────────────────────────────
def test_effort_writer_normalizes_its_own_input():
    """**直接**调用写入器 (不经过调用点的 .lower()), 大写也要能清空。"""
    _reset()
    b._write_runtime_effort("high")
    assert _runtime().get("effort") == "high"
    b._write_runtime_effort("DEFAULT")          # ← 原来这里不会清空
    assert "effort" not in _runtime(), (
        "大写 DEFAULT 没清掉 effort —— 写入器在依赖调用点替它 lower")


def test_model_writer_still_normalizes_its_own_input():
    _reset()
    b._write_runtime_model("claude-opus-5")
    assert _runtime().get("model") == "claude-opus-5"
    b._write_runtime_model("  DEFAULT  ")
    assert "model" not in _runtime()


def test_clear_word_lists_were_not_quietly_merged():
    """⚠ effort 的清空词表**没有**跟着 model 扩成四个 —— 收口不许顺手改语义。"""
    _reset()
    b._write_runtime_effort("high")
    b._write_runtime_effort("none")             # model 认它, effort 不认
    assert _runtime().get("effort") == "none", (
        "effort 开始认 'none' 当清空了 —— 那是 model 的词表, 收口时被混进来了")


def test_the_two_fields_do_not_clobber_each_other():
    """同一个文件两个键, 写一个不许把另一个抹掉 (读-改-写 的经典翻车点)。"""
    _reset()
    b._write_runtime_model("claude-opus-5")
    b._write_runtime_effort("xhigh")
    r = _runtime()
    assert r.get("model") == "claude-opus-5" and r.get("effort") == "xhigh", r


# 保存 effort 时也必须规范化。
def test_effort_value_is_normalized_on_the_way_in():
    """Behavioral regression coverage."""
    _reset()
    b._write_runtime_effort("HIGH")
    assert _runtime().get("effort") == "high", (
        f"落盘的是 {_runtime().get('effort')!r} —— 大写会原样进 SDK, 而合法值全是小写")
    b._write_runtime_effort("  XHigh  ")
    assert _runtime().get("effort") == "xhigh"


def test_model_value_is_NOT_lowercased():
    """⚠ model 反过来: **不许 lower**, 只 strip。

    模型名是标识符不是枚举, 大小写由上游定。effort 是封闭枚举 (low/medium/high/…),
    两者归一规则**本来就该不同** —— 这条防的是"顺手把两边统一了"。
    """
    _reset()
    b._write_runtime_model("  Claude-Opus-5  ")
    assert _runtime().get("model") == "Claude-Opus-5", "model 被 lower 了 —— 模型名大小写由上游定"


def test_clearing_leaves_an_empty_file_not_a_missing_one():
    """Behavioral regression coverage."""
    _reset()
    b._write_runtime_model("claude-opus-5")
    b._write_runtime_model("default")
    assert b._RUNTIME_FILE.exists(), "清空不该删掉文件"
    assert _runtime() == {}

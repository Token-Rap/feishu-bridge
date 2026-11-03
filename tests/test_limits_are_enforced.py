"""Behavioral regression coverage."""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
from feishu_bridge import fleet_bridge  # noqa: E402
sys.argv = _argv

_SRC = (Path(__file__).resolve().parents[1] / "src/feishu_bridge/fleet_bridge.py").read_text(encoding="utf-8")
_CODE = "\n".join(l.split("#")[0] for l in _SRC.split("\n"))


# ── _chunks: 分片本身 (原来一条测试都没有) ──────────────────────────
def test_chunks_never_exceeds_the_size():
    out = list(fleet_bridge._chunks("x" * 10000, 4000))
    assert all(len(c) <= 4000 for c in out), [len(c) for c in out]
    assert len(out) == 3


def test_chunks_loses_nothing():
    """切片不许吞字符 —— 拼回去必须和原文逐字相同。"""
    text = "".join(chr(0x4E00 + i % 200) for i in range(9999))
    assert "".join(fleet_bridge._chunks(text, 4000)) == text


def test_chunks_on_empty_and_exact_boundary():
    assert list(fleet_bridge._chunks("", 4000)) == []
    assert list(fleet_bridge._chunks("a" * 4000, 4000)) == ["a" * 4000]
    assert len(list(fleet_bridge._chunks("a" * 4001, 4000))) == 2


# ── 三个限额落在外部约束之内 ────────────────────────────────────────
def test_feishu_chunk_limit_stays_under_the_platform_cap():
    assert 0 < fleet_bridge._FEISHU_MSG_MAX <= 4000, (
        "飞书单条 text 有平台上限, 超了消息直接发不出去 —— "
        "而用户看到的只是'bot 没回我'")


def test_quote_limit_stays_small_enough_to_not_blow_the_prompt():
    assert 0 < fleet_bridge._QUOTE_MAX <= 4000, (
        "引用正文会拼进 prompt, 不截断就会被一条长回复撑爆")


def test_approval_timeout_is_short_enough_to_not_hang_a_turn():
    assert 0 < fleet_bridge._APPROVAL_TIMEOUT_S <= 1800, (
        "审批超时太大 ⇒ 用户不理它这一回合就永远挂着, 而'挂住'和'在想'长得一样")


# ── 用点确实走常量, 没有各写一份 ────────────────────────────────────
def test_send_path_uses_the_constant_not_a_literal():
    assert "_chunks(text, _FEISHU_MSG_MAX)" in _CODE, "发送分片没走常量"


def test_no_wait_hardcodes_a_timeout_number():
    """所有 `asyncio.wait_for` 的 timeout 都得是具名常量, 不许写字面数字。

    ⚠ 第一版断言写成 `set(waits) == {"_APPROVAL_TIMEOUT_S"}`, 结果把
    `ASK_WAIT_SEC` (**提问卡**的等待) 也报成违规 —— 那是另一件事, 用另一个常量
    是**对的**。扫描器太宽会制造误报, 而误报会让人学会忽略这条测试。
    ⇒ 改成钉"没有写死的数字" + 下面单独钉审批那两处。
    """
    waits = re.findall(r"asyncio\.wait_for\([^)]*?timeout=([^),]+)", _CODE)
    assert waits, "一处 wait_for 都没扫到 —— 先修扫描, 别改断言"
    hard = [w.strip() for w in waits if w.strip().replace(".", "").isdigit()]
    assert not hard, f"这些等待把超时写死成数字了: {hard} —— 改动时必然漏掉某一处"


def test_both_approval_waits_use_the_constant():
    waits = re.findall(r"asyncio\.wait_for\([^)]*?timeout=([^),]+)", _CODE)
    n = [w.strip() for w in waits].count("_APPROVAL_TIMEOUT_S")
    assert n >= 2, (
        f"只有 {n} 处审批等待走了 _APPROVAL_TIMEOUT_S (应≥2: 工具审批 + 计划审批) —— "
        "两处各写一份迟早只改一处")


def test_prompt_text_derives_the_minutes_from_the_constant():
    """Behavioral regression coverage."""
    assert "_APPROVAL_TIMEOUT_S//60" in _CODE or "_APPROVAL_TIMEOUT_S // 60" in _CODE, (
        "审批提示里的分钟数不是从常量算的 —— 会和真实超时分叉")


# ── 第二轮变异 又挖出 4 个: 全部改成 999999 没有一条测试红 ──
def test_stream_interval_stays_under_the_feishu_rate_limit():
    """Behavioral regression coverage."""
    assert fleet_bridge._STREAM_MIN_INTERVAL >= 0.1, (
        f"{fleet_bridge._STREAM_MIN_INTERVAL}s = {1/fleet_bridge._STREAM_MIN_INTERVAL:.0f}/秒, "
        "飞书红线 10/秒 —— 踩上去的表现是'回复发不出去', 跟网络故障没法区分")

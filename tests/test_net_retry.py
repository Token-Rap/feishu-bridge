"""Behavioral regression coverage."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("claude_default")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-neterr-")]
try:
    import fleet_bridge as b
finally:
    sys.argv = _argv


def test_api_error_text_is_recognized_without_is_error():
    """SDK 没标 is_error, 但 result 是 API Error 文本 → 也得当错误处理。"""
    assert b.looks_like_api_error("API Error: Connection closed mid-response. The response above…")


def test_plain_answer_is_not_mistaken_for_error():
    """正常回复不能被误判 —— 判据必须够窄, 否则真回复会被当成故障重跑一遍。"""
    for txt in ("好的, 我看了下 timeout 那个参数, 建议设 30s",
                "这个 socket 的用法是对的",
                "连接已关闭是正常现象, 不用管"):
        assert not b.looks_like_api_error(txt)


def test_empty_and_none_are_not_errors():
    assert not b.looks_like_api_error("")
    assert not b.looks_like_api_error(None)


def test_task_tools_count_as_readonly():
    """Task* 系列是只读的 (只记清单, 无副作用) —— 用了它的回合网络断了应当能安全重试。

    今早改提示词把 TodoWrite 换成 Task* 时**漏了这张白名单**, 后果是凡用 TaskCreate 的
    回合都被当成「有写操作」→ 不自动重试。改了一处要问另一处。
    """
    for t in ("TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite"):
        assert t in b._READONLY_TOOLS, f"{t} 应算只读"


def test_write_tools_still_unsafe():
    """写类工具照旧不许自动重试 —— 重跑会把副作用做第二次。"""
    for t in ("Write", "Edit", "Bash", "NotebookEdit"):
        assert t not in b._READONLY_TOOLS


# ── 集成: 判据真的接上了吗 ──────────────────────────────────────────
# 上面测的是纯函数。这一组测**接线** —— 今天在别处刚栽过: 函数写对但没接上,
# 表现和没做一模一样。
import asyncio  # noqa: E402

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

CHAT = "net-retry-test"


def _result(is_error: bool, text: str) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=is_error, num_turns=1, session_id="s1", result=text)


class _Sender:
    def __init__(self):
        self.replies: list[str] = []
        self.texts: list[str] = []

    def send_reply(self, chat, text, thinking=None, reply_to=None, extra=None):
        self.replies.append(text); return "mid-1"

    def send(self, chat, text): self.texts.append(text)
    def send_text(self, chat, text): self.texts.append(text)

    def open_stream(self, chat, reply_to=None, summary=""):
        class _S:
            async def start(self): return False
        return _S()


class _Front:
    chat_id = CHAT
    def __init__(self): self.events = []
    def broadcast(self, ev): self.events.append(ev)


class _ErrThenOK:
    """Behavioral regression coverage."""

    def __init__(self):
        self.calls = 0

    async def query(self, prompt): pass

    async def receive_response(self):
        self.calls += 1
        if self.calls == 1:
            yield _result(False, "API Error: Connection closed mid-response.")
        elif self.calls == 2:
            yield AssistantMessage(content=[TextBlock(text="重试后的正常答案")],
                                   model="test-model")
            yield _result(False, "")
        else:
            await asyncio.Event().wait()


def test_unflagged_api_error_still_triggers_retry(monkeypatch):
    """改之前: is_error=False → 不重试、不提示, 直接把错误文本当回复发给用户。
    改之后: 认出它是 API Error → 自动整轮重试 → 用户拿到真答案。"""
    async def _run():
        sender, front = _Sender(), _Front()
        b.SENDER, b.APP_FRONT = sender, front
        cli = _ErrThenOK()
        b.CLIENTS[CHAT] = cli
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        await b.run_turn(CHAT, "测试问题")
        await b._stop_idle_watch(CHAT)
        return sender, cli

    sender, cli = asyncio.run(_run())
    assert cli.calls >= 2, "没有重试 —— 判据没接上"
    assert any("重试后的正常答案" in r for r in sender.replies)
    assert not any("API Error" in r for r in sender.replies), "错误文本不该发给用户"















class _RecordingErrThenOK:
    """记下每次 query 收到的是什么 —— **语义全在这个参数上**。

    ⚠ 第一轮必须**先吐一点正文**再给错误 result。不然它是个"空回合"(零模型活动),
    会先被 run_turn 里那条「流中残留终止符 → 重新读取」的路径接走 —— 那条路
    **不重发 query, 只是再读一次响应**, 根本到不了网络重试。
    我第一版就是这么写的, 于是测试红了却不是因为实现没做。
    (顺带: 同文件更早那条 `test_unflagged_api_error_still_triggers_retry` 也只断言
     `calls >= 2`, 而 calls 是 receive_response 的次数 —— **空回合重读同样会让它 +1**,
     所以那条测试至今没有真正区分过这两条路径。这里用 `prompts` 而不是 `calls`,
     就是为了钉死"到底重发了没有"。)
    """

    def __init__(self):
        self.calls = 0
        self.prompts = []

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        self.calls += 1
        if self.calls == 1:
            yield AssistantMessage(content=[TextBlock(text="我先做了一半…")],
                                   model="test-model")
            yield _result(False, "API Error: Connection lost mid-response.")
        elif self.calls == 2:
            yield AssistantMessage(content=[TextBlock(text="重试后的正常答案")],
                                   model="test-model")
            yield _result(False, "")
        else:
            await asyncio.Event().wait()


def _run_turn_with(cli, prompt="帮我改个文件", unsafe=True):
    async def _run():
        sender, front = _Sender(), _Front()
        b.SENDER, b.APP_FRONT = sender, front
        b.CLIENTS[CHAT] = cli
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        await b.run_turn(CHAT, prompt)
        await b._stop_idle_watch(CHAT)
        return sender
    return asyncio.run(_run())


def test_retry_sends_a_continuation_not_the_original_prompt():
    """靶心: 重试发的必须是「重试」, 不是原 prompt。

    机械重发原 prompt = 让模型从头再做一遍, 已经写掉的文件会被再写一次;
    发「重试」= 模型看得到完整上下文, 自己决定从哪儿接 —— 这正是用户手点按钮时
    一直在工作的行为 (7e62a46 给 /retry 定的语义)。
    """
    cli = _RecordingErrThenOK()
    _run_turn_with(cli)
    assert len(cli.prompts) >= 2, cli.prompts
    assert cli.prompts[0] == "帮我改个文件", cli.prompts
    assert cli.prompts[1] == "重试", f"重试发的应该是 continuation, 实际: {cli.prompts[1]!r}"


def test_the_tip_no_longer_claims_it_did_not_retry():
    """重试之后再失败时, 不许还说「未自动重试以防重复」—— 那句会变成假的。"""
    tip = b._failure_tip(True, True, "API Error: Connection lost mid-response.")
    assert "未自动重试" not in tip, tip
    assert "已自动重试" in tip, tip


def test_write_op_alone_still_says_it_did_not_retry():
    """回归: 没重试过的那种 (非网络错误 + 有写操作) 那句话仍然是真的。"""
    tip = b._failure_tip(True, False, "某种别的错误")
    assert "未自动重试" in tip, tip


def test_auth_failure_still_never_retries():
    """认证失效不许被这次放开顺带卷进来 —— 重试用同一份凭据, 是无用功。"""
    assert not any(k in "failed to authenticate: oauth session expired"
                   for k in b._NET_ERR_KEYS), "认证错误不该落进网络错误关键词"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

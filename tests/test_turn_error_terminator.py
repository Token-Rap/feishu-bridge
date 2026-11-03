"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-test-home-")]
try:
    from feishu_bridge import fleet_bridge as b
finally:
    sys.argv = _argv

CHAT = "turn-error-test"


class StubFeishu:
    def __init__(self):
        self.texts: list[str] = []

    def send(self, chat, text):
        self.texts.append(text)


class StubSender:
    """只实现本测试用到的; feishu 属性对应真实的 TeeTransport.feishu。"""

    def __init__(self):
        self.feishu = StubFeishu()

    def send(self, chat, text):
        self.feishu.send(chat, text)

    def send_text(self, chat, text):
        self.feishu.send(chat, text)

    def send_reply(self, chat, text, thinking=None, reply_to=None, extra=None):
        return "mid-1"

    def open_stream(self, chat, reply_to=None, summary=""):
        class _S:
            async def start(self):
                return False
        return _S()

    def begin_busy(self, message_id):
        return None

    def end_busy(self, message_id, reaction_id):
        pass


class StubFront:
    chat_id = CHAT

    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, ev):
        self.events.append(ev)


class BoomClient:
    """模拟 CLI 子进程被 SIGTERM: 回合中途抛 CLIConnectionError 那一类。"""

    async def query(self, prompt):
        pass

    async def receive_response(self):
        raise RuntimeError("Cannot write to terminated process (exit code: 143)")
        yield  # pragma: no cover  (让它是 async generator)


def _run_boom_turn():
    async def _run():
        sender, front = StubSender(), StubFront()
        b.SENDER, b.APP_FRONT = sender, front
        b.CLIENTS[CHAT] = BoomClient()
        # 签名要对齐 _get_client 实际会读到的 runtime.json, 不能假设 DEFAULT_*——
        # 该文件是跨测试文件共享的模块单例状态, 全套跑时可能已被别的用例改写
        # (e.g. /model 测试), 签名对不上会导致 _get_client 把这里的桩 client 换成
        # 真 client, 整个异常场景就模拟不出来了。
        b.CLIENT_SIG[CHAT] = b._runtime_sig(b._read_runtime())
        await b.run_turn(CHAT, "会炸的一轮")
        await b._stop_idle_watch(CHAT)
        return sender, front

    return asyncio.run(_run())


def test_error_event_is_broadcast():
    """核心: App/Web 必须收到 ev=error —— 那是它们清「生成中」的依据。"""
    _sender, front = _run_boom_turn()
    kinds = [e.get("ev") for e in front.events]
    assert "error" in kinds, f"异常路径没发终结事件, 只有: {kinds}"


def test_error_event_carries_reason():
    _sender, front = _run_boom_turn()
    err = next(e for e in front.events if e.get("ev") == "error")
    assert "内部错误" in err.get("text", "")


def test_feishu_still_gets_text():
    """飞书那侧不能因为改事件类型就没了文字。"""
    sender, _front = _run_boom_turn()
    assert any("内部错误" in t for t in sender.feishu.texts)


def test_no_duplicate_notice_for_app():
    """App 侧只该收到一条 (error), 不该 notice + error 各来一条重复刷屏。"""
    _sender, front = _run_boom_turn()
    texts = [e for e in front.events
             if e.get("ev") in ("notice", "error") and "内部错误" in (e.get("text") or "")]
    assert len(texts) == 1, f"重复播报: {texts}"


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")

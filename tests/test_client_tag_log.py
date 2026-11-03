"""Behavioral regression coverage."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
_argv = sys.argv
sys.argv = [_argv[0], tempfile.mkdtemp(prefix="bridge-client-tag-")]
try:
    from feishu_bridge.bridge import fleet_bridge as b       # 导入期就读 argv[1] 当 home, 必须在这个块里
    from feishu_bridge.bridge.app_frontend import AppFrontend
finally:
    sys.argv = _argv


@pytest.fixture
def front(tmp_path):
    return AppFrontend(tmp_path, log=lambda _m: None)


def _feed(front, frames) -> list[tuple]:
    """把几帧喂进读循环, 返回 inject 收到的 (text, client)。"""
    got: list[tuple] = []

    async def _inject(text, client="缺省未传"):
        got.append((text, client))

    async def _drive():
        front._inject = _inject
        front._loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        for f in frames:
            reader.feed_data((json.dumps(f) + "\n").encode())
        reader.feed_eof()

        class _W:
            def write(self, _b): ...
            async def drain(self): ...
            def close(self): ...
            async def wait_closed(self): ...
            def get_extra_info(self, *_a): return None
            def is_closing(self): return False

        await front._on_conn(reader, _W())
        await asyncio.sleep(0)      # 让 _spawn 出去的注入任务跑一轮

    asyncio.run(_drive())
    return got


def test_client_reaches_the_injector(front):
    got = _feed(front, [{"type": "user_message", "text": "你好", "client": "ios"}])
    assert got == [("你好", "ios")], got


def test_missing_client_is_unknown_not_guessed(front):
    """老网关不带这个字段 —— 记「不知道」, 别猜成 web (猜错的标识和真的长得一样)。"""
    got = _feed(front, [{"type": "user_message", "text": "你好"}])
    assert got == [("你好", "unknown")], got


def test_answer_frame_also_carries_client(front):
    got = _feed(front, [{"type": "answer", "answers": [{"header": "选", "answer": "A"}]}])
    assert got and got[0][1] == "unknown", got


def test_shared_answer_exposes_actor_and_clean_text_to_answer_handler(front):
    """公共群成员点提问卡时，回答回执仍要显示真实回答者，不能退化成 bot 自己。"""
    seen = []

    def _answer(text, ask_id):
        seen.append((text, ask_id, b.app_frontend.APP_USER.get(),
                     b.app_frontend.APP_USER_TEXT.get()))
        return True

    async def _drive():
        front._answer_ask = _answer
        await front._handle_frame({"type": "answer", "ask_id": "ask-public",
                                   "answers": [{"header": "选", "answer": "A"}],
                                   "from": "sample-user"}, None)

    asyncio.run(_drive())
    assert seen == [("选: A", "ask-public", "sample-user", "选: A")]
    assert b.app_frontend.APP_USER.get() == ""
    assert b.app_frontend.APP_USER_TEXT.get() == ""


def test_shared_bot_prefix_still_applied(front):
    """共享 bot 的说话人前缀是既有行为, 加字段不许把它挤掉。"""
    got = _feed(front, [{"type": "user_message", "text": "你好", "from": "sample-user", "client": "web"}])
    assert got == [("[sample-user] 你好", "web")], got


# ── 一帧出错不许拆掉整条通道 ────────────────────────────────────────
def test_bad_frame_does_not_kill_the_connection(front):
    """今天改 inject 签名时**真炸过**: 旧回调少一个参数 → TypeError → 读循环被外层
    `except: pass` 吃掉 → 这一帧静默丢弃、连接断开、网关 5 秒后重连, 两侧都不报错。

    所以判据不是「不会有坏帧」, 而是**一帧坏了后面的帧还能到**。"""
    got: list[tuple] = []
    seen: list[str] = []
    front._log = seen.append
    calls: list[str] = []

    async def _ok(text, client):
        got.append((text, client))

    def _first_one_blows_up(text, client="unknown"):
        calls.append(text)
        if len(calls) == 1:
            raise TypeError("回调签名对不上")  # 分派期错误
        return _ok(text, client)

    async def _drive():
        front._inject = _first_one_blows_up
        front._loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        for f in ({"type": "user_message", "text": "第一条", "client": "ios"},
                  {"type": "user_message", "text": "第二条", "client": "ios"}):
            reader.feed_data((json.dumps(f) + "\n").encode())
        reader.feed_eof()

        class _W:
            def write(self, _b): ...
            async def drain(self): ...
            def close(self): ...
            async def wait_closed(self): ...
            def get_extra_info(self, *_a): return None
            def is_closing(self): return False

        await front._on_conn(reader, _W())
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert got == [("第二条", "ios")], f"坏帧把后面的帧一起带走了: {got}"
    assert any("本帧丢弃" in l for l in seen), f"丢帧要留痕, 不能静默: {seen}"


# ── 日志措辞: 记标识, 别记翻译 ──────────────────────────────────────
def test_log_line_carries_the_identifier_verbatim(monkeypatch):
    """这行是本次改动的靶心: 读日志的人要看到 client=ios, 而不是我们翻译的「Web 消息」。"""
    lines: list[str] = []
    monkeypatch.setattr(b, "log", lines.append)
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "oc_test1234")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", lambda _e: None)
    monkeypatch.setattr(b, "SENDER", _Sender())
    monkeypatch.setattr(b, "handle_message", _noop)
    asyncio.run(b._app_inject("你好", client="ios"))
    said = " ".join(lines)
    assert "client=ios" in said, said
    assert "Web 消息" not in said, f"翻译还在, 这正是把人带偏的那一句: {said}"


def test_internal_group_task_runs_but_does_not_emit_a_duplicate_user_bubble(monkeypatch):
    events = []
    ran = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "oc_test1234")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "SENDER", _Sender())

    async def _ran(*args, **_kwargs):
        ran.append(args)

    monkeypatch.setattr(b, "handle_message", _ran)
    tok = b.app_frontend.INTERNAL_GROUP_TASK.set(True)
    try:
        asyncio.run(b._app_inject("内部 brief", client="bot"))
    finally:
        b.app_frontend.INTERNAL_GROUP_TASK.reset(tok)
    assert ran, "内部指令仍要送进模型"
    assert not any(e.get("ev") == "user" for e in events), "摘要卡之外不该再画 user 气泡"


def test_shared_user_echo_keeps_actor_structured_and_text_clean(monkeypatch):
    """模型看到 `[sample-user]`，客户端收到 `user:sample-user + 原文`；不能把身份永远焊进正文。"""
    events = []
    prompts = []
    monkeypatch.setattr(b.APP_FRONT, "chat_id", "oc_test1234")
    monkeypatch.setattr(b.APP_FRONT, "broadcast", events.append)
    monkeypatch.setattr(b, "SENDER", _Sender())

    async def _ran(_chat, text, _msg):
        prompts.append(text)

    monkeypatch.setattr(b, "handle_message", _ran)
    tok_user = b.app_frontend.APP_USER.set("sample-user")
    tok_text = b.app_frontend.APP_USER_TEXT.set("晚上吃什么")
    try:
        asyncio.run(b._app_inject("[sample-user] 晚上吃什么", client="web"))
    finally:
        b.app_frontend.APP_USER_TEXT.reset(tok_text)
        b.app_frontend.APP_USER.reset(tok_user)
    user = next(e for e in events if e.get("ev") == "user")
    assert user["user"] == "sample-user"
    assert user["text"] == "晚上吃什么"
    assert prompts == ["[sample-user] 晚上吃什么"]


def test_shared_image_context_keeps_actor_and_clean_caption(front):
    got = []

    async def _image(paths, caption):
        got.append((paths, caption, b.app_frontend.APP_USER.get(),
                    b.app_frontend.APP_USER_TEXT.get()))

    async def _drive():
        front._inject_image = _image
        front._loop = asyncio.get_running_loop()
        await front._handle_frame({"type": "user_image", "paths": ["/tmp/a.png"],
                                   "caption": "看这张", "from": "sample-user"}, None)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert got == [(["/tmp/a.png"], "[sample-user] 看这张", "sample-user", "看这张")]


class _Sender:
    class feishu:
        @staticmethod
        def send(*_a, **_k): ...


async def _noop(*_a, **_k):
    return None


if __name__ == "__main__":      # noqa: PLR0133
    # 直接运行测试文件不会执行 pytest 测试。
    raise SystemExit(
        "❌ 这样跑一条测试都不会执行 (退出码 0 会骗你)。用: venv/bin/python -m pytest "
        + __file__ + " -q")
